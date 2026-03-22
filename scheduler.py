#!/usr/bin/env python3
"""
DOOM Scheduler — Cron-style decree execution

Checks every 60 seconds for decrees with a `schedule` field.
When it's time, creates a new open decree from the template.

Schedule format (simplified cron):
  "every 6h"      — every 6 hours
  "every 30m"     — every 30 minutes
  "daily 06:00"   — daily at 6:00 AM UTC
  "daily 22:00"   — daily at 10:00 PM UTC
  "weekly mon 09:00" — every Monday at 9AM UTC

Usage:
    python scheduler.py              # Run the daemon
    python scheduler.py --once       # Check once and exit
"""

import argparse
import fcntl
import os
import re
import secrets
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

DOOMBOT_DIR = os.path.dirname(os.path.abspath(__file__))
DOOM_DIR = DOOMBOT_DIR  # Alias used by heartbeat
DB_PATH = os.path.join(DOOMBOT_DIR, "memory.db")
LOG_DIR = os.path.join(DOOMBOT_DIR, "logs")
LOCK_FILE = os.path.join(LOG_DIR, "scheduler.lock")
CHECK_INTERVAL = 60  # Check every 60 seconds
HEARTBEAT_INTERVAL = 1800  # 30 minutes

_shutdown = False


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    if not os.path.exists(DB_PATH):
        print(f"[SCHEDULER] ERROR: {DB_PATH} not found")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def log_chronicle(conn, event_type, content, agent_id="SCHEDULER"):
    session = conn.execute(
        "SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
    ).fetchone()
    session_id = session["id"] if session else None
    conn.execute(
        "INSERT INTO chronicle (session_id, event_type, agent_id, content, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, event_type, agent_id, content, now()),
    )
    conn.commit()


def parse_schedule(schedule_str):
    """Parse schedule string. Returns (should_run: bool, next_run_description: str)."""
    s = schedule_str.strip().lower()

    # "every Xh" or "every Xm"
    match = re.match(r'every\s+(\d+)(h|m)', s)
    if match:
        val, unit = int(match.group(1)), match.group(2)
        return {"type": "interval", "seconds": val * 3600 if unit == "h" else val * 60}

    # "daily HH:MM"
    match = re.match(r'daily\s+(\d{2}):(\d{2})', s)
    if match:
        return {"type": "daily", "hour": int(match.group(1)), "minute": int(match.group(2))}

    # "weekly DAY HH:MM"
    match = re.match(r'weekly\s+(mon|tue|wed|thu|fri|sat|sun)\s+(\d{2}):(\d{2})', s)
    if match:
        day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        return {"type": "weekly", "weekday": day_map[match.group(1)],
                "hour": int(match.group(2)), "minute": int(match.group(3))}

    return None


