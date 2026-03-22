#!/usr/bin/env python3
"""
dm — DOOM's Command Interface

The sovereign CLI for the DOOM multi-agent orchestration framework.
All commands read from and write to ~/DOOMBOT/memory.db (SQLite).

Usage: python dm.py <command> [subcommand] [args]
"""

import argparse
import os
import secrets
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "memory.db")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    """Return a sqlite3 connection to memory.db, or exit with guidance."""
    if not os.path.exists(DB_PATH):
        print("ERROR: memory.db not found at", DB_PATH)
        print("Run init_db.py first to establish the memory core.")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def gen_id(prefix):
    """Generate a random ID like dc-a1b2 or ar-f3e4."""
    return f"{prefix}-{secrets.token_hex(2)}"


def now():
    """Always return UTC timestamp — matches SQLite datetime('now')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def current_session_id(conn):
    """Return the id of the most recent open session, or None."""
    row = conn.execute(
        "SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def log_chronicle(conn, event_type, content, agent_id=None, session_id=None):
    """Write an event to the chronicle."""
    if session_id is None:
        session_id = current_session_id(conn)
    conn.execute(
        "INSERT INTO chronicle (session_id, event_type, agent_id, content, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, event_type, agent_id, content, now()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# dm wake
# ---------------------------------------------------------------------------

def cmd_wake(args):
    conn = get_db()

    # Session number
    row = conn.execute(
        "SELECT session_number FROM sessions ORDER BY session_number DESC LIMIT 1"
    ).fetchone()
    session_num = row["session_number"] if row else 0

    # Active bots
    bots = conn.execute(
        "SELECT * FROM agents WHERE status='active'"
    ).fetchall()
    active_bot_count = len(bots)

    # Open decrees
    open_decrees = conn.execute(
        "SELECT * FROM decrees WHERE status IN ('open', 'active')"
    ).fetchall()
    open_count = len(open_decrees)

    # Active decrees (status = 'active')
    active_decrees = conn.execute(
        "SELECT id, title, status, assigned_to FROM decrees WHERE status='active'"
    ).fetchall()

    # Unblocked decrees (open, not blocked)
    all_open = conn.execute(
        "SELECT id, title, blocked_by FROM decrees WHERE status='open'"
    ).fetchall()
    fulfilled_ids = {
        r["id"]
        for r in conn.execute(
            "SELECT id FROM decrees WHERE status='fulfilled'"
        ).fetchall()
    }
    unblocked = []
    for d in all_open:
        if not d["blocked_by"]:
            unblocked.append(d)
        else:
            blockers = [b.strip() for b in d["blocked_by"].split(",") if b.strip()]
            if all(b in fulfilled_ids for b in blockers):
                unblocked.append(d)

    # Standing concerns (archives with importance = 1)
    concerns = conn.execute(
        "SELECT topic, content FROM archives WHERE importance = 1 ORDER BY created_at DESC LIMIT 5"
    ).fetchall()

    # Last session summary
    last_session = conn.execute(
        "SELECT summary FROM sessions WHERE status='closed' ORDER BY session_number DESC LIMIT 1"
    ).fetchone()
    last_summary = last_session["summary"] if last_session and last_session["summary"] else "No prior sessions."

    # Archive topics
    topics = conn.execute(
        "SELECT DISTINCT topic FROM archives ORDER BY topic"
    ).fetchall()
    topic_list = [t["topic"] for t in topics]

    # --- Output ---
    print(f"=== DOOM AWAKENS — SESSION {session_num} ===")
    print(f"Status: {active_bot_count} Doom Bots active \u00b7 {open_count} decrees open")

    print("\nActive Decrees:")
    if active_decrees:
        for d in active_decrees:
            assigned = f" [{d['assigned_to']}]" if d["assigned_to"] else ""
            print(f"  {d['id']}: {d['title']} ({d['status']}){assigned}")
    else:
        print("  None")

    print("\nUnblocked:")
    if unblocked:
        for d in unblocked:
            print(f"  {d['id']}: {d['title']}")
    else:
        print("  None")

    print("\nStanding Concerns:")
    if concerns:
        for c in concerns:
            print(f"  [{c['topic']}] {c['content'][:120]}")
    else:
        print("  None")

    print(f"\nLast Session: {last_summary}")

    print(f"\nArchives loaded: {', '.join(topic_list) if topic_list else 'None'}")
    print("================================")

    conn.close()


# ---------------------------------------------------------------------------
# dm status
# ---------------------------------------------------------------------------

def cmd_status(args):
    conn = get_db()

    # Current session
    session = conn.execute(
        "SELECT * FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
    ).fetchone()

    print("=== DOOM STATUS ===")
    if session:
        print(f"Session: {session['id']} (#{session['session_number']})")
        print(f"  Focus: {session['focus'] or 'None'}")
        print(f"  Started: {session['started_at']}")
    else:
        print("Session: No active session")

    # Agents
    agents = conn.execute(
        "SELECT * FROM agents WHERE status IN ('active', 'idle', 'blocked') ORDER BY id"
    ).fetchall()
    print(f"\nAgents ({len(agents)} online):")
    if agents:
        for a in agents:
            decree_info = f" -> {a['current_decree']}" if a["current_decree"] else ""
            ctx = f" [{a['context_pct']}% ctx]" if a["context_pct"] else ""
            print(f"  {a['id']} ({a['type']}) [{a['status']}]{decree_info}{ctx}")
    else:
        print("  None active")

    # Decree summary
    counts = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM decrees GROUP BY status"
    ).fetchall()
    print("\nDecrees:")
    if counts:
        for c in counts:
            print(f"  {c['status']}: {c['cnt']}")
    else:
        print("  No decrees issued")

    print("===================")
    conn.close()


# ---------------------------------------------------------------------------
# dm decree create
# ---------------------------------------------------------------------------

def cmd_decree_create(args):
    conn = get_db()
    decree_id = gen_id("dc")
    blocked_by = args.blocked_by if args.blocked_by else None
    priority = args.priority if args.priority else 2
    ts = now()

    conn.execute(
        "INSERT INTO decrees (id, title, description, status, priority, blocked_by, created_at, updated_at) "
        "VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
        (decree_id, args.title, args.description, priority, blocked_by, ts, ts),
    )
    conn.commit()

    log_chronicle(conn, "decree", f"Decree issued: {decree_id} — {args.title}")

    print(f"Decree {decree_id} issued: {args.title}")
    if blocked_by:
        print(f"  Blocked by: {blocked_by}")
    print(f"  Priority: {priority}")
    conn.close()


# ---------------------------------------------------------------------------
# dm decree list
# ---------------------------------------------------------------------------

def cmd_decree_list(args):
    conn = get_db()
    status_filter = args.status if args.status else None

    if status_filter and status_filter != "all":
        rows = conn.execute(
            "SELECT * FROM decrees WHERE status=? ORDER BY priority, created_at",
            (status_filter,),
        ).fetchall()
    elif status_filter == "all":
        rows = conn.execute(
            "SELECT * FROM decrees ORDER BY priority, created_at"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM decrees WHERE status IN ('open', 'active') ORDER BY priority, created_at"
        ).fetchall()

    print(f"=== DECREES ({len(rows)}) ===")
    for d in rows:
        assigned = f" [{d['assigned_to']}]" if d["assigned_to"] else ""
        blocked = f" (blocked by: {d['blocked_by']})" if d["blocked_by"] else ""
        print(f"  [{d['priority']}] {d['id']}: {d['title']} [{d['status']}]{assigned}{blocked}")
    if not rows:
        print("  No decrees match this filter.")
    print("=======================")
    conn.close()


# ---------------------------------------------------------------------------
# dm decree ready
# ---------------------------------------------------------------------------

def cmd_decree_ready(args):
    conn = get_db()

    all_open = conn.execute(
        "SELECT * FROM decrees WHERE status='open' ORDER BY priority, created_at"
    ).fetchall()
    fulfilled_ids = {
        r["id"]
        for r in conn.execute(
            "SELECT id FROM decrees WHERE status='fulfilled'"
        ).fetchall()
    }

    ready = []
    for d in all_open:
        if not d["blocked_by"]:
            ready.append(d)
        else:
            blockers = [b.strip() for b in d["blocked_by"].split(",") if b.strip()]
            if all(b in fulfilled_ids for b in blockers):
                ready.append(d)

    print(f"=== READY DECREES ({len(ready)}) ===")
    for d in ready:
        print(f"  [{d['priority']}] {d['id']}: {d['title']}")
    if not ready:
        print("  No unblocked decrees.")
    print("============================")
    conn.close()


# ---------------------------------------------------------------------------
# dm decree claim
# ---------------------------------------------------------------------------

def cmd_decree_claim(args):
    conn = get_db()
    decree_id = args.id
    bot_id = args.bot
    ts = now()

    # Verify decree exists
    decree = conn.execute("SELECT * FROM decrees WHERE id=?", (decree_id,)).fetchone()
    if not decree:
        print(f"ERROR: Decree {decree_id} not found.")
        conn.close()
        sys.exit(1)

    conn.execute(
        "UPDATE decrees SET status='active', assigned_to=?, updated_at=? WHERE id=?",
        (bot_id, ts, decree_id),
    )
    conn.execute(
        "UPDATE agents SET current_decree=?, status='active', last_active=? WHERE id=?",
        (decree_id, ts, bot_id),
    )
    conn.commit()

    log_chronicle(conn, "decree", f"{bot_id} claims decree {decree_id}: {decree['title']}", agent_id=bot_id)

    print(f"Decree {decree_id} claimed by {bot_id}.")
    conn.close()


# ---------------------------------------------------------------------------
# dm decree fulfill
# ---------------------------------------------------------------------------

def cmd_decree_fulfill(args):
    conn = get_db()
    decree_id = args.id
    notes = args.notes if args.notes else None
    ts = now()

    decree = conn.execute("SELECT * FROM decrees WHERE id=?", (decree_id,)).fetchone()
    if not decree:
        print(f"ERROR: Decree {decree_id} not found.")
        conn.close()
        sys.exit(1)

    conn.execute(
        "UPDATE decrees SET status='fulfilled', fulfilled_at=?, fulfillment_notes=?, updated_at=? WHERE id=?",
        (ts, notes, ts, decree_id),
    )
    conn.commit()

    log_chronicle(
        conn,
        "decree",
        f"Decree {decree_id} FULFILLED: {decree['title']}" + (f" — {notes}" if notes else ""),
        agent_id=decree["assigned_to"],
    )

    print(f"Decree {decree_id} fulfilled.")
    if notes:
        print(f"  Notes: {notes}")
    conn.close()


# ---------------------------------------------------------------------------
# dm bot spawn
# ---------------------------------------------------------------------------

def cmd_bot_spawn(args):
    conn = get_db()
    bot_id = args.id
    decree_id = args.decree
    ts = now()

    # Verify decree exists
    decree = conn.execute("SELECT * FROM decrees WHERE id=?", (decree_id,)).fetchone()
    if not decree:
        print(f"ERROR: Decree {decree_id} not found.")
        conn.close()
        sys.exit(1)

    # Check if agent already exists
    existing = conn.execute("SELECT * FROM agents WHERE id=?", (bot_id,)).fetchone()
    if existing:
        print(f"ERROR: Agent {bot_id} already exists (status: {existing['status']}).")
        conn.close()
        sys.exit(1)

    conn.execute(
        "INSERT INTO agents (id, type, status, current_decree, context_pct, spawned_at, last_active) "
        "VALUES (?, 'doom_bot', 'active', ?, 0, ?, ?)",
        (bot_id, decree_id, ts, ts),
    )
    conn.commit()

    log_chronicle(conn, "spawn", f"Doom Bot {bot_id} spawned for decree {decree_id}: {decree['title']}", agent_id=bot_id)

    print(f"Doom Bot {bot_id} spawned.")
    print(f"  Decree: {decree_id} — {decree['title']}")
    conn.close()


# ---------------------------------------------------------------------------
# dm bot status
# ---------------------------------------------------------------------------

def cmd_bot_status(args):
    conn = get_db()
    agents = conn.execute("SELECT * FROM agents ORDER BY status, id").fetchall()

    print(f"=== AGENTS ({len(agents)}) ===")
    for a in agents:
        decree_info = f" -> {a['current_decree']}" if a["current_decree"] else ""
        ctx = f" [{a['context_pct']}% ctx]" if a["context_pct"] else ""
        notes = f"  ({a['notes']})" if a["notes"] else ""
        print(f"  {a['id']} [{a['type']}] [{a['status']}]{decree_info}{ctx}{notes}")
    if not agents:
        print("  No agents in the system.")
    print("======================")
    conn.close()


# ---------------------------------------------------------------------------
# dm bot retire
# ---------------------------------------------------------------------------

def cmd_bot_retire(args):
    conn = get_db()
    bot_id = args.id
    ts = now()

    agent = conn.execute("SELECT * FROM agents WHERE id=?", (bot_id,)).fetchone()
    if not agent:
        print(f"ERROR: Agent {bot_id} not found.")
        conn.close()
        sys.exit(1)

    conn.execute(
        "UPDATE agents SET status='retired', current_decree=NULL, last_active=? WHERE id=?",
        (ts, bot_id),
    )
    conn.commit()

    log_chronicle(conn, "retire", f"Doom Bot {bot_id} retired.", agent_id=bot_id)

    print(f"Doom Bot {bot_id} retired.")
    conn.close()


# ---------------------------------------------------------------------------
# dm archive write
# ---------------------------------------------------------------------------

def cmd_archive_write(args):
    conn = get_db()
    archive_id = gen_id("ar")
    importance = args.importance if args.importance else 3
    source_session = args.source_session if args.source_session else current_session_id(conn)
    ts = now()

    conn.execute(
        "INSERT INTO archives (id, topic, content, source_session, importance, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (archive_id, args.topic, args.content, source_session, importance, ts, ts),
    )
    conn.commit()

    print(f"Archive {archive_id} written.")
    print(f"  Topic: {args.topic}")
    print(f"  Importance: {importance}")
    conn.close()


# ---------------------------------------------------------------------------
# dm archive recall
# ---------------------------------------------------------------------------

def cmd_archive_recall(args):
    conn = get_db()
    topic = args.topic
    rows = conn.execute(
        "SELECT * FROM archives WHERE topic LIKE ? ORDER BY importance, created_at DESC",
        (f"%{topic}%",),
    ).fetchall()

    print(f"=== ARCHIVES: '{topic}' ({len(rows)} entries) ===")
    for r in rows:
        print(f"\n  [{r['id']}] {r['topic']} (importance: {r['importance']})")
        print(f"  Source: {r['source_session'] or 'unknown'}")
        print(f"  {r['content']}")
    if not rows:
        print("  No archives match this topic.")
    print("==============================")
    conn.close()


# ---------------------------------------------------------------------------
# dm session open
# ---------------------------------------------------------------------------

def cmd_session_open(args):
    conn = get_db()
    focus = args.focus if args.focus else None

    # Guard: if there's already an open session, warn and return
    existing = conn.execute(
        "SELECT id, session_number FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
    ).fetchone()
    if existing:
        print(f"WARNING: Session {existing['session_number']} is already open ({existing['id']}). Close it first.")
        conn.close()
        return

    # Determine next session number
    row = conn.execute(
        "SELECT MAX(session_number) as max_num FROM sessions"
    ).fetchone()
    next_num = (row["max_num"] or 0) + 1
    session_id = f"session-{next_num:03d}"
    ts = now()

    conn.execute(
        "INSERT INTO sessions (id, session_number, started_at, focus, status) "
        "VALUES (?, ?, ?, ?, 'open')",
        (session_id, next_num, ts, focus),
    )
    # Clear council for fresh session
    conn.execute("DELETE FROM council")
    conn.commit()

    log_chronicle(conn, "decision", f"Session {next_num} opened." + (f" Focus: {focus}" if focus else ""), session_id=session_id)

    print(f"Session {next_num} opened ({session_id}).")
    if focus:
        print(f"  Focus: {focus}")
    conn.close()


# ---------------------------------------------------------------------------
# Git auto-push helper
# ---------------------------------------------------------------------------

REPO_DIR = SCRIPT_DIR


def git_auto_push(session_num, summary):
    """Stage all changes, commit with session summary, push to origin main.

    Returns True on full success, False if any git step failed (non-fatal).
    """
    summary_str = summary or "Session closed"
    commit_msg = f"DOOM Session {session_num}: {summary_str}"

    try:
        # 1. Stage everything (respects .gitignore — memory.db/.venv excluded)
        result = subprocess.run(
            ["git", "add", "-A"],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  [git] WARNING: git add failed: {result.stderr.strip()}")
            return False

        # 2. Check if there is anything staged to commit
        diff_check = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=REPO_DIR,
            capture_output=True,
        )

        if diff_check.returncode == 0:
            print("  [git] No changes staged — nothing to commit.")
        else:
            result = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=REPO_DIR,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(f"  [git] WARNING: git commit failed: {result.stderr.strip()}")
                return False
            print(f"  [git] Committed: {commit_msg}")

        # Git push disabled — pushing is a deliberate human action only.
        # Never auto-push. Keys were leaked once. Never again.
        return True

    except FileNotFoundError:
        msg = "git executable not found in PATH."
        print(f"  [git] WARNING: {msg}", file=sys.stderr)
        import warnings
        warnings.warn(msg, RuntimeWarning, stacklevel=2)
        return False
    except Exception as exc:
        msg = f"Unexpected error during push: {exc}"
        print(f"  [git] WARNING: {msg}", file=sys.stderr)
        import warnings
        warnings.warn(msg, RuntimeWarning, stacklevel=2)
        return False


# ---------------------------------------------------------------------------
# dm session close
# ---------------------------------------------------------------------------

def cmd_session_close(args):
    conn = get_db()
    summary = args.summary if args.summary else None
    no_push = getattr(args, "no_push", False)
    ts = now()

    session = conn.execute(
        "SELECT * FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
    ).fetchone()

    if not session:
        print("ERROR: No open session to close.")
        conn.close()
        sys.exit(1)

    conn.execute(
        "UPDATE sessions SET status='closed', ended_at=?, summary=? WHERE id=?",
        (ts, summary, session["id"]),
    )
    conn.commit()

    log_chronicle(
        conn,
        "decision",
        f"Session {session['session_number']} closed." + (f" Summary: {summary}" if summary else ""),
        session_id=session["id"],
    )

    print(f"Session {session['session_number']} closed ({session['id']}).")
    if summary:
        print(f"  Summary: {summary}")
    conn.close()

    # Auto-push to GitHub unless explicitly skipped
    if no_push:
        print("  [git] Auto-push skipped (--no-push).")
    else:
        print("  [git] Pushing session to GitHub...")
        git_auto_push(session["session_number"], summary)


# ---------------------------------------------------------------------------
# dm chronicle log
# ---------------------------------------------------------------------------

def cmd_chronicle_log(args):
    conn = get_db()
    agent_id = args.agent_id if args.agent_id else None

    log_chronicle(conn, args.event_type, args.content, agent_id=agent_id)

    print(f"Chronicle entry logged: [{args.event_type}] {args.content}")
    conn.close()


# ---------------------------------------------------------------------------
# dm chronicle show
# ---------------------------------------------------------------------------

def cmd_chronicle_show(args):
    conn = get_db()
    limit = args.limit if args.limit else 20

    rows = conn.execute(
        "SELECT * FROM chronicle ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    ).fetchall()

    print(f"=== CHRONICLE (last {limit}) ===")
    for r in reversed(rows):
        agent = f" [{r['agent_id']}]" if r["agent_id"] else ""
        session = f" ({r['session_id']})" if r["session_id"] else ""
        print(f"  {r['timestamp']} [{r['event_type']}]{agent}{session} {r['content']}")
    if not rows:
        print("  The chronicle is empty.")
    print("============================")
    conn.close()


# ---------------------------------------------------------------------------
# dm siege — Siege Engine launcher
# ---------------------------------------------------------------------------

def cmd_siege(args):
    """Launch a Siege Engine autonomous loop."""
    import subprocess as _sp

    objective = args.objective or "Execute PRD"
    project_path = args.project_path
    prd = args.prd
    tag = args.tag
    max_iter = args.max_iterations
    no_commit = args.no_commit
    background = args.background

    if not args.objective and not args.prd:
        print("ERROR: dm siege requires --objective or --prd")
        print("  Example: dm siege run --objective 'Build a REST API for user management' --project-path ~/Desktop/my-api/")
        print("  Example: dm siege run --prd ~/Desktop/prd.json --project-path ~/Desktop/my-project/")
        sys.exit(1)

    cmd = [
        sys.executable, os.path.join(SCRIPT_DIR, "worker.py"),
        "--siege",
    ]
    if args.objective:
        cmd += ["--objective", args.objective]
    if prd:
        cmd += ["--prd", prd]
    if project_path:
        cmd += ["--project-path", project_path]
    if max_iter:
        cmd += ["--max-iterations", str(max_iter)]
    if tag:
        cmd += ["--tag", tag]
    if no_commit:
        cmd += ["--no-commit"]

    if background:
        # Run in background with nohup, log to logs/siege-{tag}.log
        log_dir = os.path.join(SCRIPT_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_tag = tag or "default"
        log_file = os.path.join(log_dir, f"siege-{log_tag}.log")
        with open(log_file, "w") as lf:
            proc = _sp.Popen(
                cmd, stdout=lf, stderr=_sp.STDOUT,
                stdin=_sp.DEVNULL, start_new_session=True,
                env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
            )
        print(f"=== SIEGE ENGINE LAUNCHED (background) ===")
        print(f"  PID:       {proc.pid}")
        print(f"  Objective: {objective[:80]}")
        if project_path:
            print(f"  Project:   {project_path}")
        print(f"  Log:       {log_file}")
        print(f"  Tag:       {tag or 'auto'}")
        print(f"========================================")
        print(f"\nMonitor: tail -f {log_file}")

        # Log to chronicle
        conn = get_db()
        log_chronicle(conn, "decision",
            f"[SIEGE] Background loop launched — PID={proc.pid}, tag={tag or 'auto'}, objective: {objective[:100]}")
        conn.close()
    else:
        # Run in foreground
        print(f"=== SIEGE ENGINE — FOREGROUND ===")
        print(f"Objective: {objective[:80]}")
        print(f"Press Ctrl+C to stop gracefully.")
        print()
        os.execv(sys.executable, cmd)


def cmd_siege_status(args):
    """Show status of Siege Engine decrees."""
    conn = get_db()

    # Find all Siege decrees
    rows = conn.execute(
        "SELECT id, title, status, priority, assigned_to, created_at, fulfilled_at "
        "FROM decrees WHERE title LIKE '[SIEGE]%' ORDER BY created_at ASC"
    ).fetchall()

    if not rows:
        print("No Siege Engine decrees found.")
        conn.close()
        return

    # Group by tag (extracted from description)
    tags = {}
    for r in rows:
        desc_row = conn.execute("SELECT description FROM decrees WHERE id=?", (r["id"],)).fetchone()
        desc = desc_row["description"] if desc_row else ""
        tag = "unknown"
        for line in desc.split("\n"):
            if line.startswith("TAG:"):
                tag = line.replace("TAG:", "").strip()
                break
        tags.setdefault(tag, []).append(r)

    for tag, decrees in tags.items():
        total = len(decrees)
        fulfilled = sum(1 for d in decrees if d["status"] in ("fulfilled", "sealed"))
        blocked = sum(1 for d in decrees if d["status"] == "blocked")
        active = sum(1 for d in decrees if d["status"] == "active")
        pending = sum(1 for d in decrees if d["status"] == "open")

        print(f"\n=== SIEGE TAG: {tag} ===")
        print(f"  Total: {total} | Fulfilled: {fulfilled} | Active: {active} | Pending: {pending} | Blocked: {blocked}")
        for d in decrees:
            status_icon = {"open": "○", "active": "►", "fulfilled": "✓", "sealed": "✓", "blocked": "✗"}.get(d["status"], "?")
            title = d["title"].replace("[SIEGE] ", "")
            print(f"  {status_icon} {d['id']}: {title} [{d['status']}]")
    print()
    conn.close()


# ---------------------------------------------------------------------------
# Stress test — self-diagnostic suite
# ---------------------------------------------------------------------------

def cmd_stress(args):
    """Run DOOM stress test, optionally auto-fix via Siege Engine."""
    import subprocess as _sp

    subcmd = getattr(args, "subcommand", "run") or "run"
    stress_script = os.path.join(SCRIPT_DIR, "stress_test.py")

    if subcmd == "siege":
        # Launch a Siege Engine that runs stress test and fixes failures
        objective = (
            "Run the DOOM stress test (python stress_test.py --json) in the DOOMBOT directory. "
            "Parse the JSON output. For each failure, diagnose the root cause by reading the "
            "relevant source files (server.py, worker.py, dm.py, doom-ui.html, doom-mobile.html). "
            "Fix each failure. After fixing, re-run the stress test to verify. "
            "Continue until all tests pass or you've exhausted your iteration budget. "
            "Do NOT create test decrees, test bots, or spawn Claude sessions for testing. "
            "Only modify code to fix real failures."
        )
        cmd = [
            sys.executable, os.path.join(SCRIPT_DIR, "worker.py"),
            "--siege",
            "--objective", objective,
            "--project-path", SCRIPT_DIR,
            "--tag", "self-heal",
            "--no-commit",
        ]
        if getattr(args, "background", False):
            log_dir = os.path.join(SCRIPT_DIR, "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, "siege-self-heal.log")
            with open(log_file, "w") as lf:
                proc = _sp.Popen(
                    cmd, stdout=lf, stderr=_sp.STDOUT,
                    stdin=_sp.DEVNULL, start_new_session=True,
                    env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
                )
            print(f"=== SIEGE SELF-HEAL LAUNCHED ===")
            print(f"  PID: {proc.pid}")
            print(f"  Log: {log_file}")
            print(f"  Monitor: tail -f {log_file}")
        else:
            print("=== SIEGE SELF-HEAL — FOREGROUND ===")
            print("Running stress test → diagnose → fix → re-test loop")
            print("Press Ctrl+C to stop.\n")
            os.execv(sys.executable, cmd)
        return

    # Regular stress test run
    cmd = [sys.executable, stress_script]
    if getattr(args, "category", None):
        cmd += ["--category", args.category]
    if getattr(args, "json", False):
        cmd += ["--json"]
    if subcmd == "fix":
        cmd += ["--fix"]

    result = _sp.run(cmd, cwd=SCRIPT_DIR)
    sys.exit(result.returncode)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="dm",
        description="DOOM's Command Interface — sovereign multi-agent orchestration.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Top-level commands")

    # --- wake ---
    subparsers.add_parser("wake", help="Awaken DOOM — load context and display status")

    # --- status ---
    subparsers.add_parser("status", help="Display current DOOM status")

    # --- decree ---
    decree_parser = subparsers.add_parser("decree", help="Manage decrees")
    decree_sub = decree_parser.add_subparsers(dest="subcommand", help="Decree commands")

    # decree create
    dc_create = decree_sub.add_parser("create", help="Issue a new decree")
    dc_create.add_argument("--title", required=True, help="Decree title")
    dc_create.add_argument("--description", default=None, help="Decree description")
    dc_create.add_argument("--priority", type=int, default=2, help="Priority (1=urgent, 2=high, 3=standard)")
    dc_create.add_argument("--blocked-by", default=None, help="Comma-separated decree IDs that block this")

    # decree list
    dc_list = decree_sub.add_parser("list", help="List decrees")
    dc_list.add_argument("--status", default=None, help="Filter by status (open, active, fulfilled, sealed, all)")

    # decree ready
    decree_sub.add_parser("ready", help="Show unblocked decrees ready for execution")

    # decree claim
    dc_claim = decree_sub.add_parser("claim", help="Assign a decree to an agent")
    dc_claim.add_argument("id", help="Decree ID")
    dc_claim.add_argument("--bot", required=True, help="Bot ID to assign")

    # decree fulfill
    dc_fulfill = decree_sub.add_parser("fulfill", help="Mark a decree as fulfilled")
    dc_fulfill.add_argument("id", help="Decree ID")
    dc_fulfill.add_argument("--notes", default=None, help="Fulfillment notes")

    # --- bot ---
    bot_parser = subparsers.add_parser("bot", help="Manage Doom Bots")
    bot_sub = bot_parser.add_subparsers(dest="subcommand", help="Bot commands")

    # bot spawn
    bt_spawn = bot_sub.add_parser("spawn", help="Spawn a new Doom Bot")
    bt_spawn.add_argument("id", help="Bot ID (e.g. DOOM-BOT-I)")
    bt_spawn.add_argument("--decree", required=True, help="Decree ID to assign")

    # bot status
    bot_sub.add_parser("status", help="Show all agents")

    # bot retire
    bt_retire = bot_sub.add_parser("retire", help="Retire a Doom Bot")
    bt_retire.add_argument("id", help="Bot ID")

    # --- archive ---
    archive_parser = subparsers.add_parser("archive", help="Manage the Archives")
    archive_sub = archive_parser.add_subparsers(dest="subcommand", help="Archive commands")

    # archive write
    ar_write = archive_sub.add_parser("write", help="Write to the Archives")
    ar_write.add_argument("--topic", required=True, help="Archive topic")
    ar_write.add_argument("--content", required=True, help="Archive content")
    ar_write.add_argument("--importance", type=int, default=3, help="Importance (1=critical, 2=high, 3=standard)")
    ar_write.add_argument("--source-session", default=None, help="Source session ID")

    # archive recall
    ar_recall = archive_sub.add_parser("recall", help="Query archives by topic")
    ar_recall.add_argument("topic", help="Topic to search for (partial match)")

    # --- session ---
    session_parser = subparsers.add_parser("session", help="Manage sessions")
    session_sub = session_parser.add_subparsers(dest="subcommand", help="Session commands")

    # session open
    ss_open = session_sub.add_parser("open", help="Open a new session")
    ss_open.add_argument("--focus", default=None, help="Session focus")

    # session close
    ss_close = session_sub.add_parser("close", help="Close the current session and push to GitHub")
    ss_close.add_argument("--summary", default=None, help="Session summary")
    ss_close.add_argument(
        "--no-push",
        action="store_true",
        default=False,
        help="Skip the automatic git commit and push to GitHub",
    )

    # --- chronicle ---
    chronicle_parser = subparsers.add_parser("chronicle", help="The sacred chronicle")
    chronicle_sub = chronicle_parser.add_subparsers(dest="subcommand", help="Chronicle commands")

    # chronicle log
    ch_log = chronicle_sub.add_parser("log", help="Log an event")
    ch_log.add_argument("--event-type", required=True, help="Event type (decree, spawn, retire, decision, discovery, warning)")
    ch_log.add_argument("--content", required=True, help="Event content")
    ch_log.add_argument("--agent-id", default=None, help="Agent ID")

    # chronicle show
    ch_show = chronicle_sub.add_parser("show", help="Show recent chronicle entries")
    ch_show.add_argument("--limit", type=int, default=20, help="Number of entries to show")

    # --- siege ---
    siege_parser = subparsers.add_parser("siege", help="Siege Engine — autonomous iteration engine")
    siege_sub = siege_parser.add_subparsers(dest="subcommand", help="Siege commands")

    # siege run
    rl_run = siege_sub.add_parser("run", help="Launch a Siege Engine autonomous loop")
    rl_run.add_argument("--objective", type=str, help="Objective text to decompose and execute")
    rl_run.add_argument("--prd", type=str, help="Path to PRD file (JSON array of stories, or plain text)")
    rl_run.add_argument("--project-path", type=str, help="Project directory (created if needed)")
    rl_run.add_argument("--max-iterations", type=int, help="Max iterations (default: 50)")
    rl_run.add_argument("--tag", type=str, help="Tag for grouping decrees")
    rl_run.add_argument("--no-commit", action="store_true", help="Skip auto-commit after each story")
    rl_run.add_argument("--background", action="store_true", help="Run in background (daemon mode)")

    # siege status
    siege_sub.add_parser("status", help="Show Siege Engine decree status")

    # stress — self-diagnostic suite
    stress_parser = subparsers.add_parser("stress", help="Run DOOM self-diagnostic stress test")
    stress_parser.add_argument("subcommand", nargs="?", default="run",
                               choices=["run", "fix", "siege"],
                               help="run=execute tests, fix=show fixes, siege=auto-fix via Siege Engine")
    stress_parser.add_argument("--category", "-c", type=str, help="Test only this category (api,db,daemons,ui,council,integrity)")
    stress_parser.add_argument("--json", action="store_true", help="Output JSON report")

    return parser


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

DISPATCH = {
    ("wake", None): cmd_wake,
    ("status", None): cmd_status,
    ("decree", "create"): cmd_decree_create,
    ("decree", "list"): cmd_decree_list,
    ("decree", "ready"): cmd_decree_ready,
    ("decree", "claim"): cmd_decree_claim,
    ("decree", "fulfill"): cmd_decree_fulfill,
    ("bot", "spawn"): cmd_bot_spawn,
    ("bot", "status"): cmd_bot_status,
    ("bot", "retire"): cmd_bot_retire,
    ("archive", "write"): cmd_archive_write,
    ("archive", "recall"): cmd_archive_recall,
    ("session", "open"): cmd_session_open,
    ("session", "close"): cmd_session_close,
    ("chronicle", "log"): cmd_chronicle_log,
    ("chronicle", "show"): cmd_chronicle_show,
    ("siege", "run"): cmd_siege,
    ("siege", "status"): cmd_siege_status,
    ("stress", None): cmd_stress,
    ("stress", "run"): cmd_stress,
    ("stress", "fix"): cmd_stress,
    ("stress", "siege"): cmd_stress,
}


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    subcommand = getattr(args, "subcommand", None)
    key = (args.command, subcommand)

    # For top-level commands (wake, status), subcommand is None
    if key not in DISPATCH:
        # Maybe the command itself has no subcommand
        key_no_sub = (args.command, None)
        if key_no_sub in DISPATCH:
            key = key_no_sub
        else:
            print(f"Unknown command: dm {args.command}" + (f" {subcommand}" if subcommand else ""))
            parser.print_help()
            sys.exit(1)

    handler = DISPATCH[key]
    handler(args)


if __name__ == "__main__":
    main()
