#!/usr/bin/env python3
"""
DOOM Introspect — Self-Improvement Daemon

Runs every 2 hours. Scans DOOM's own failures, detects patterns,
and auto-generates fix decrees. DOOM examining itself.

Usage:
    python introspect.py              # Run the daemon loop
    python introspect.py --once       # Run one analysis and exit
    python introspect.py --interval 3600  # Custom interval (seconds)
"""

import argparse
import fcntl
import json
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

DOOMBOT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DOOMBOT_DIR, "memory.db")
LOG_DIR = os.path.join(DOOMBOT_DIR, "logs")
LOCK_FILE = os.path.join(LOG_DIR, "introspect.lock")
CHECK_INTERVAL = 7200  # 2 hours

MAX_AUTO_DECREES = 3  # Cap auto-generated fix decrees per cycle

_claude_candidates = [
    os.path.expanduser("~/.local/bin/claude"),
    os.path.expanduser("~/.claude/local/claude"),
    "/usr/local/bin/claude",
]
CLAUDE_PATH = next((p for p in _claude_candidates if os.path.isfile(p)), _claude_candidates[0])

_shutdown = False


def now():
    """UTC timestamp matching SQLite datetime('now')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    if not os.path.exists(DB_PATH):
        print(f"[INTROSPECT] ERROR: {DB_PATH} not found")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def log_chronicle(conn, event_type, content, agent_id="INTROSPECT"):
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


def post_to_council(conn, content, role="doom"):
    """Log to chronicle only. Council is reserved for human <-> DOOM conversation."""
    log_chronicle(conn, "decision", content, "INTROSPECT")


# ---------------------------------------------------------------------------
# SCAN: Gather failure data
# ---------------------------------------------------------------------------

def scan_failures(conn):
    """Scan chronicle and decrees for failures in the last 24 hours."""
    # Warnings and errors from chronicle
    warnings = conn.execute(
        "SELECT content, timestamp, agent_id FROM chronicle "
        "WHERE event_type IN ('warning', 'error') "
        "AND datetime(timestamp) > datetime('now', '-24 hours') "
        "ORDER BY timestamp DESC LIMIT 50"
    ).fetchall()

    # Blocked decrees (with error info)
    blocked = conn.execute(
        "SELECT id, title, description, fulfillment_notes, assigned_to FROM decrees "
        "WHERE status = 'blocked'"
    ).fetchall()

    # Decrees that needed multiple fix passes
    hard_decrees = conn.execute(
        "SELECT id, title, fulfillment_notes FROM decrees "
        "WHERE status IN ('fulfilled', 'sealed') "
        "AND fulfillment_notes LIKE '%fix pass%' "
        "AND datetime(fulfilled_at) > datetime('now', '-48 hours')"
    ).fetchall()

    # Recent decree failures (fulfilled but with errors noted)
    recent_errors = conn.execute(
        "SELECT content FROM chronicle "
        "WHERE event_type = 'warning' "
        "AND content LIKE '%failed%' "
        "AND datetime(timestamp) > datetime('now', '-24 hours')"
    ).fetchall()

    return {
        "warnings": [dict(w) for w in warnings],
        "blocked": [dict(b) for b in blocked],
        "hard_decrees": [dict(h) for h in hard_decrees],
        "recent_errors": [dict(e) for e in recent_errors],
    }


# ---------------------------------------------------------------------------
# DETECT: Find patterns in failures
# ---------------------------------------------------------------------------

def detect_patterns(failures):
    """Identify recurring patterns in failure data."""
    patterns = []

    # Group blocked decrees by error type
    error_types = {}
    for d in failures["blocked"]:
        notes = d.get("fulfillment_notes") or ""
        if "ImportError" in notes:
            error_types.setdefault("ImportError", []).append(d)
        elif "SyntaxError" in notes:
            error_types.setdefault("SyntaxError", []).append(d)
        elif "timeout" in notes.lower() or "Timed out" in notes:
            error_types.setdefault("Timeout", []).append(d)
        elif "ModuleNotFoundError" in notes:
            error_types.setdefault("MissingModule", []).append(d)
        elif notes:
            error_types.setdefault("Other", []).append(d)

    for etype, decrees in error_types.items():
        if len(decrees) >= 1:
            examples = [f"{d['id']}: {d['title']}" for d in decrees[:3]]
            sample_notes = (decrees[0].get("fulfillment_notes") or "")[:300]
            patterns.append({
                "type": etype,
                "frequency": len(decrees),
                "examples": examples,
                "sample_error": sample_notes,
            })

    # Check for recurring warning patterns
    warning_texts = [w["content"] for w in failures["warnings"]]
    if len(warning_texts) > 5:
        # Simple frequency count of warning prefixes
        prefix_counts = {}
        for w in warning_texts:
            prefix = w[:50]
            prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
        for prefix, count in prefix_counts.items():
            if count >= 3:
                patterns.append({
                    "type": "RecurringWarning",
                    "frequency": count,
                    "examples": [prefix],
                    "sample_error": f"Warning repeated {count} times in 24h: {prefix}",
                })

    # Hard decrees pattern
    if failures["hard_decrees"]:
        patterns.append({
            "type": "HardDecrees",
            "frequency": len(failures["hard_decrees"]),
            "examples": [f"{d['id']}: {d['title']}" for d in failures["hard_decrees"][:3]],
            "sample_error": "These decrees needed multiple fix passes — indicating fragile initial builds",
        })

    return patterns


# ---------------------------------------------------------------------------
# FIX: Generate improvement decrees
# ---------------------------------------------------------------------------

def generate_fix_decrees(conn, patterns):
    """Use Claude to generate fix decrees for detected patterns."""
    if not patterns:
        return []

    # Check for existing auto-fix decrees to avoid duplicates (check ALL statuses)
    existing = conn.execute(
        "SELECT title FROM decrees WHERE description LIKE '%[AUTO-FIX]%'"
    ).fetchall()
    existing_titles = {r["title"].lower() for r in existing}

    # Also check non-auto-fix decrees with similar keywords
    all_recent = conn.execute(
        "SELECT title FROM decrees WHERE datetime(created_at) > datetime('now', '-7 days')"
    ).fetchall()
    recent_keywords = set()
    for r in all_recent:
        for word in r["title"].lower().split():
            if len(word) > 4:
                recent_keywords.add(word)

    # Auto-decree generation DISABLED — introspect logs findings to chronicle only.
    # It was generating junk decrees (e.g. "Build a to-do list Flask app") that nobody asked for.
    print(f"[INTROSPECT] Found {len(patterns)} patterns. Logging to chronicle only (auto-decree disabled).")
    for p in patterns[:5]:
        log_chronicle(conn, "discovery", f"[INTROSPECT] Pattern: {p['type']} ({p['frequency']}x) — {', '.join(p['examples'][:2])}", "INTROSPECT")
    conn.commit()
    return []

    # Hard cap: if we've already created 10+ auto-fix decrees total, stop
    total_autofix = conn.execute("SELECT count(*) as c FROM decrees WHERE description LIKE '%[AUTO-FIX]%'").fetchone()["c"]
    if total_autofix >= 10:
        print(f"[INTROSPECT] Auto-fix cap reached ({total_autofix} total). Skipping generation.")
        return []

    created = []

    for pattern in patterns[:MAX_AUTO_DECREES]:
        # Skip self-referential patterns (auto-fix decrees that failed)
        if any("[AUTO-FIX]" in ex for ex in pattern["examples"]):
            continue

        prompt = f"""You are DOOM's self-improvement engine. Given this failure pattern, generate ONE fix decree.
