#!/usr/bin/env python3
"""
DOOM Process Reaper — Standalone daemon that monitors and kills orphaned
Claude subprocesses. Prevents zombie accumulation and resource exhaustion.

Features:
  - Scans for orphaned claude CLI processes every 30 seconds
  - Enforces 15-minute decree timeout (kills claude processes running too long)
  - Cleans stale entries from the `processes` table in memory.db
  - Logs all cleanup events to the chronicle table
  - Designed to be spawned at startup alongside other DOOM daemons

Usage:
    python reaper.py              # Run the reaper daemon loop
    python reaper.py --once       # Single sweep and exit
    python reaper.py --dry-run    # Show what would be reaped without killing

This file lives in the project folder but is designed to be copied to or
symlinked from ~/Desktop/DOOMBOT/ and launched by start.sh.
"""

import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

# ── Configuration ────────────────────────────────────────────────────────────

DOOMBOT_DIR = os.path.expanduser("~/Desktop/DOOMBOT")
DB_PATH = os.path.join(DOOMBOT_DIR, "memory.db")
LOG_DIR = os.path.join(DOOMBOT_DIR, "logs")

SCAN_INTERVAL = 30        # seconds between sweeps
DECREE_TIMEOUT = 900      # 15 minutes in seconds
ORPHAN_GRACE = 60         # seconds before a parentless process is considered orphaned

# Known DOOM framework process patterns (do NOT kill these)
PROTECTED_PATTERNS = [
    r"server\.py",
    r"worker\.py",
    r"watchtower\.py",
    r"introspect\.py",
    r"reaper\.py",
    r"doom_healthmon",
]

# The main interactive claude session (user's terminal) — never kill
# Identified by being attached to a TTY (has terminal in ps output)

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    log("Shutdown signal received. Exiting after current sweep.")


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ── Helpers ──────────────────────────────────────────────────────────────────

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] REAPER: {msg}"
    print(line, flush=True)