def should_run(schedule_parsed, last_run_str):
    """Check if a scheduled decree should run now."""
    utc_now = datetime.now(timezone.utc)

    last_run = None
    if last_run_str:
        try:
            last_run = datetime.strptime(last_run_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    stype = schedule_parsed["type"]

    if stype == "interval":
        if not last_run:
            return True
        elapsed = (utc_now - last_run).total_seconds()
        return elapsed >= schedule_parsed["seconds"]

    elif stype == "daily":
        target_today = utc_now.replace(
            hour=schedule_parsed["hour"], minute=schedule_parsed["minute"], second=0, microsecond=0
        )
        if utc_now >= target_today:
            if not last_run or last_run < target_today:
                return True
        return False

    elif stype == "weekly":
        # Find the most recent target day
        days_since = (utc_now.weekday() - schedule_parsed["weekday"]) % 7
        target_day = utc_now - timedelta(days=days_since)
        target_dt = target_day.replace(
            hour=schedule_parsed["hour"], minute=schedule_parsed["minute"], second=0, microsecond=0
        )
        if utc_now >= target_dt:
            if not last_run or last_run < target_dt:
                return True
        return False

    return False


def _current_session_id(conn):
    """Get the current open session ID."""
    r = conn.execute("SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1").fetchone()
    return r["id"] if r else "unknown"


def run_heartbeat():
    """Run config-driven heartbeat checks from heartbeat.md"""
    hb_path = os.path.join(DOOM_DIR, "heartbeat.md")
    if not os.path.isfile(hb_path):
        return

    with open(hb_path) as f:
        lines = f.readlines()

    checks = []
    for line in lines:
        line = line.strip()
        if line.startswith("- [") and "]" in line:
            check_type = line.split("[")[1].split("]")[0]
            description = line.split("]")[1].strip()
            checks.append((check_type, description))

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    alerts = []

    for check_type, desc in checks:
        try:
            if check_type == "STALE_DECREE":
                stale = conn.execute(
                    "SELECT id, title FROM decrees WHERE status='active' AND datetime(updated_at) < datetime('now', '-2 hours')"
                ).fetchall()
                for s in stale:
                    alerts.append(f"Stale decree {s['id']}: {s['title']} (no update in 2+ hours)")

            elif check_type == "DISK_SPACE":
                import shutil
                usage = shutil.disk_usage("/")
                pct = usage.used / usage.total * 100
                if pct > 90:
                    alerts.append(f"Disk usage at {pct:.0f}%")

            elif check_type == "DB_SIZE":
                db_size = os.path.getsize(DB_PATH)
                if db_size > 50 * 1024 * 1024:
                    alerts.append(f"memory.db is {db_size / 1024 / 1024:.0f}MB")

            elif check_type == "ORPHAN_BOTS":
                active = conn.execute("SELECT id, notes FROM agents WHERE status='active'").fetchall()
                for bot in active:
                    # Check if any process is running for this bot
                    notes = bot["notes"] or ""
                    # Can't easily check PID from here, skip detailed check
                    pass

            elif check_type == "PROJECT_HEALTH":
                import urllib.request
                projects = conn.execute("SELECT id, name, port FROM projects WHERE status='running'").fetchall()
                for p in projects:
                    try:
                        urllib.request.urlopen(f"http://localhost:{p['port']}/", timeout=5)
                    except Exception:
                        alerts.append(f"Project {p['name']} (port {p['port']}) not responding")
        except Exception as e:
            print(f"[SCHEDULER] Heartbeat check {check_type} failed: {e}")

    # Log alerts to chronicle
    if alerts:
        sid = _current_session_id(conn)
        for alert in alerts:
            conn.execute(
                "INSERT INTO chronicle (session_id, event_type, agent_id, content, timestamp) VALUES (?, 'warning', 'HEARTBEAT', ?, ?)",
                (sid, alert, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
            )
            print(f"[HEARTBEAT] {alert}")
        conn.commit()
    else:
        print(f"[HEARTBEAT] All {len(checks)} checks passed")

    conn.close()


def check_scheduled_decrees():
    """Check all scheduled decrees and create instances as needed."""
    conn = get_db()
    try:
        # Find decrees with schedule field set
        scheduled = conn.execute(
            "SELECT id, title, description, priority, schedule, last_scheduled_run, model "
            "FROM decrees WHERE schedule IS NOT NULL AND schedule != ''"
        ).fetchall()

        if not scheduled:
            return 0

        created = 0
        for decree in scheduled:
            parsed = parse_schedule(decree["schedule"])
            if not parsed:
                print(f"[SCHEDULER] Invalid schedule for {decree['id']}: {decree['schedule']}")
                continue

            if should_run(parsed, decree["last_scheduled_run"]):
                # Create a new instance of this decree
                new_id = f"dc-{secrets.token_hex(2)}"
                ts = now()
                title = f"{decree['title']}"
                desc = f"[SCHEDULED from {decree['id']}] {decree['description'] or ''}"

                conn.execute(
                    "INSERT INTO decrees (id, title, description, status, priority, created_at, updated_at, model) "
                    "VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
                    (new_id, title, desc, decree["priority"], ts, ts, decree["model"])
                )

                # Update last_scheduled_run
                conn.execute(
                    "UPDATE decrees SET last_scheduled_run=? WHERE id=?",
                    (ts, decree["id"])
                )

                log_chronicle(conn, "decree",
                    f"Scheduler created {new_id} from template {decree['id']}: {title}", "SCHEDULER")

                print(f"[SCHEDULER] Created {new_id}: {title}")
                created += 1

        conn.commit()
        return created
    finally:
        conn.close()


def main_loop(once=False):
    os.makedirs(LOG_DIR, exist_ok=True)

    # Lock file
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                old_pid = f.read().strip()
            if old_pid and old_pid.isdigit():
                try:
                    os.kill(int(old_pid), 0)
                    print(f"[SCHEDULER] Another scheduler (PID {old_pid}) is running. Exiting.")
                    sys.exit(1)
                except OSError:
                    os.remove(LOCK_FILE)
        except (IOError, ValueError):
            os.remove(LOCK_FILE)

    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("[SCHEDULER] ERROR: Another scheduler is running. Exiting.")
        sys.exit(1)

    lock_fd.write(str(os.getpid()))
    lock_fd.flush()

    import atexit
    def _cleanup():
        try:
            lock_fd.close()
            os.remove(LOCK_FILE)
        except OSError:
            pass
    atexit.register(_cleanup)

    def _handle_shutdown(signum, frame):
        global _shutdown
        print(f"\n[SCHEDULER] Received {signal.Signals(signum).name}. Shutting down...")
        _shutdown = True

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    print("=" * 50)
    print("  DOOM SCHEDULER — Cron-style Decree Execution")
    print(f"  Database: {DB_PATH}")
    print(f"  Check interval: {CHECK_INTERVAL}s")
    print(f"  PID: {os.getpid()}")
    print("=" * 50)
    print()

    conn = get_db()
    log_chronicle(conn, "spawn", f"Scheduler daemon started (PID={os.getpid()})", "SCHEDULER")
    conn.close()

    last_heartbeat = 0

    try:
        while not _shutdown:
            try:
                created = check_scheduled_decrees()
                if created:
                    print(f"[SCHEDULER] Created {created} scheduled decree(s)")
            except Exception as e:
                print(f"[SCHEDULER] Error: {e}")

            # Heartbeat checks every 30 minutes
            if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                try:
                    run_heartbeat()
                except Exception as e:
                    print(f"[SCHEDULER] Heartbeat error: {e}")
                last_heartbeat = time.time()

            if once:
                break

            for _ in range(CHECK_INTERVAL):
                if _shutdown:
                    break
                time.sleep(1)
    finally:
        try:
            conn = get_db()
            log_chronicle(conn, "retire", "Scheduler daemon stopped", "SCHEDULER")
            conn.close()
        except Exception:
            pass
        print("[SCHEDULER] Shutdown complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DOOM Scheduler")
    parser.add_argument("--once", action="store_true", help="Check once and exit")
    args = parser.parse_args()
    main_loop(once=args.once)