Return ONLY valid JSON: {{"title": "short title under 80 chars", "description": "clear fix instructions under 300 chars", "priority": 1-3}}

CRITICAL: The fix must be buildable as a STANDALONE script inside a project folder.
It must NOT modify core DOOM files (server.py, worker.py, watchtower.py, dm.py, start.sh).
Instead, create helper scripts, config files, or wrapper utilities that work alongside the framework.

Pattern type: {pattern['type']}
Frequency: {pattern['frequency']} occurrences
Examples: {', '.join(pattern['examples'][:3])}
Error sample: {pattern['sample_error'][:300]}

The fix decree should address the ROOT CAUSE. Do not retry — fix the underlying issue.
If it's a missing dependency, the fix should install it.
If it's a code error, the fix should identify and correct the pattern."""

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        try:
            result = subprocess.run(
                [CLAUDE_PATH, "-p", "--model", "haiku",
                 "--dangerously-skip-permissions", prompt],
                capture_output=True, text=True, timeout=30, env=env,
            )
            if result.returncode != 0:
                continue

            raw = result.stdout.strip()
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if not json_match:
                continue

            fix = json.loads(json_match.group())
            title = str(fix.get("title", ""))[:80]
            desc = str(fix.get("description", ""))[:300]
            priority = int(fix.get("priority", 2))

            if not title:
                continue

            # Duplicate check
            if title.lower() in existing_titles:
                print(f"[INTROSPECT] Skipping duplicate: {title}")
                continue

            decree_id = f"dc-{secrets.token_hex(2)}"
            ts = now()
            conn.execute(
                "INSERT INTO decrees (id, title, description, status, priority, created_at, updated_at) "
                "VALUES (?, ?, ?, 'open', ?, ?, ?)",
                (decree_id, title, f"[AUTO-FIX] {desc}", priority, ts, ts)
            )
            created.append({"id": decree_id, "title": title, "pattern": pattern["type"]})
            existing_titles.add(title.lower())
            print(f"[INTROSPECT] Created fix decree {decree_id}: {title}")

        except subprocess.TimeoutExpired:
            continue
        except (json.JSONDecodeError, Exception) as e:
            print(f"[INTROSPECT] Fix generation error: {e}")
            continue

    if created:
        conn.commit()
    return created