def get_db():
    """Get a database connection with WAL mode and row factory."""
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def chronicle_log(content, event_type="warning"):
    """Write an event to the chronicle table."""
    try:
        conn = get_db()
        if not conn:
            return
        conn.execute(
            "INSERT INTO chronicle (session_id, event_type, agent_id, content, timestamp) "
            "VALUES ((SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1), ?, 'REAPER', ?, ?)",
            (event_type, content, now()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"Chronicle write failed: {e}")


# ── Process Discovery ────────────────────────────────────────────────────────

def get_claude_processes():
    """
    Get all running claude CLI processes via `ps`.
    Returns list of dicts: {pid, ppid, elapsed_seconds, command, has_tty}
    """
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,ppid,etime,tty,command"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return []
    except Exception as e:
        log(f"ps command failed: {e}")
        return []

    processes = []
    for line in result.stdout.strip().split("\n")[1:]:  # skip header
        line = line.strip()
        if not line:
            continue

        # Parse ps output
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue

        pid_str, ppid_str, etime, tty, command = parts

        # Only care about claude processes
        if "/claude" not in command and "claude -p" not in command:
            continue

        try:
            pid = int(pid_str)
            ppid = int(ppid_str)
        except ValueError:
            continue

        # Skip our own process
        if pid == os.getpid():
            continue

        elapsed = parse_etime(etime)
        has_tty = tty not in ("??", "-", "?")

        processes.append({
            "pid": pid,
            "ppid": ppid,
            "elapsed_seconds": elapsed,
            "command": command,
            "has_tty": has_tty,
        })

    return processes


def parse_etime(etime_str):
    """
    Parse ps etime format into seconds.
    Formats: MM:SS, HH:MM:SS, D-HH:MM:SS
    """
    total = 0
    try:
        # Handle days
        if "-" in etime_str:
            days_part, time_part = etime_str.split("-", 1)
            total += int(days_part) * 86400
        else:
            time_part = etime_str

        parts = time_part.split(":")
        if len(parts) == 3:
            total += int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            total += int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 1:
            total += int(parts[0])
    except (ValueError, IndexError):
        return 0
    return total


def is_worker_alive():
    """Check if the DOOM worker process is running."""
    pid_file = os.path.join(LOG_DIR, "worker.pid")
    if not os.path.exists(pid_file):
        return False
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # signal 0 = check existence
        return True
    except (OSError, ValueError):
        return False


def get_tracked_processes():
    """Get processes tracked in memory.db processes table."""
    try:
        conn = get_db()
        if not conn:
            return []
        rows = conn.execute(
            "SELECT pid, agent_id, decree_id, started_at, status "
            "FROM processes WHERE status='running'"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log(f"DB query failed: {e}")
        return []


def get_active_decree_ids():
    """Get decree IDs that are currently active (being worked on)."""
    try:
        conn = get_db()
        if not conn:
            return set()
        rows = conn.execute(
            "SELECT id FROM decrees WHERE status='active'"
        ).fetchall()
        conn.close()
        return {r["id"] for r in rows}
    except Exception:
        return set()


# ── Reaping Logic ────────────────────────────────────────────────────────────

def classify_process(proc, tracked_pids, worker_alive):
    """
    Classify a claude process. Returns one of:
      'interactive' — user's terminal session, never kill
      'tracked'     — known worker-spawned process, check timeout
      'orphaned'    — no parent worker, no tracking, should be killed
      'protected'   — matches a protected pattern
    """
    cmd = proc["command"]

    # Interactive sessions have a TTY
    if proc["has_tty"]:
        return "interactive"

    # Check if it matches protected framework patterns
    # Only check the executable path (before --system-prompt), not args which may
    # contain mentions of these files in the bot's system prompt text
    cmd_executable = cmd.split("--system-prompt")[0] if "--system-prompt" in cmd else cmd
    cmd_executable = cmd_executable.split(" -p ")[0] if " -p " in cmd_executable else cmd_executable
    for pattern in PROTECTED_PATTERNS:
        if re.search(pattern, cmd_executable):
            return "protected"

    # Check if PID is tracked in the processes table
    if proc["pid"] in tracked_pids:
        return "tracked"

    # Check if parent is the worker
    if worker_alive:
        worker_pid_file = os.path.join(LOG_DIR, "worker.pid")
        try:
            with open(worker_pid_file) as f:
                worker_pid = int(f.read().strip())
            if proc["ppid"] == worker_pid:
                return "tracked"
        except (OSError, ValueError):
            pass

    # If parent PID is 1 (init/launchd) and not tracked — orphaned
    if proc["ppid"] == 1:
        return "orphaned"

    # If parent is not worker and not tracked and been running > grace period
    if proc["elapsed_seconds"] > ORPHAN_GRACE:
        return "orphaned"

    return "tracked"  # give benefit of doubt for young processes


def kill_process(pid, reason, dry_run=False):
    """Kill a process by PID. Returns True if killed."""
    if dry_run:
        log(f"[DRY-RUN] Would kill PID {pid}: {reason}")
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        log(f"SIGTERM sent to PID {pid}: {reason}")

        # Wait briefly for graceful shutdown
        time.sleep(2)

        # Check if still alive, force kill
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
            log(f"SIGKILL sent to PID {pid} (did not exit after SIGTERM)")
        except OSError:
            pass  # already dead, good

        return True
    except OSError as e:
        log(f"Could not kill PID {pid}: {e}")
        return False


def clean_stale_db_entries():
    """Remove processes table entries whose PIDs no longer exist."""
    try:
        conn = get_db()
        if not conn:
            return 0
        rows = conn.execute(
            "SELECT pid, agent_id, decree_id FROM processes WHERE status='running'"
        ).fetchall()

        cleaned = 0
        for row in rows:
            pid = row["pid"]
            try:
                os.kill(pid, 0)
            except OSError:
                # Process is dead — mark in DB
                conn.execute(
                    "UPDATE processes SET status='reaped', ended_at=?, kill_reason='stale_entry_reaped' WHERE pid=? AND status='running'",
                    (now(), pid),
                )
                cleaned += 1

        if cleaned > 0:
            conn.commit()
            log(f"Cleaned {cleaned} stale DB entries from processes table")
        conn.close()
        return cleaned
    except Exception as e:
        log(f"DB cleanup failed: {e}")
        return 0


# ── Main Sweep ───────────────────────────────────────────────────────────────

def sweep(dry_run=False):
    """
    Perform one reaper sweep:
    1. Find all claude processes
    2. Classify each (interactive, tracked, orphaned)
    3. Kill orphans and timed-out processes
    4. Clean stale DB entries
    5. Log events
    """
    claude_procs = get_claude_processes()
    if not claude_procs:
        return {"orphans_killed": 0, "timeouts_killed": 0, "stale_cleaned": 0}

    tracked_db = get_tracked_processes()
    tracked_pids = {p["pid"] for p in tracked_db}
    worker_alive = is_worker_alive()

    orphans_killed = 0
    timeouts_killed = 0

    for proc in claude_procs:
        classification = classify_process(proc, tracked_pids, worker_alive)

        if classification == "interactive":
            continue

        if classification == "protected":
            continue

        if classification == "orphaned":
            reason = f"Orphaned claude process (PPID={proc['ppid']}, age={proc['elapsed_seconds']}s)"
            if kill_process(proc["pid"], reason, dry_run):
                orphans_killed += 1
                chronicle_log(f"Reaped orphaned process PID {proc['pid']} ({reason})")

                # Update DB if tracked
                if proc["pid"] in tracked_pids:
                    try:
                        conn = get_db()
                        if conn:
                            conn.execute(
                                "UPDATE processes SET status='reaped', ended_at=?, kill_reason=? WHERE pid=? AND status='running'",
                                (now(), "orphan_reaped", proc["pid"]),
                            )
                            conn.commit()
                            conn.close()
                    except Exception:
                        pass

        elif classification == "tracked":
            # Check decree timeout (15 minutes)
            if proc["elapsed_seconds"] > DECREE_TIMEOUT:
                reason = f"Decree timeout exceeded ({proc['elapsed_seconds']}s > {DECREE_TIMEOUT}s)"
                if kill_process(proc["pid"], reason, dry_run):
                    timeouts_killed += 1
                    chronicle_log(f"Reaped timed-out process PID {proc['pid']} ({reason})")

                    # Update DB
                    try:
                        conn = get_db()
                        if conn:
                            conn.execute(
                                "UPDATE processes SET status='reaped', ended_at=?, kill_reason=? WHERE pid=? AND status='running'",
                                (now(), "decree_timeout", proc["pid"]),
                            )
                            conn.commit()
                            conn.close()
                    except Exception:
                        pass

    # Clean stale entries
    stale_cleaned = clean_stale_db_entries()

    return {
        "orphans_killed": orphans_killed,
        "timeouts_killed": timeouts_killed,
        "stale_cleaned": stale_cleaned,
    }


# ── Daemon Loop ──────────────────────────────────────────────────────────────

def run_daemon():
    """Run the reaper as a continuous daemon."""
    log("Process Reaper started")
    log(f"  Scan interval: {SCAN_INTERVAL}s")
    log(f"  Decree timeout: {DECREE_TIMEOUT}s (15m)")
    log(f"  DB path: {DB_PATH}")
    chronicle_log("Process Reaper daemon started", event_type="spawn")

    sweep_count = 0

    while not _shutdown:
        try:
            results = sweep()
            sweep_count += 1

            # Only log if something happened or every 10th sweep for heartbeat
            total_actions = results["orphans_killed"] + results["timeouts_killed"] + results["stale_cleaned"]
            if total_actions > 0:
                log(f"Sweep #{sweep_count}: killed {results['orphans_killed']} orphans, "
                    f"{results['timeouts_killed']} timeouts, cleaned {results['stale_cleaned']} stale entries")
            elif sweep_count % 20 == 0:
                log(f"Sweep #{sweep_count}: all clear (heartbeat)")

        except Exception as e:
            log(f"Sweep error: {e}")

        # Sleep in small increments to allow graceful shutdown
        for _ in range(SCAN_INTERVAL):
            if _shutdown:
                break
            time.sleep(1)

    log("Process Reaper shutting down")
    chronicle_log("Process Reaper daemon stopped", event_type="warning")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="DOOM Process Reaper")
    parser.add_argument("--once", action="store_true", help="Single sweep and exit")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be reaped")
    args = parser.parse_args()

    if args.once or args.dry_run:
        log("Running single sweep" + (" (dry-run)" if args.dry_run else ""))
        results = sweep(dry_run=args.dry_run)
        log(f"Results: {results}")
    else:
        run_daemon()


if __name__ == "__main__":
    main()