# ---------------------------------------------------------------------------
# SYNTHESIZE: Chronicle to Archives
# ---------------------------------------------------------------------------

def synthesize_memory(conn):
    """Synthesize recent chronicle entries into curated archive insights."""
    recent = conn.execute(
        "SELECT event_type, agent_id, content, timestamp FROM chronicle ORDER BY timestamp DESC LIMIT 200"
    ).fetchall()
    if len(recent) < 20:
        return  # Not enough data to synthesize

    events_text = "\n".join([f"[{r['event_type']}] {r['agent_id']}: {r['content'][:100]}" for r in recent[:100]])

    existing = conn.execute("SELECT topic, content FROM archives ORDER BY updated_at DESC LIMIT 10").fetchall()
    existing_text = "\n".join([f"[{a['topic']}] {a['content'][:100]}" for a in existing])

    prompt = (
        f"You are DOOM's memory synthesis engine. Analyze these recent events and extract ONE new pattern or insight "
        f"that is NOT already in the existing archives.\n\n"
        f"RECENT EVENTS:\n{events_text}\n\n"
        f"EXISTING ARCHIVES (do not duplicate):\n{existing_text}\n\n"
        f"Return ONLY valid JSON: {{\"topic\": \"one-word\", \"content\": \"the synthesized insight under 200 chars\", \"importance\": 1-3}}\n"
        f"If nothing new is worth archiving, return {{\"topic\": \"none\"}}"
    )

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        result = subprocess.run(
            [CLAUDE_PATH, "-p", "--model", "haiku", "--no-session-persistence", prompt],
            capture_output=True, text=True, timeout=20, env=env
        )
        if result.returncode == 0:
            match = re.search(r'\{.*\}', result.stdout, re.DOTALL)
            if match:
                data = json.loads(match.group())
                if data.get("topic", "none") != "none":
                    ar_id = f"ar-{secrets.token_hex(2)}"
                    conn.execute(
                        "INSERT INTO archives (id, topic, content, source_session, importance, created_at) "
                        "VALUES (?, ?, ?, 'synthesis', ?, ?)",
                        (ar_id, data["topic"], data["content"][:300], data.get("importance", 3),
                         datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
                    )
                    conn.commit()
                    print(f"[INTROSPECT] Synthesized archive: [{data['topic']}] {data['content'][:60]}")
    except Exception as e:
        print(f"[INTROSPECT] Synthesis failed: {e}")


# ---------------------------------------------------------------------------
# MAIN CYCLE
# ---------------------------------------------------------------------------

def run_introspection_cycle():
    """Execute one full introspection cycle."""
    ts_start = time.time()
    print(f"\n{'='*50}")
    print(f"  DOOM INTROSPECT — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    conn = get_db()

    # 1. Scan for failures
    print("\n  [1/4] Scanning failures...")
    failures = scan_failures(conn)
    print(f"    Warnings: {len(failures['warnings'])}")
    print(f"    Blocked decrees: {len(failures['blocked'])}")
    print(f"    Hard decrees (multi-fix): {len(failures['hard_decrees'])}")

    # 2. Detect patterns
    print("\n  [2/4] Detecting patterns...")
    patterns = detect_patterns(failures)
    if patterns:
        for p in patterns:
            print(f"    {p['type']}: {p['frequency']} occurrences")
    else:
        print("    No patterns detected")

    # 3. Generate fix decrees
    print("\n  [3/4] Generating fixes...")
    if patterns:
        fixes = generate_fix_decrees(conn, patterns)
        if fixes:
            print(f"    Created {len(fixes)} fix decree(s)")
            # Post summary to council
            fix_summary = "\n".join(f"  - {f['id']}: {f['title']} (pattern: {f['pattern']})" for f in fixes)
            post_to_council(conn,
                f"SELF-IMPROVEMENT ANALYSIS:\n"
                f"Scanned {len(failures['warnings'])} warnings, {len(failures['blocked'])} blocked decrees.\n"
                f"Detected {len(patterns)} pattern(s). Created {len(fixes)} fix decree(s):\n{fix_summary}",
                role="doom"
            )
        else:
            print("    No fixes needed")
    else:
        print("    No patterns to fix")

    # 4. Synthesize memory — chronicle patterns to archives
    print("\n  [4/4] Synthesizing memory...")
    try:
        synthesize_memory(conn)
    except Exception as e:
        print(f"    Synthesis error: {e}")

    # Log to chronicle
    elapsed = time.time() - ts_start
    log_chronicle(conn, "decision",
        f"Introspection cycle ({elapsed:.1f}s): {len(failures['warnings'])} warnings, "
        f"{len(failures['blocked'])} blocked, {len(patterns)} patterns, "
        f"{len(fixes) if patterns else 0} fixes generated",
        "INTROSPECT"
    )

    conn.close()
    print(f"\n  Introspection complete in {elapsed:.1f}s")
    print(f"{'='*50}\n")


# ---------------------------------------------------------------------------
# DAEMON LOOP
# ---------------------------------------------------------------------------

def main_loop(once=False, interval=CHECK_INTERVAL):
    os.makedirs(LOG_DIR, exist_ok=True)

    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("[INTROSPECT] ERROR: Another introspect is already running. Exiting.")
        sys.exit(1)

    lock_fd.write(str(os.getpid()))
    lock_fd.flush()

    def _handle_shutdown(signum, frame):
        global _shutdown
        print(f"\n[INTROSPECT] Received {signal.Signals(signum).name}. Shutting down...")
        _shutdown = True

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    print("=" * 50)
    print("  DOOM INTROSPECT — Self-Improvement Daemon")
    print(f"  Database: {DB_PATH}")
    print(f"  Interval: {interval}s ({interval // 3600}h {(interval % 3600) // 60}m)")
    print(f"  PID: {os.getpid()}")
    print("=" * 50)
    print()

    conn = get_db()
    log_chronicle(conn, "spawn", f"Introspect daemon started (interval={interval}s, PID={os.getpid()})", "INTROSPECT")
    conn.close()

    try:
        while not _shutdown:
            try:
                run_introspection_cycle()
            except Exception as e:
                print(f"[INTROSPECT] Cycle error: {e}")
                import traceback
                traceback.print_exc()

            if once:
                break

            for _ in range(interval):
                if _shutdown:
                    break
                time.sleep(1)
    finally:
        try:
            conn = get_db()
            log_chronicle(conn, "retire", "Introspect daemon stopped", "INTROSPECT")
            conn.close()
        except Exception:
            pass

        try:
            lock_fd.close()
            os.remove(LOCK_FILE)
        except OSError:
            pass

        print("[INTROSPECT] Shutdown complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DOOM Introspect — Self-Improvement Daemon")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--interval", type=int, default=CHECK_INTERVAL,
                        help=f"Interval in seconds (default: {CHECK_INTERVAL})")
    args = parser.parse_args()

    main_loop(once=args.once, interval=args.interval)
