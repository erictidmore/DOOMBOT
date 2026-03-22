#!/usr/bin/env python3
"""
DOOM Worker — Background Decree Executor

Watches memory.db for open decrees and executes them autonomously
via the claude CLI. Each decree spawns a Doom Bot, runs to completion,
and writes results back to the database.

Usage:
    python worker.py              # Run the worker loop
    python worker.py --once       # Execute one decree and exit
    python worker.py --dry-run    # Show what would execute without running

Start alongside server.py in a separate terminal:
    source .venv/bin/activate
    python worker.py
"""

import argparse
import fcntl
import json
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from datetime import datetime, timezone


DOOMBOT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DOOMBOT_DIR, "memory.db")
POLL_INTERVAL = 5  # seconds between checks
MAX_CONCURRENT_BOTS = 3
LOCK_FILE = os.path.join(DOOMBOT_DIR, "logs", "worker.lock")
# Claude CLI: check common locations
_claude_candidates = [
    os.path.expanduser("~/.local/bin/claude"),
    os.path.expanduser("~/.claude/local/claude"),
    "/usr/local/bin/claude",
]
CLAUDE_PATH = next((p for p in _claude_candidates if os.path.isfile(p)), _claude_candidates[0])

# Voice reports via server API
VOICE_API = "http://127.0.0.1:5050/api/voice/speak"

def _bot_name_to_speech(text):
    """Convert DOOM-BOT-LXXXVIII to Doom Bot 88 for TTS."""
    import re
    def _roman_to_int(s):
        vals = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}
        total = 0
        for i, c in enumerate(s):
            if i + 1 < len(s) and vals.get(c, 0) < vals.get(s[i+1], 0):
                total -= vals.get(c, 0)
            else:
                total += vals.get(c, 0)
        return total
    def _replace(m):
        return f"Doom Bot {_roman_to_int(m.group(1))}"
    return re.sub(r'DOOM-BOT-([IVXLCDM]+)', _replace, text)

def voice_report(text, event_type="worker"):
    """Send a voice announcement via the DOOM server. Non-blocking."""
    def _send():
        try:
            speech = _bot_name_to_speech(text)
            data = json.dumps({"text": speech, "event_type": event_type}).encode()
            req = Request(VOICE_API, data=data, headers={"Content-Type": "application/json"})
            urlopen(req, timeout=5)
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()

# Graceful shutdown flag
_shutdown_requested = False

# Track all spawned subprocess PIDs for cleanup on shutdown
_active_processes = set()  # type: set[int]
_active_processes_lock = threading.Lock()

# Lock for bot naming — prevents concurrent bots getting the same number
import threading  # noqa: E811 — re-import for clarity at module level
_bot_name_lock = threading.Lock()

ROMAN_NUMERALS = [
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
    (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
]


def to_roman(n):
    result = ""
    for value, numeral in ROMAN_NUMERALS:
        while n >= value:
            result += numeral
            n -= value
    return result


def now():
    """Always return UTC timestamp — matches SQLite datetime('now')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def gen_id(prefix):
    return f"{prefix}-{secrets.token_hex(2)}"


def get_db_connection(db_path, retries=3, delay=1):
    """Connect to SQLite with retry logic for OperationalError (e.g. locked DB).
    Retries up to `retries` times with `delay` seconds between attempts."""
    for attempt in range(retries):
        try:
            conn = sqlite3.connect(db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            return conn
        except sqlite3.OperationalError as e:
            print(f"[WORKER] DB connect attempt {attempt + 1}/{retries} failed: {e}")
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise


class _RetryConnection:
    """Wrapper around sqlite3.Connection that auto-retries write operations
    (INSERT/UPDATE/DELETE/CREATE/ALTER/DROP) and commits on
    sqlite3.OperationalError (database locked). Retry: 3 attempts, 1s delay."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, parameters=None):
        is_write = sql.lstrip().upper().startswith(
            ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP')
        )
        _args = (sql,) if parameters is None else (sql, parameters)
        if not is_write:
            return self._conn.execute(*_args)
        for attempt in range(3):
            try:
                return self._conn.execute(*_args)
            except sqlite3.OperationalError as e:
                if attempt < 2:
                    print(f"[WORKER] DB write retry {attempt + 1}/3: {e}")
                    time.sleep(1)
                else:
                    raise

    def commit(self):
        for attempt in range(3):
            try:
                self._conn.commit()
                return
            except sqlite3.OperationalError as e:
                if attempt < 2:
                    print(f"[WORKER] DB commit retry {attempt + 1}/3: {e}")
                    time.sleep(1)
                else:
                    raise

    def close(self):
        self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._conn.close()


def get_db():
    if not os.path.exists(DB_PATH):
        print(f"[WORKER] ERROR: {DB_PATH} not found")
        sys.exit(1)
    return _RetryConnection(get_db_connection(DB_PATH))


def log_chronicle(conn, event_type, content, agent_id=None):
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


def roman_to_int(s):
    """Convert Roman numeral string to integer."""
    vals = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}
    total = 0
    prev = 0
    for c in reversed(s):
        v = vals.get(c, 0)
        if v < prev:
            total -= v
        else:
            total += v
        prev = v
    return total

def next_bot_number(conn):
    """Get the next Doom Bot number by finding the highest existing (including retired/purged)."""
    # Check chronicle for the highest bot number ever spawned
    rows = conn.execute(
        "SELECT content FROM chronicle WHERE event_type='spawn' AND content LIKE 'DOOM-BOT-%' ORDER BY timestamp DESC"
    ).fetchall()
    max_num = 0
    for r in rows:
        # Extract DOOM-BOT-XXX from "DOOM-BOT-III spawned for..."
        part = r["content"].split(" ")[0]
        suffix = part.replace("DOOM-BOT-", "")
        num = roman_to_int(suffix)
        if num > max_num:
            max_num = num
    # Also check agents table (in case chronicle was wiped)
    rows2 = conn.execute("SELECT id FROM agents WHERE id LIKE 'DOOM-BOT-%'").fetchall()
    for r in rows2:
        suffix = r["id"].replace("DOOM-BOT-", "")
        num = roman_to_int(suffix)
        if num > max_num:
            max_num = num
    return max_num + 1


def build_bot_prompt(decree, conn, fix_errors=None):
    """Build the system prompt for a Doom Bot executing a decree.
    If fix_errors is provided, this is a repair pass with error context."""
    # Identity
    identity_rows = conn.execute("SELECT key, value FROM identity").fetchall()
    identity_block = "\n".join(f"  {r['key']}: {r['value']}" for r in identity_rows)

    # Current session
    session = conn.execute(
        "SELECT * FROM sessions ORDER BY session_number DESC LIMIT 1"
    ).fetchone()
    session_num = session["session_number"] if session else "?"

    # Relevant archives
    archives = conn.execute(
        "SELECT topic, content FROM archives ORDER BY importance ASC LIMIT 5"
    ).fetchall()
    archives = [dict(a) for a in archives]

    # LLM-validated archive filtering — only inject relevant context
    if archives and len(archives) > 3:
        archive_list = "\n".join([f"{i+1}. [{a['topic']}] {a['content'][:100]}" for i, a in enumerate(archives)])
        filter_prompt = (
            f"Given this decree: \"{decree.get('title','')}\"\n"
            f"Which of these archive entries are relevant? Return ONLY the numbers as comma-separated integers.\n\n{archive_list}"
        )
        _filter_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        try:
            _filt = subprocess.run(
                [CLAUDE_PATH, "-p", "--model", "haiku", "--no-session-persistence", filter_prompt],
                capture_output=True, text=True, timeout=10, env=_filter_env
            )
            if _filt.returncode == 0:
                import re as _re
                nums = [int(n.strip()) for n in _re.findall(r'\d+', _filt.stdout) if n.strip().isdigit()]
                filtered = [archives[n-1] for n in nums if 0 < n <= len(archives)]
                if filtered:
                    archives = filtered
        except Exception:
            pass  # Fall back to unfiltered

    archives_block = "\n".join(
        f"  [{r['topic']}] {r['content'][:300]}"
        for r in archives
    ) or "  None"

    # Bot collaboration — inject output from predecessor decrees (blocked_by chain)
    collab_block = ""
    try:
        blocked_by = conn.execute("SELECT blocked_by FROM decrees WHERE id=?", (decree["id"],)).fetchone()
        if blocked_by and blocked_by["blocked_by"]:
            predecessor_ids = [x.strip() for x in blocked_by["blocked_by"].split(",") if x.strip()]
            sibling_outputs = []
            for pred_id in predecessor_ids[:3]:  # Max 3 predecessors
                pred = conn.execute(
                    "SELECT title, fulfillment_notes FROM decrees WHERE id=? AND status IN ('fulfilled','sealed')",
                    (pred_id,)
                ).fetchone()
                if pred and pred["fulfillment_notes"]:
                    notes = pred["fulfillment_notes"][:2000]
                    sibling_outputs.append(f"  [{pred_id}] {pred['title']}:\n{notes}")
            if sibling_outputs:
                collab_block = "\n\nPREDECESSOR DECREE OUTPUTS (build on this work):\n" + "\n\n".join(sibling_outputs) + "\n"
    except Exception:
        pass

    # Lessons learned — query past failures on similar work
    lessons_block = ""
    try:
        keywords = decree["title"].split()
        # Filter to meaningful words (skip short/common ones)
        keywords = [w for w in keywords if len(w) > 3 and w.lower() not in ("the", "and", "for", "from", "with", "that", "this", "build", "create")][:5]
        past_issues = []
        for kw in keywords:
            # Blocked decrees with similar keywords
            blocked = conn.execute(
                "SELECT title, fulfillment_notes FROM decrees WHERE status='blocked' AND (title LIKE ? OR fulfillment_notes LIKE ?) LIMIT 2",
                (f"%{kw}%", f"%{kw}%")
            ).fetchall()
            for b in blocked:
                note = (b["fulfillment_notes"] or "")[:150]
                if note and note not in past_issues:
                    past_issues.append(f"- {b['title']}: {note}")
            # Warnings from chronicle
            warns = conn.execute(
                "SELECT content FROM chronicle WHERE event_type IN ('warning','error') AND content LIKE ? ORDER BY timestamp DESC LIMIT 2",
                (f"%{kw}%",)
            ).fetchall()
            for w in warns:
                snippet = w["content"][:150]
                if snippet not in past_issues:
                    past_issues.append(f"- {snippet}")
        if past_issues:
            lessons_text = "\n".join(past_issues[:5])
            lessons_block = f"\n\nLESSONS LEARNED (from past failures on similar work):\n{lessons_text}\nAvoid repeating these mistakes.\n"
    except Exception:
        pass  # Don't let lessons query break bot spawning

    # Verification instructions — the bot MUST test its own work
    verify_block = """
VERIFICATION PROTOCOL (MANDATORY):
After writing code, you MUST verify it works before reporting completion:
1. Run the code. If it's a Flask app, start it briefly and curl the endpoints.
2. If it's a module, import it and call the main functions.
3. If tests exist, run them.
4. If there are errors, FIX THEM. Do not report success with broken code.
5. Repeat until the code runs cleanly with no errors.
6. Only then report completion with a summary of what works."""

    # If this is a fix pass, add error context
    fix_block = ""
    if fix_errors:
        fix_block = f"""

CRITICAL — PREVIOUS BUILD HAD ERRORS:
The code you (or a previous bot) wrote has the following errors that MUST be fixed:

{fix_errors}

Fix ALL of these errors. Then verify the fix works by running the code again.
Do not just patch — understand the root cause and fix it properly."""

    # Inject relevant solution patterns
    solutions_block = ""
    try:
        title_words = (decree.get("title") or "").lower().split()
        desc_words = (decree.get("description") or "").lower().split()
        keywords = [w for w in title_words + desc_words if len(w) > 4][:8]
        if keywords:
            conditions = " OR ".join(["problem LIKE ?"] * len(keywords))
            params = [f"%{kw}%" for kw in keywords]
            solutions = conn.execute(
                f"SELECT problem, solution FROM solutions WHERE {conditions} ORDER BY success_count DESC, last_used DESC LIMIT 3",
                params
            ).fetchall()
            if solutions:
                sol_text = "\n".join([f"- Problem: {s['problem']}\n  Solution: {s['solution']}" for s in solutions])
                solutions_block = f"\n\nPROVEN SOLUTIONS FROM PAST DECREES:\n{sol_text}\n"
    except Exception:
        pass

    # Inject completed steps context for crash recovery
    crash_recovery_block = ""
    try:
        completed_steps = conn.execute(
            "SELECT step_number, description, output FROM decree_steps WHERE decree_id=? AND status='completed' ORDER BY step_number",
            (decree.get("id", ""),)
        ).fetchall()
        if completed_steps:
            steps_text = "\n".join([f"Step {s['step_number']}: {s['description']} — COMPLETED\nOutput: {(s['output'] or '')[:200]}" for s in completed_steps])
            crash_recovery_block = (
                f"\n\nCRASH RECOVERY — PREVIOUSLY COMPLETED STEPS:\n{steps_text}\n"
                f"These steps are DONE. Do NOT redo them. Continue from where the previous bot left off.\n"
            )
    except Exception:
        pass

    system_prompt = f"""You are a Doom Bot — an autonomous execution agent in the DOOM framework.

You have been spawned to execute a single decree. Complete it thoroughly, then report.

FRAMEWORK IDENTITY:
{identity_block}

CURRENT SESSION: {session_num}

YOUR DECREE:
  ID: {decree['id']}
  Title: {decree['title']}
  Description: {decree['description'] or 'No additional description.'}
  Priority: {decree['priority']}

RELEVANT ARCHIVES:
{archives_block}

STANDING ORDERS:
- Execute the decree fully. Do the actual work — write code, edit files, run commands.
- If the decree references a specific external project path (e.g. ~/projects/my-app/), work DIRECTLY in that directory. Modify files there. Do NOT copy them to your project folder.
- If no external path is referenced, your working directory is your project folder: {os.path.join(DOOMBOT_DIR, 'projects', decree['id'])}/
- The DOOM framework lives at {DOOMBOT_DIR}/ — NEVER modify framework files (server.py, worker.py, watchtower.py, introspect.py, dm.py, start.sh, doom-ui.html, doom-mobile.html). These are protected.
- You have full web access. Use WebSearch to find documentation, APIs, libraries, tutorials, or any information you need. Use WebFetch to read web pages. Do not guess — look it up.
- Be thorough but efficient. Do not over-engineer.
- When complete, output a clear summary of what was accomplished.
- If you cannot complete the decree, explain why clearly.
{collab_block}{lessons_block}{solutions_block}{crash_recovery_block}{verify_block}{fix_block}"""

    return system_prompt


MAX_EXECUTION_TIME = 1800  # 30 minutes per claude pass
MAX_VERIFY_LOOPS = 3       # Max build-verify-fix cycles


def verify_project(project_dir):
    """Verify a project works by running syntax checks and attempting imports/launches.
    Returns (success: bool, errors: str)."""
    errors = []

    # Find all Python files
    py_files = []
    for root, dirs, files in os.walk(project_dir):
        # Skip venvs and caches
        dirs[:] = [d for d in dirs if d not in ('.venv', '__pycache__', 'node_modules', '.git')]
        for f in files:
            if f.endswith('.py'):
                py_files.append(os.path.join(root, f))

    if not py_files:
        return True, ""  # No Python files to verify

    # 1. Syntax check all Python files
    for pf in py_files:
        try:
            result = subprocess.run(
                [sys.executable, "-c", f"import py_compile; py_compile.compile('{pf}', doraise=True)"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                errors.append(f"SYNTAX ERROR in {os.path.relpath(pf, project_dir)}:\n{result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            pass
        except Exception as e:
            errors.append(f"Could not check {os.path.relpath(pf, project_dir)}: {e}")

    # 2. Try importing the main modules (look for app.py, main.py, or __init__.py)
    venv_python = os.path.join(project_dir, ".venv", "bin", "python")
    if not os.path.isfile(venv_python):
        venv_python = sys.executable

    main_files = [f for f in py_files if os.path.basename(f) in ('app.py', 'main.py', 'server.py')]
    for mf in main_files:
        rel = os.path.relpath(mf, project_dir)
        try:
            # Import check — just load the module, don't run it
            result = subprocess.run(
                [venv_python, "-c", f"import importlib.util, sys; "
                 f"spec = importlib.util.spec_from_file_location('_check', '{mf}'); "
                 f"mod = importlib.util.module_from_spec(spec); "
                 f"sys.modules['_check'] = mod; "
                 f"spec.loader.exec_module(mod); "
                 f"print('OK')"],
                capture_output=True, text=True, timeout=30,
                cwd=project_dir,
                env={**os.environ, "FLASK_RUN_FROM_CLI": "false"},
            )
            if result.returncode != 0:
                err = result.stderr.strip()
                # Filter out Flask "running" messages — those are fine
                if "Error" in err or "Traceback" in err or "ImportError" in err:
                    errors.append(f"IMPORT ERROR in {rel}:\n{err[-500:]}")
        except subprocess.TimeoutExpired:
            pass  # Module runs a server loop — that's OK
        except Exception as e:
            errors.append(f"Could not test {rel}: {e}")

    # 3. If there's a start.sh, check it's executable and has valid shebang
    start_sh = os.path.join(project_dir, "start.sh")
    if os.path.isfile(start_sh):
        if not os.access(start_sh, os.X_OK):
            errors.append("start.sh is not executable (missing chmod +x)")
        with open(start_sh) as f:
            first_line = f.readline()
            if not first_line.startswith("#!"):
                errors.append("start.sh missing shebang (#!/bin/bash)")

    if errors:
        return False, "\n\n".join(errors)
    return True, ""


def _register_process(proc, agent_id=None, decree_id=None):
    """Track a subprocess for cleanup on shutdown — in-memory and in DB."""
    with _active_processes_lock:
        _active_processes.add(proc.pid)
    # Persist to processes table for cross-restart visibility
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO processes (pid, agent_id, decree_id, status, started_at) "
            "VALUES (?, ?, ?, 'running', ?)",
            (proc.pid, agent_id, decree_id, now()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[WORKER] WARNING: Could not register PID {proc.pid} in DB: {e}")


def _unregister_process(proc, exit_code=None, kill_reason=None):
    """Remove a subprocess from tracking — in-memory and in DB."""
    with _active_processes_lock:
        _active_processes.discard(proc.pid)
    try:
        conn = get_db()
        conn.execute(
            "UPDATE processes SET status='exited', ended_at=?, exit_code=?, kill_reason=? "
            "WHERE pid=? AND status='running'",
            (now(), exit_code, kill_reason, proc.pid),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[WORKER] WARNING: Could not unregister PID {proc.pid} in DB: {e}")


def _cleanup_process(proc, kill_reason=None):
    """Ensure a subprocess is fully terminated and all pipes are closed."""
    exit_code = None
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        exit_code = proc.returncode
    except OSError:
        pass
    finally:
        # Explicitly close all pipes to prevent fd leaks
        for pipe in (proc.stdout, proc.stderr, proc.stdin):
            if pipe:
                try:
                    pipe.close()
                except OSError:
                    pass
        _unregister_process(proc, exit_code=exit_code, kill_reason=kill_reason)


def kill_all_tracked_processes():
    """Kill all tracked child processes (called during shutdown)."""
    with _active_processes_lock:
        pids = list(_active_processes)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    # Give them a moment then force kill
    time.sleep(1)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    # Reap all zombies
    for pid in pids:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (OSError, ChildProcessError):
            pass
    with _active_processes_lock:
        _active_processes.clear()
    # Mark all running processes in DB as killed (shutdown)
    try:
        conn = get_db()
        conn.execute(
            "UPDATE processes SET status='killed', ended_at=?, kill_reason='worker_shutdown' "
            "WHERE status='running'",
            (now(),),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def reap_stale_processes():
    """Kill processes older than 30 minutes that are still marked running.
    Also cleans up DB records for PIDs that no longer exist."""
    reaped = 0
    cleaned = 0
    try:
        conn = get_db()
        # Find all processes marked running
        stale = conn.execute(
            "SELECT id, pid, agent_id, decree_id, started_at FROM processes "
            "WHERE status='running'"
        ).fetchall()

        for row in stale:
            pid = row["pid"]
            proc_id = row["id"]
            # Check if process is still alive
            try:
                os.kill(pid, 0)  # signal 0 = existence check
                alive = True
            except OSError:
                alive = False

            if not alive:
                # Process already gone — mark as exited in DB
                conn.execute(
                    "UPDATE processes SET status='exited', ended_at=?, kill_reason='already_dead' WHERE id=?",
                    (now(), proc_id),
                )
                cleaned += 1
                continue

            # Process is alive — check age
            started = row["started_at"]
            if not started:
                continue
            try:
                started_dt = datetime.strptime(started, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                age_seconds = (datetime.now(timezone.utc) - started_dt).total_seconds()
            except (ValueError, TypeError):
                continue

            if age_seconds > 1800:  # 30 minutes
                print(f"[REAPER] Killing stale process PID {pid} (age: {int(age_seconds)}s, "
                      f"agent: {row['agent_id']}, decree: {row['decree_id']})")
                try:
                    os.kill(pid, signal.SIGTERM)
                    # Give it 5 seconds then SIGKILL
                    time.sleep(2)
                    try:
                        os.kill(pid, 0)
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass  # Already dead
                except OSError:
                    pass
                conn.execute(
                    "UPDATE processes SET status='reaped', ended_at=?, kill_reason=? WHERE id=?",
                    (now(), f"stale_reap_after_{int(age_seconds)}s", proc_id),
                )
                reaped += 1

                # Also remove from in-memory tracking
                with _active_processes_lock:
                    _active_processes.discard(pid)

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[REAPER] Error during reap: {e}")

    if reaped > 0 or cleaned > 0:
        print(f"[REAPER] Reaped {reaped} stale processes, cleaned {cleaned} dead records")
        try:
            conn = get_db()
            log_chronicle(conn, "decision", f"Process reaper: killed {reaped} stale, cleaned {cleaned} dead PIDs")
            conn.close()
        except Exception:
            pass


def kill_processes_for_decree(decree_id):
    """SIGTERM all running processes associated with a specific decree."""
    killed = 0
    try:
        conn = get_db()
        procs = conn.execute(
            "SELECT id, pid FROM processes WHERE decree_id=? AND status='running'",
            (decree_id,),
        ).fetchall()
        for row in procs:
            pid = row["pid"]
            try:
                os.kill(pid, signal.SIGTERM)
                killed += 1
                print(f"[WORKER] SIGTERM sent to PID {pid} for decree {decree_id}")
            except OSError:
                pass
            conn.execute(
                "UPDATE processes SET status='killed', ended_at=?, kill_reason=? WHERE id=?",
                (now(), f"decree_{decree_id}_completed", row["id"]),
            )
            with _active_processes_lock:
                _active_processes.discard(pid)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[WORKER] Error killing processes for decree {decree_id}: {e}")
    return killed


def _flush_output(bot_id, decree_id, text, chunk_index):
    """Write a chunk of output to bot_output table with retry."""
    for _retry in range(3):
        try:
            conn_out = _RetryConnection(get_db_connection(DB_PATH))
            conn_out.execute(
                "INSERT INTO bot_output (agent_id, decree_id, chunk, chunk_index, timestamp) VALUES (?, ?, ?, ?, ?)",
                (bot_id, decree_id, text, chunk_index, now())
            )
            conn_out.commit()
            conn_out.close()
            return True
        except Exception as e:
            if _retry < 2:
                time.sleep(0.5)
            else:
                print(f"[WORKER] {bot_id} output flush failed: {e}")
    return False


def run_claude_pass(cmd, env, project_dir, bot_id, decree_id, timeout):
    """Run a single claude CLI pass with stream-json for real-time output.
    Returns (full_output, success, error_msg)."""

    # Add stream-json flags for real-time streaming output
    stream_cmd = list(cmd)
    # Insert streaming flags after "-p"
    p_idx = stream_cmd.index("-p")
    stream_cmd.insert(p_idx + 1, "--output-format")
    stream_cmd.insert(p_idx + 2, "stream-json")
    stream_cmd.insert(p_idx + 3, "--verbose")
    stream_cmd.insert(p_idx + 4, "--include-partial-messages")
    stream_cmd.insert(p_idx + 5, "--no-session-persistence")

    print(f"[WORKER] {bot_id} CMD: {' '.join(stream_cmd[:8])}... cwd={project_dir}")

    proc = subprocess.Popen(
        stream_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=env,
        cwd=project_dir,
    )

    _register_process(proc, agent_id=bot_id, decree_id=decree_id)

    # Read stderr in background thread to capture errors
    stderr_output = []
    def _read_stderr():
        try:
            for line in proc.stderr:
                stderr_output.append(line.decode("utf-8", errors="replace"))
        except Exception:
            pass
    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()

    timed_out = threading.Event()
    def _timeout_kill():
        timed_out.set()
        try:
            proc.kill()
        except OSError:
            pass

    timeout_timer = threading.Timer(timeout, _timeout_kill)
    timeout_timer.daemon = True
    timeout_timer.start()

    full_output = ""
    result_text = ""
    output_buffer = ""
    chunk_index = 0
    last_flush = time.time()
    last_heartbeat = time.time()
    last_status = ""

    try:
        for raw_line in proc.stdout:
            if _shutdown_requested or timed_out.is_set():
                break

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                output_buffer += line + "\n"
                continue

            msg_type = obj.get("type", "")

            # Extract meaningful content from stream events
            if msg_type == "assistant":
                content_list = obj.get("message", {}).get("content", [])
                for content in content_list:
                    ctype = content.get("type", "")
                    if ctype == "text":
                        text = content.get("text", "")
                        if text:
                            output_buffer += text + "\n"
                            full_output += text + "\n"
                    elif ctype == "tool_use":
                        tool_name = content.get("name", "?")
                        tool_input = content.get("input", {})
                        # Show what tool is being used
                        status_line = f"[TOOL: {tool_name}]"
                        if tool_name == "Write" and "file_path" in tool_input:
                            status_line = f"[WRITING: {tool_input['file_path']}]"
                        elif tool_name == "Edit" and "file_path" in tool_input:
                            status_line = f"[EDITING: {tool_input['file_path']}]"
                        elif tool_name == "Bash" and "command" in tool_input:
                            cmd_preview = tool_input["command"][:80]
                            status_line = f"[RUNNING: {cmd_preview}]"
                        elif tool_name == "Read" and "file_path" in tool_input:
                            status_line = f"[READING: {tool_input['file_path']}]"
                        elif tool_name == "Grep":
                            status_line = f"[SEARCHING: {tool_input.get('pattern', '?')}]"
                        if status_line != last_status:
                            output_buffer += status_line + "\n"
                            full_output += status_line + "\n"
                            last_status = status_line
                            # Track as a decree step for crash recovery
                            try:
                                _step_conn = _RetryConnection(get_db_connection(DB_PATH))
                                _step_num = (_step_conn.execute(
                                    "SELECT COALESCE(MAX(step_number), 0) FROM decree_steps WHERE decree_id=?", (decree_id,)
                                ).fetchone()[0] or 0) + 1
                                _step_conn.execute(
                                    "INSERT OR IGNORE INTO decree_steps (decree_id, step_number, description, status, completed_at) VALUES (?, ?, ?, 'completed', ?)",
                                    (decree_id, _step_num, status_line[:200], now())
                                )
                                _step_conn.commit()
                                _step_conn.close()
                            except Exception:
                                pass

            elif msg_type == "result":
                result_text = obj.get("result", "")
                if result_text:
                    full_output = result_text  # Use clean result as final output
                # Capture cost from result metadata
                cost = obj.get("total_cost_usd", 0)
                if cost:
                    proc._doom_cost_usd = cost

            # Periodic flush to DB (every 2 seconds)
            if output_buffer and time.time() - last_flush >= 2:
                if _flush_output(bot_id, decree_id, output_buffer, chunk_index):
                    output_buffer = ""
                    chunk_index += 1
                last_flush = time.time()
                sys.stdout.write(".")
                sys.stdout.flush()

            # Heartbeat
            if time.time() - last_heartbeat > 60:
                try:
                    conn_hb = get_db()
                    conn_hb.execute("UPDATE agents SET last_active=? WHERE id=?", (now(), bot_id))
                    conn_hb.commit()
                    conn_hb.close()
                except Exception:
                    pass
                last_heartbeat = time.time()

        timeout_timer.cancel()

        if timed_out.is_set():
            _cleanup_process(proc, kill_reason=f"timeout_after_{timeout}s")
            return full_output, False, f"Timed out after {timeout}s", getattr(proc, '_doom_cost_usd', 0)

        # Flush remaining buffered output
        if output_buffer:
            _flush_output(bot_id, decree_id, output_buffer, chunk_index)

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        print()  # newline after dots

        returncode = proc.returncode
        kill_reason = f"exit_{returncode}" if returncode != 0 else "completed"
        _cleanup_process(proc, kill_reason=kill_reason)

        # Log stderr if present
        stderr_thread.join(timeout=2)
        if stderr_output:
            stderr_text = "".join(stderr_output).strip()
            if stderr_text:
                print(f"[WORKER] {bot_id} STDERR: {stderr_text[:500]}")

        cost_usd = getattr(proc, '_doom_cost_usd', 0)
        print(f"[WORKER] {bot_id} exit={returncode} output_len={len(full_output)} chunks={chunk_index} cost=${cost_usd:.4f}")

        if returncode != 0 and not full_output:
            return full_output, False, f"Exit {returncode}", cost_usd

        return full_output, True, "", cost_usd

    except Exception:
        timeout_timer.cancel()
        _cleanup_process(proc, kill_reason="exception")
        raise


SIMPLE_KEYWORDS = ["script", "hello", "test", "print", "config", "setup", "install", "fix", "patch", "rename", "delete", "cleanup"]
COMPLEX_KEYWORDS = ["full project", "dashboard", "api", "database", "authentication", "deploy", "multi-page", "real-time"]

def _select_model(decree, fix_pass):
    """Auto-select model based on decree complexity. Fix passes always use opus."""
    if fix_pass > 0:
        return "opus"  # Fix passes need the best model

    title = (decree.get("title") or "").lower()
    desc = (decree.get("description") or "").lower()
    text = f"{title} {desc}"
    priority = decree.get("priority", 2)

    # Priority 1 (urgent) → opus
    if priority == 1:
        return "opus"

    # Check for complexity signals
    if any(kw in text for kw in COMPLEX_KEYWORDS):
        return "opus"

    # Check for simplicity signals
    if any(kw in text for kw in SIMPLE_KEYWORDS) and len(desc) < 200:
        return "sonnet"

    # Priority 3 (standard) with short description → sonnet
    if priority == 3 and len(desc) < 300:
        return "sonnet"

    # Default → opus
    return "opus"


def execute_decree(decree, dry_run=False):
    """Execute a single decree with build-verify-fix loop."""
    conn = get_db()

    # Verify decree is still active
    current = conn.execute("SELECT status FROM decrees WHERE id=?", (decree["id"],)).fetchone()
    if not current or current["status"] != "active":
        print(f"[WORKER] Decree {decree['id']} is no longer active (status={current['status'] if current else 'gone'}). Skipping.")
        conn.close()
        return False

    # Assign a bot
    with _bot_name_lock:
        bot_num = next_bot_number(conn)
        bot_id = f"DOOM-BOT-{to_roman(bot_num)}"
        decree_id = decree["id"]
        ts = now()

        print(f"[WORKER] Spawning {bot_id} for decree {decree_id}: {decree['title']}")
        voice_report(f"{bot_id} deployed. Objective: {decree['title']}", "spawn")

        if dry_run:
            print(f"[WORKER] DRY RUN — would execute: {decree['title']}")
            conn.close()
            return True

        conn.execute(
            "INSERT OR REPLACE INTO agents (id, type, status, current_decree, context_pct, spawned_at, last_active, notes) "
            "VALUES (?, 'doom_bot', 'active', ?, 0, ?, ?, ?)",
            (bot_id, decree_id, ts, ts, f"Executing: {decree['title']}"),
        )
        conn.execute(
            "UPDATE decrees SET status='active', assigned_to=?, updated_at=? WHERE id=?",
            (bot_id, ts, decree_id),
        )
        log_chronicle(conn, "spawn", f"{bot_id} spawned for decree {decree_id}: {decree['title']}", bot_id)
        decree["_start_time"] = time.time()
        decree["_started_at"] = ts
        conn.commit()
    conn.close()

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    project_dir = os.path.join(DOOMBOT_DIR, "projects", decree_id)
    os.makedirs(project_dir, exist_ok=True)

    fix_errors = None
    last_output = ""

    for loop_num in range(MAX_VERIFY_LOOPS + 1):  # 0=build, 1-3=fix passes
        pass_label = "BUILD" if loop_num == 0 else f"FIX #{loop_num}"
        print(f"[WORKER] {bot_id} — {pass_label} pass for {decree_id}")

        # Build prompt (with error context on fix passes)
        conn2 = get_db()
        system_prompt = build_bot_prompt(decree, conn2, fix_errors=fix_errors)
        conn2.close()

        if loop_num == 0:
            user_prompt = f"Execute this decree now: {decree['title']}"
            if decree["description"]:
                user_prompt += f"\n\nDetails: {decree['description']}"
        else:
            user_prompt = (
                f"The previous build of decree '{decree['title']}' has errors. "
                f"Fix them now. The project is at {project_dir}\n\n"
                f"ERRORS TO FIX:\n{fix_errors}"
            )

        # Update bot status
        try:
            conn_up = get_db()
            conn_up.execute(
                "UPDATE agents SET notes=?, last_active=? WHERE id=?",
                (f"{pass_label}: {decree['title']}", now(), bot_id),
            )
            conn_up.commit()
            conn_up.close()
        except Exception:
            pass

        # Smart model routing — use decree's model if set, otherwise auto-select
        model = decree.get("model") or _select_model(decree, loop_num)

        cmd = [
            CLAUDE_PATH, "-p",
            "--model", model,
            "--system-prompt", system_prompt,
            "--dangerously-skip-permissions",
            user_prompt,
        ]

        try:
            full_output, success, error_msg, pass_cost = run_claude_pass(
                cmd, env, project_dir, bot_id, decree_id, MAX_EXECUTION_TIME
            )
            last_output = full_output
            decree["_total_cost"] = decree.get("_total_cost", 0) + pass_cost

            if not success:
                print(f"[WORKER] {bot_id} {pass_label} FAILED: {error_msg}")
                if loop_num == MAX_VERIFY_LOOPS:
                    # Out of retries
                    break
                fix_errors = error_msg
                continue

        except FileNotFoundError:
            print(f"[WORKER] ERROR: claude CLI not found at {CLAUDE_PATH}")
            _fail_decree(decree_id, bot_id, "claude CLI not found")
            return False
        except Exception as e:
            print(f"[WORKER] {bot_id} ERROR: {e}")
            _fail_decree(decree_id, bot_id, str(e)[:500])
            return False

        # VERIFY the project
        print(f"[WORKER] {bot_id} — VERIFYING project...")
        verified, verify_errors = verify_project(project_dir)

        if verified:
            print(f"[WORKER] {bot_id} — VERIFIED OK on {pass_label}")
            # Also log what decree description points to for external projects
            desc = decree.get("description") or ""
            ext_dirs = []
            for token in desc.split():
                if token.startswith("~/") or token.startswith("/Users/"):
                    expanded = os.path.expanduser(token.rstrip("/"))
                    if os.path.isdir(expanded):
                        ext_dirs.append(expanded)
            for ext_dir in ext_dirs:
                ext_ok, ext_err = verify_project(ext_dir)
                if not ext_ok:
                    verified = False
                    verify_errors = f"External project at {ext_dir}:\n{ext_err}"
                    print(f"[WORKER] {bot_id} — External project FAILED verification: {ext_dir}")
                    break

        if verified:
            # SUCCESS — fulfill and retire, kill any orphaned processes
            kill_processes_for_decree(decree_id)
            print(f"[WORKER] {bot_id} FULFILLED decree {decree_id} (verified on pass {loop_num})")
            conn3 = get_db()
            loops_note = f" (after {loop_num} fix passes)" if loop_num > 0 else ""
            conn3.execute(
                "UPDATE decrees SET status='fulfilled', fulfilled_at=?, updated_at=?, fulfillment_notes=? WHERE id=?",
                (now(), now(), f"Verified OK{loops_note}. Output:\n{last_output[:15000]}", decree_id),
            )
            conn3.execute(
                "UPDATE agents SET status='retired', last_active=?, notes=? WHERE id=?",
                (now(), f"Fulfilled {decree_id}{loops_note}", bot_id),
            )
            log_chronicle(conn3, "decree", f"{bot_id} fulfilled decree {decree_id}: {decree['title']}{loops_note}", bot_id)
            log_chronicle(conn3, "retire", f"{bot_id} retired after fulfilling {decree_id}", bot_id)
            voice_report(f"{bot_id} fulfilled. {decree['title']} complete{loops_note}.", "fulfill")
            conn3.commit()
            conn3.close()
            # Push notification
            try:
                from notify import send_notification
                send_notification(
                    f"Decree Fulfilled: {decree['title'][:60]}",
                    f"{bot_id} completed {decree_id}{loops_note}",
                    priority="default", tags=["white_check_mark"]
                )
            except Exception:
                pass

            # Analytics logging
            try:
                conn_a = get_db()
                conn_a.execute(
                    "INSERT INTO analytics (decree_id, agent_id, model, started_at, finished_at, duration_seconds, outcome, fix_passes, output_size, cost_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'fulfilled', ?, ?, ?)",
                    (decree_id, bot_id, decree.get("model") or "opus", decree.get("_started_at", now()), now(),
                     time.time() - decree.get("_start_time", time.time()), loop_num, len(last_output),
                     decree.get("_total_cost", 0))
                )
                conn_a.commit()
                conn_a.close()
            except Exception:
                pass

            # Solution pattern storage — learn from successful decrees
            try:
                from subprocess import run as _run
                _env_sol = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
                _sol_prompt = (
                    f"Extract a reusable problem-solution pair from this completed decree.\n"
                    f"Title: {decree['title']}\nDescription: {decree.get('description','')}\n"
                    f"Output (first 2000 chars): {last_output[:2000]}\n\n"
                    f"Return ONLY valid JSON: {{\"problem\": \"short generic problem description\", \"solution\": \"concise reusable solution steps\"}}\n"
                    f"Keep both under 200 chars. Be generic — strip specific names/paths so it applies to similar future work."
                )
                _sol_result = _run(
                    [CLAUDE_PATH, "-p", "--model", "haiku", "--no-session-persistence", _sol_prompt],
                    capture_output=True, text=True, timeout=15, env=_env_sol
                )
                if _sol_result.returncode == 0:
                    import re as _re
                    _sol_match = _re.search(r'\{.*\}', _sol_result.stdout, _re.DOTALL)
                    if _sol_match:
                        _sol_data = json.loads(_sol_match.group())
                        _sol_conn = get_db()
                        _sol_conn.execute(
                            "INSERT INTO solutions (problem, solution, decree_id, agent_id) VALUES (?, ?, ?, ?)",
                            (_sol_data.get("problem", "")[:500], _sol_data.get("solution", "")[:500], decree_id, bot_id)
                        )
                        _sol_conn.commit()
                        _sol_conn.close()
                        print(f"[WORKER] {bot_id} stored solution pattern")
            except Exception as e:
                print(f"[WORKER] Solution extraction failed: {e}")

            # Auto-memory ingestion — extract knowledge to archives
            try:
                _mem_prompt = (
                    f"Analyze this completed decree and extract ONE key insight worth remembering.\n"
                    f"Title: {decree['title']}\nOutput (first 1500 chars): {last_output[:1500]}\n\n"
                    f"Return ONLY valid JSON: {{\"topic\": \"one-word topic\", \"insight\": \"the key takeaway in under 150 chars\"}}\n"
                    f"Topics should be: architecture, tooling, patterns, errors, libraries, or deployment.\n"
                    f"If there is nothing genuinely worth remembering, return {{\"topic\": \"none\", \"insight\": \"none\"}}"
                )
                _mem_result = _run(
                    [CLAUDE_PATH, "-p", "--model", "haiku", "--no-session-persistence", _mem_prompt],
                    capture_output=True, text=True, timeout=15, env=_env_sol
                )
                if _mem_result.returncode == 0:
                    _mem_match = _re.search(r'\{.*\}', _mem_result.stdout, _re.DOTALL)
                    if _mem_match:
                        _mem_data = json.loads(_mem_match.group())
                        if _mem_data.get("topic", "none") != "none":
                            _mem_conn = get_db()
                            _ar_id = f"ar-{secrets.token_hex(2)}"
                            _mem_conn.execute(
                                "INSERT INTO archives (id, topic, content, source_session, importance, created_at) "
                                "VALUES (?, ?, ?, ?, 3, ?)",
                                (_ar_id, _mem_data["topic"], _mem_data["insight"][:300], decree_id, now())
                            )
                            _mem_conn.commit()
                            _mem_conn.close()
                            print(f"[WORKER] {bot_id} archived insight: {_mem_data['topic']}")
            except Exception as e:
                print(f"[WORKER] Memory ingestion failed: {e}")

            # Pipeline trigger — create next decree in chain
            try:
                conn_p = get_db()
                trigger = conn_p.execute(
                    "SELECT triggers_decree, trigger_template FROM decrees WHERE id=?", (decree_id,)
                ).fetchone()
                if trigger and trigger["trigger_template"]:
                    import json as _json
                    tmpl = _json.loads(trigger["trigger_template"])
                    next_id = f"dc-{secrets.token_hex(2)}"
                    ts_p = now()
                    # Inject previous decree output into description
                    desc = tmpl.get("description", "")
                    desc += f"\n\nPREVIOUS DECREE OUTPUT ({decree_id}):\n{last_output[:3000]}"
                    conn_p.execute(
                        "INSERT INTO decrees (id, title, description, status, priority, created_at, updated_at, model) "
                        "VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
                        (next_id, tmpl.get("title", "Pipeline continuation"), desc,
                         tmpl.get("priority", 2), ts_p, ts_p, tmpl.get("model"))
                    )
                    log_chronicle(conn_p, "decree",
                        f"Pipeline: {decree_id} triggered {next_id}: {tmpl.get('title', '?')[:60]}", "WORKER")
                    conn_p.commit()
                    print(f"[WORKER] Pipeline: {decree_id} → {next_id}")
                conn_p.close()
            except Exception as e:
                print(f"[WORKER] Pipeline trigger error: {e}")

            return True
        else:
            print(f"[WORKER] {bot_id} — VERIFY FAILED on {pass_label}:\n{verify_errors[:300]}")
            if loop_num >= MAX_VERIFY_LOOPS:
                break
            fix_errors = verify_errors
            try:
                conn_log = get_db()
                log_chronicle(
                    conn_log, "warning",
                    f"{bot_id} verification failed (pass {loop_num}), retrying: {verify_errors[:200]}",
                    bot_id,
                )
                conn_log.close()
            except Exception:
                pass

    # All retries exhausted — mark blocked
    print(f"[WORKER] {bot_id} FAILED after {MAX_VERIFY_LOOPS} fix attempts on {decree_id}")
    _fail_decree(decree_id, bot_id, f"Failed verification after {MAX_VERIFY_LOOPS} fix passes.\nLast errors:\n{fix_errors or 'unknown'}")
    # Analytics for failure
    try:
        conn_a = get_db()
        conn_a.execute(
            "INSERT INTO analytics (decree_id, agent_id, model, started_at, finished_at, duration_seconds, outcome, fix_passes, output_size, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, 'blocked', ?, ?, ?)",
            (decree_id, bot_id, decree.get("model") or "opus", decree.get("_started_at", now()), now(),
             time.time() - decree.get("_start_time", time.time()), MAX_VERIFY_LOOPS, len(last_output),
             decree.get("_total_cost", 0))
        )
        conn_a.commit()
        conn_a.close()
    except Exception:
        pass
    # Push notification for failure
    try:
        from notify import send_notification
        send_notification(
            f"Decree BLOCKED: {decree['title'][:60]}",
            f"{bot_id} failed after {MAX_VERIFY_LOOPS} fix passes on {decree_id}",
            priority="high", tags=["x"]
        )
    except Exception:
        pass
    return False


def _fail_decree(decree_id, bot_id, reason):
    """Mark a decree as blocked, retire its bot, and kill orphaned processes."""
    # Kill any lingering processes for this decree
    kill_processes_for_decree(decree_id)
    conn = get_db()
    conn.execute(
        "UPDATE decrees SET status='blocked', updated_at=?, fulfillment_notes=? WHERE id=?",
        (now(), reason[:2000], decree_id),
    )
    conn.execute(
        "UPDATE agents SET status='retired', last_active=?, notes=? WHERE id=?",
        (now(), f"Failed: {reason[:200]}", bot_id),
    )
    log_chronicle(conn, "warning", f"{bot_id} failed on decree {decree_id}: {reason[:200]}", bot_id)
    conn.commit()
    conn.close()
    voice_report(f"{bot_id} failed on decree {decree_id}. Marking blocked.", "fail")


def find_and_claim_decrees(max_count, in_flight_ids=None):
    """Find and atomically claim up to max_count open decrees.
    in_flight_ids: set of decree IDs already being executed — skip these."""
    if in_flight_ids is None:
        in_flight_ids = set()

    conn = get_db()
    open_decrees = conn.execute(
        "SELECT * FROM decrees WHERE status='open' ORDER BY priority ASC, created_at ASC"
    ).fetchall()

    fulfilled_ids = {
        r["id"] for r in conn.execute("SELECT id FROM decrees WHERE status IN ('fulfilled','sealed')").fetchall()
    }

    claimed = []
    for d in open_decrees:
        if len(claimed) >= max_count:
            break
        if d["id"] in in_flight_ids:
            continue
        if d["blocked_by"]:
            blockers = [b.strip() for b in d["blocked_by"].split(",")]
            if not all(b in fulfilled_ids for b in blockers):
                continue
        # Atomic claim — only succeeds if still 'open'
        cur = conn.execute(
            "UPDATE decrees SET status='active', updated_at=? WHERE id=? AND status='open'",
            (now(), d["id"])
        )
        if cur.rowcount > 0:
            claimed.append(dict(d))

    conn.commit()
    conn.close()
    return claimed


def cleanup_dead_bots():
    """Purge retired bots older than 10 minutes and auto-seal fulfilled decrees older than 5 minutes."""
    conn = get_db()
    try:
        # Purge retired bots older than 10 minutes
        purged = conn.execute(
            "DELETE FROM agents WHERE status = 'retired' AND datetime(last_active) < datetime('now', '-10 minutes')"
        )
        if purged.rowcount > 0:
            log_chronicle(conn, "retire", f"Auto-purged {purged.rowcount} retired bots")
            print(f"[WORKER] Auto-purged {purged.rowcount} retired bots")

        # Retire stale "active" bots (active >2 hours with no update — they're dead)
        stale = conn.execute(
            "SELECT id FROM agents WHERE status = 'active' AND datetime(last_active) < datetime('now', '-2 hours')"
        ).fetchall()
        if stale:
            stale_ids = [r["id"] for r in stale]
            placeholders = ",".join("?" * len(stale_ids))
            conn.execute(
                f"UPDATE agents SET status = 'retired', last_active = ? WHERE id IN ({placeholders})",
                [now()] + stale_ids,
            )
            log_chronicle(conn, "retire", f"Auto-retired {len(stale_ids)} stale bots: {', '.join(stale_ids)}")
            print(f"[WORKER] Auto-retired stale bots: {', '.join(stale_ids)}")

        # Clean up bot_output for non-active bots
        # Keep output for recently retired bots (last 30 min) so UI can still show it
        conn.execute("DELETE FROM bot_output WHERE agent_id NOT IN (SELECT id FROM agents WHERE status='active') AND datetime(timestamp) < datetime('now', '-30 minutes')")

        # Auto-close sessions open for more than 24 hours
        stale_sessions = conn.execute(
            "SELECT id, session_number FROM sessions WHERE status='open' AND datetime(started_at) < datetime('now', '-24 hours')"
        ).fetchall()
        for s in stale_sessions:
            conn.execute(
                "UPDATE sessions SET status='closed', ended_at=?, summary=? WHERE id=?",
                (now(), "Auto-closed after 24 hours by worker", s["id"]),
            )
            log_chronicle(conn, "decision", f"Session {s['session_number']} ({s['id']}) auto-closed after 24 hours")
            print(f"[WORKER] Auto-closed stale session {s['id']} (open >24 hours)")

        # Recover orphaned active decrees (bot died, decree stuck at active for >5 min)
        orphaned = conn.execute(
            "SELECT d.id, d.title FROM decrees d "
            "WHERE d.status='active' "
            "AND datetime(d.updated_at) < datetime('now', '-30 minutes') "
            "AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.current_decree=d.id AND a.status='active')"
        ).fetchall()
        for d in orphaned:
            # Check if project folder has output files
            pdir = os.path.join(DOOMBOT_DIR, "projects", d["id"])
            has_output = os.path.isdir(pdir) and len([f for f in os.listdir(pdir) if not f.startswith('.')]) > 0
            if has_output:
                conn.execute(
                    "UPDATE decrees SET status='fulfilled', fulfilled_at=?, updated_at=? WHERE id=?",
                    (now(), now(), d["id"])
                )
                log_chronicle(conn, "decree", f"Auto-fulfilled orphaned decree {d['id']}: {d['title']} (output exists)")
                print(f"[WORKER] Auto-fulfilled orphaned decree {d['id']} (output exists)")
            else:
                conn.execute(
                    "UPDATE decrees SET status='open', assigned_to=NULL, updated_at=? WHERE id=?",
                    (now(), d["id"])
                )
                log_chronicle(conn, "warning", f"Reset orphaned decree {d['id']}: {d['title']} back to open")
                print(f"[WORKER] Reset orphaned decree {d['id']} to open (no output)")

        # Auto-seal fulfilled decrees older than 5 minutes
        sealed = conn.execute(
            "UPDATE decrees SET status = 'sealed', updated_at = ? "
            "WHERE status = 'fulfilled' AND datetime(fulfilled_at) < datetime('now', '-5 minutes')",
            (now(),),
        )
        if sealed.rowcount > 0:
            log_chronicle(conn, "decree", f"Auto-sealed {sealed.rowcount} fulfilled decrees")
            print(f"[WORKER] Auto-sealed {sealed.rowcount} fulfilled decrees")

        conn.commit()
    finally:
        conn.close()


def main_loop(once=False, dry_run=False, max_bots=3):
    """Main worker loop — poll for decrees and execute them."""
    # Verify claude CLI exists and is executable
    if not os.path.isfile(CLAUDE_PATH):
        print(f"[WORKER] FATAL: Claude CLI not found at {CLAUDE_PATH}")
        print("[WORKER] Install it or update CLAUDE_PATH in worker.py")
        sys.exit(1)
    if not os.access(CLAUDE_PATH, os.X_OK):
        print(f"[WORKER] FATAL: Claude CLI at {CLAUDE_PATH} is not executable")
        print(f"[WORKER] Fix with: chmod +x {CLAUDE_PATH}")
        sys.exit(1)

    # Prevent duplicate workers — with self-healing stale lock detection
    def _acquire_lock():
        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE, 'r') as f:
                    old_pid = f.read().strip()
                if old_pid and old_pid.isdigit():
                    try:
                        os.kill(int(old_pid), 0)
                        # Process is alive — real duplicate
                        print(f"[WORKER] ERROR: Another worker (PID {old_pid}) is already running. Exiting.")
                        sys.exit(1)
                    except OSError:
                        # Process is dead — stale lock
                        print(f"[WORKER] Stale lock from dead PID {old_pid} — cleaning up")
                        os.remove(LOCK_FILE)
                else:
                    print("[WORKER] Corrupt lock file — cleaning up")
                    os.remove(LOCK_FILE)
            except (IOError, ValueError):
                os.remove(LOCK_FILE)

        fd = open(LOCK_FILE, 'w')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            print("[WORKER] ERROR: Another worker is already running. Exiting.")
            sys.exit(1)
        fd.write(str(os.getpid()))
        fd.flush()
        return fd

    lock_fd = _acquire_lock()

    # Clean up lock on any exit
    import atexit
    def _cleanup_lock():
        try:
            lock_fd.close()
            os.remove(LOCK_FILE)
        except OSError:
            pass
    atexit.register(_cleanup_lock)

    # Install SIGCHLD handler to auto-reap zombie child processes
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    # Install signal handlers for graceful shutdown
    def _handle_shutdown(signum, frame):
        global _shutdown_requested
        sig_name = signal.Signals(signum).name
        print(f"\n[WORKER] Received {sig_name}. Finishing current work and shutting down...")
        _shutdown_requested = True

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    print("=" * 50)
    print("  DOOM WORKER — Background Decree Executor")
    print(f"  Polling: {DB_PATH}")
    print(f"  Interval: {POLL_INTERVAL}s")
    print(f"  Max concurrent bots: {max_bots}")
    print(f"  Claude: {CLAUDE_PATH}")
    print("=" * 50)
    print()

    if dry_run:
        print("[WORKER] DRY RUN MODE — no decrees will be executed")
        print()

    # Ensure bot_output table exists (migration)
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS bot_output (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    decree_id TEXT NOT NULL,
    chunk TEXT,
    chunk_index INTEGER,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_output_agent ON bot_output(agent_id)")

    # Ensure processes table exists (process lifecycle management)
    conn.execute("""CREATE TABLE IF NOT EXISTS processes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pid INTEGER NOT NULL,
    agent_id TEXT,
    decree_id TEXT,
    status TEXT DEFAULT 'running',
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    exit_code INTEGER,
    kill_reason TEXT
)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_processes_status ON processes(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_processes_pid ON processes(pid)")

    # Pipeline columns migration — add if not present
    try:
        conn.execute("SELECT triggers_decree FROM decrees LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE decrees ADD COLUMN triggers_decree TEXT")  # decree ID to create on fulfill
        conn.execute("ALTER TABLE decrees ADD COLUMN trigger_template TEXT")  # JSON: {title, description, priority}
        conn.execute("ALTER TABLE decrees ADD COLUMN model TEXT")  # haiku, sonnet, opus — for smart routing
        conn.execute("ALTER TABLE decrees ADD COLUMN schedule TEXT")  # cron expression for scheduled decrees
        conn.execute("ALTER TABLE decrees ADD COLUMN last_scheduled_run TIMESTAMP")
        print("[WORKER] Migration: added pipeline, model, schedule columns to decrees")

    # Analytics table migration
    conn.execute("""CREATE TABLE IF NOT EXISTS analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decree_id TEXT,
    agent_id TEXT,
    model TEXT,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    duration_seconds REAL,
    outcome TEXT,
    fix_passes INTEGER DEFAULT 0,
    output_size INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""")
    # Migration: add cost_usd if missing
    try:
        conn.execute("ALTER TABLE analytics ADD COLUMN cost_usd REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Solutions table — stores problem-solution pairs from fulfilled decrees
    conn.execute("""CREATE TABLE IF NOT EXISTS solutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem TEXT,
    solution TEXT,
    decree_id TEXT,
    agent_id TEXT,
    success_count INTEGER DEFAULT 1,
    last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""")

    # Decree steps table — crash-resilient step tracking for resume on bot death
    conn.execute("""CREATE TABLE IF NOT EXISTS decree_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decree_id TEXT NOT NULL,
    step_number INTEGER NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'pending',
    output TEXT,
    completed_at TIMESTAMP,
    UNIQUE(decree_id, step_number)
)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decree_steps_decree ON decree_steps(decree_id)")

    # Ephemeral wisps — transient column on chronicle for auto-purge
    try:
        conn.execute("ALTER TABLE chronicle ADD COLUMN transient INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists

    conn.commit()
    conn.close()

    # Startup: retire orphaned bots whose processes are dead
    conn = get_db()
    orphans = conn.execute(
        "SELECT id, current_decree FROM agents WHERE status = 'active'"
    ).fetchall()
    if orphans:
        ts = now()
        for o in orphans:
            # Check processes table for a running PID
            proc_row = conn.execute(
                "SELECT pid FROM processes WHERE agent_id=? AND status='running' ORDER BY started_at DESC LIMIT 1",
                (o["id"],)
            ).fetchone()
            alive = False
            if proc_row:
                try:
                    os.kill(proc_row["pid"], 0)
                    alive = True
                except OSError:
                    pass
            if not alive:
                conn.execute("UPDATE agents SET status='retired', last_active=? WHERE id=?", (ts, o["id"]))
                conn.execute(
                    "UPDATE decrees SET status='blocked', fulfillment_notes='Bot process died — orphaned by worker restart', updated_at=? WHERE id=? AND status='active'",
                    (ts, o["current_decree"])
                )
                conn.execute("UPDATE processes SET status='killed', ended_at=?, kill_reason='orphan_cleanup' WHERE agent_id=? AND status='running'",
                             (ts, o["id"]))
                print(f"[WORKER] Retired orphaned bot {o['id']} (decree {o['current_decree']})")
                log_chronicle(conn, "retire", f"Retired orphaned bot {o['id']} on startup — process dead", o["id"])
        conn.commit()
    conn.close()

    # Auto-open session if none exists
    conn = get_db()
    session = conn.execute("SELECT id FROM sessions WHERE status='open' LIMIT 1").fetchone()
    if not session:
        next_num = (conn.execute("SELECT MAX(session_number) as m FROM sessions").fetchone()["m"] or 0) + 1
        sid = f"session-{next_num:03d}"
        conn.execute("INSERT OR IGNORE INTO sessions (id, session_number, started_at, focus, status) VALUES (?, ?, ?, ?, 'open')",
                     (sid, next_num, now(), "Auto-opened by worker"))
        conn.commit()
        print(f"[WORKER] Session {next_num} opened ({sid})")
    conn.close()

    # Purge transient chronicle entries older than 24 hours
    try:
        conn = get_db()
        purged = conn.execute(
            "DELETE FROM chronicle WHERE transient=1 AND datetime(timestamp) < datetime('now', '-24 hours')"
        ).rowcount
        if purged:
            conn.commit()
            print(f"[WORKER] Purged {purged} transient chronicle entries")
        conn.close()
    except Exception:
        pass

    executor = ThreadPoolExecutor(max_workers=max_bots)
    active_futures = {}  # decree_id -> Future

    cycle = 0
    try:
        while not _shutdown_requested:
            cycle += 1

            # Clean up completed futures
            done_ids = [did for did, f in active_futures.items() if f.done()]
            for did in done_ids:
                try:
                    active_futures[did].result()  # Re-raise exceptions for logging
                except Exception as e:
                    print(f"[WORKER] Bot for {did} raised: {e}")
                del active_futures[did]

            # Autonomous cleanup every 12 cycles (~60 seconds)
            if cycle % 12 == 0:
                try:
                    cleanup_dead_bots()
                except Exception as e:
                    print(f"[WORKER] Cleanup error: {e}")

            # Process reaper every 60 cycles (~5 minutes) — kills stale PIDs >30 min
            if cycle % 60 == 0:
                try:
                    reap_stale_processes()
                except Exception as e:
                    print(f"[WORKER] Reaper error: {e}")

            # How many slots available?
            slots = max_bots - len(active_futures)
            decrees = []
            if slots > 0 and not _shutdown_requested:
                decrees = find_and_claim_decrees(slots, in_flight_ids=set(active_futures.keys()))
                for decree in decrees:
                    print(f"\n[WORKER] Decree claimed: {decree['id']} — {decree['title']}")
                    future = executor.submit(execute_decree, decree, dry_run)
                    active_futures[decree['id']] = future
                    if once:
                        break

            if once and not active_futures:
                if not decrees:
                    print("[WORKER] No decrees to execute. Exiting.")
                break

            if cycle == 1 and not active_futures:
                print("[WORKER] No open decrees. Watching...")

            time.sleep(POLL_INTERVAL)

    finally:
        # Graceful shutdown: wait for active bots to finish
        if active_futures:
            print(f"[WORKER] Waiting for {len(active_futures)} active bot(s) to finish...")
            for did, f in active_futures.items():
                try:
                    f.result(timeout=60)
                except Exception as e:
                    print(f"[WORKER] Bot for {did} raised during shutdown: {e}")

        # Kill any remaining tracked child processes
        kill_all_tracked_processes()

        executor.shutdown(wait=False)

        # Clean up lock file
        try:
            lock_fd.close()
            os.remove(LOCK_FILE)
        except OSError:
            pass

        print("[WORKER] Shutdown complete. The bots rest.")


# ===========================================================================
# SIEGE ENGINE — Autonomous iteration engine
# ===========================================================================
# DOOM's relentless execution loop. Feed it an objective or PRD — it decomposes
# into decrees, executes them sequentially with fresh context each iteration,
# and doesn't stop until the objective falls or the siege breaks.
# ===========================================================================

SIEGE_MAX_ITERATIONS = 50       # hard cap — prevents runaway loops
SIEGE_COOLDOWN = 5              # seconds between iterations
SIEGE_PROGRESS_FILE = "siege_progress.md"

def siege_decompose(objective, project_path, max_stories=10):
    """Use Claude to decompose an objective into ordered decree-sized stories.
    Returns list of dicts: [{title, description, priority}, ...]"""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    prompt = (
        f"You are a project decomposer. Break this objective into {max_stories} or fewer "
        f"sequential implementation stories. Each story must be completable in one Claude session.\n\n"
        f"OBJECTIVE:\n{objective}\n\n"
        f"PROJECT PATH: {project_path}\n\n"
        f"Return ONLY a valid JSON array. Each element:\n"
        f'{{"title": "short imperative title", "description": "detailed requirements and acceptance criteria", "priority": 2}}\n\n'
        f"Order them by dependency — earliest first. Keep each story focused and small.\n"
        f"Do NOT include testing-only stories. Each story should produce working code."
    )
    try:
        result = subprocess.run(
            [CLAUDE_PATH, "-p", "--model", "sonnet", "--no-session-persistence", prompt],
            capture_output=True, text=True, timeout=60, env=env
        )
        if result.returncode == 0:
            import re
            # Find the JSON array in the output
            match = re.search(r'\[.*\]', result.stdout, re.DOTALL)
            if match:
                stories = json.loads(match.group())
                return stories[:max_stories]
    except Exception as e:
        print(f"[SIEGE] Decomposition failed: {e}")
    return []


def siege_write_progress(project_path, completed, total, stories, iteration):
    """Write a progress file so the next iteration knows what's done."""
    progress_path = os.path.join(project_path, SIEGE_PROGRESS_FILE)
    lines = [
        f"# SIEGE Progress",
        f"",
        f"**Iteration**: {iteration}",
        f"**Completed**: {completed}/{total}",
        f"**Status**: {'ALL DONE' if completed >= total else 'IN PROGRESS'}",
        f"",
        f"## Stories",
        f"",
    ]
    for i, s in enumerate(stories):
        status = s.get("_status", "pending")
        marker = "[x]" if status == "fulfilled" else "[ ]"
        lines.append(f"- {marker} **{i+1}. {s['title']}** — {status}")
        if s.get("_notes"):
            lines.append(f"  - Notes: {s['_notes'][:200]}")
    lines.append("")
    with open(progress_path, "w") as f:
        f.write("\n".join(lines))
    return progress_path


def siege_loop(objective, project_path=None, max_iterations=None, prd_file=None, tag=None, auto_commit=True):
    """
    Run the SIEGE autonomous loop.

    Args:
        objective: Text description of what to build (ignored if prd_file given)
        project_path: Where to build (created if needed)
        max_iterations: Override SIEGE_MAX_ITERATIONS
        prd_file: Path to a PRD/JSON file with pre-defined stories
        tag: Optional tag for grouping these decrees in the DB
        auto_commit: Git commit after each successful story
    """
    max_iter = max_iterations or SIEGE_MAX_ITERATIONS
    tag = tag or f"siege-{secrets.token_hex(3)}"

    if not project_path:
        project_path = os.path.join(DOOMBOT_DIR, "projects", f"siege-{secrets.token_hex(3)}")
    project_path = os.path.expanduser(project_path)
    os.makedirs(project_path, exist_ok=True)

    conn = get_db()

    # Log the siege session start
    log_chronicle(conn, "decision", f"[SIEGE] Loop started — tag={tag}, objective: {objective[:100]}", "SIEGE")
    conn.close()

    print("=" * 60)
    print("  SIEGE ENGINE — Autonomous Iteration Engine")
    print(f"  Objective: {objective[:80]}")
    print(f"  Project:   {project_path}")
    print(f"  Max iter:  {max_iter}")
    print(f"  Tag:       {tag}")
    print(f"  Commit:    {'yes' if auto_commit else 'no'}")
    print("=" * 60)
    print()

    # Step 1: Get stories — from PRD file or decompose the objective
    stories = []
    if prd_file and os.path.exists(prd_file):
        print(f"[SIEGE] Loading PRD from {prd_file}")
        try:
            with open(prd_file) as f:
                content = f.read()
            # Try JSON first
            try:
                stories = json.loads(content)
                if isinstance(stories, dict) and "stories" in stories:
                    stories = stories["stories"]
            except json.JSONDecodeError:
                # Treat as plain text objective, decompose it
                print("[SIEGE] PRD is not JSON — decomposing as text objective...")
                stories = siege_decompose(content, project_path)
        except Exception as e:
            print(f"[SIEGE] Failed to load PRD: {e}")
            return False

    if not stories:
        print("[SIEGE] Decomposing objective into stories...")
        stories = siege_decompose(objective, project_path)

    if not stories:
        print("[SIEGE] ERROR: Could not decompose objective into stories. Aborting.")
        return False

    print(f"[SIEGE] {len(stories)} stories to execute:")
    for i, s in enumerate(stories):
        print(f"  {i+1}. {s.get('title', 'Untitled')}")
    print()

    # Step 2: Create decrees for each story
    conn = get_db()
    decree_ids = []
    prev_id = None
    for i, story in enumerate(stories):
        d_id = gen_id("dc")
        ts = now()
        blocked_by = prev_id if prev_id and i > 0 else None  # chain them sequentially
        conn.execute(
            "INSERT INTO decrees (id, title, description, status, priority, blocked_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
            (d_id, f"[SIEGE] {story.get('title', f'Story {i+1}')}",
             f"TAG: {tag}\nPROJECT: {project_path}\n\n{story.get('description', story.get('title', ''))}",
             story.get("priority", 2), blocked_by, ts, ts)
        )
        decree_ids.append(d_id)
        stories[i]["_decree_id"] = d_id
        stories[i]["_status"] = "pending"
        prev_id = d_id
    log_chronicle(conn, "decree", f"[SIEGE] Created {len(decree_ids)} decrees for tag={tag}", "SIEGE")
    conn.commit()
    conn.close()

    # Step 3: Execute the loop
    completed = 0
    blocked_count = 0
    iteration = 0
    total_cost = 0.0

    for iteration in range(1, max_iter + 1):
        if _shutdown_requested:
            print(f"\n[SIEGE] Shutdown requested at iteration {iteration}. Stopping.")
            break

        # Find next pending story
        next_story = None
        next_idx = None
        for idx, s in enumerate(stories):
            if s["_status"] == "pending":
                next_story = s
                next_idx = idx
                break

        if next_story is None:
            print(f"\n[SIEGE] All stories processed after {iteration - 1} iterations.")
            break

        d_id = next_story["_decree_id"]
        print(f"\n{'='*50}")
        print(f"  SIEGE — Iteration {iteration}/{max_iter}")
        print(f"  Story {next_idx + 1}/{len(stories)}: {next_story.get('title', '?')}")
        print(f"  Progress: {completed}/{len(stories)} complete, {blocked_count} blocked")
        print(f"{'='*50}")

        # Write progress file for context
        siege_write_progress(project_path, completed, len(stories), stories, iteration)

        # Unblock this decree (the sequential chain was just for ordering)
        conn = get_db()
        conn.execute("UPDATE decrees SET blocked_by=NULL, status='open' WHERE id=?", (d_id,))
        conn.commit()
        conn.close()

        # Claim and execute
        conn = get_db()
        decree_row = conn.execute("SELECT * FROM decrees WHERE id=?", (d_id,)).fetchone()
        conn.close()

        if not decree_row:
            print(f"[SIEGE] Decree {d_id} not found — skipping")
            stories[next_idx]["_status"] = "skipped"
            continue

        decree = dict(decree_row)

        # Inject siege progress context into description
        progress_path = os.path.join(project_path, SIEGE_PROGRESS_FILE)
        if os.path.exists(progress_path):
            with open(progress_path) as f:
                progress_text = f.read()
            decree["description"] = (
                f"{decree['description']}\n\n"
                f"--- SIEGE ENGINE PROGRESS ---\n{progress_text}\n"
                f"--- END PROGRESS ---\n\n"
                f"You are iteration {iteration}. "
                f"Previous iterations have completed {completed}/{len(stories)} stories. "
                f"Review what's already done in {project_path} before starting. "
                f"Do NOT redo completed work."
            )

        # Mark active and execute
        conn = get_db()
        conn.execute("UPDATE decrees SET status='active', updated_at=? WHERE id=?", (now(), d_id))
        conn.commit()
        conn.close()

        success = execute_decree(decree)

        if success:
            completed += 1
            stories[next_idx]["_status"] = "fulfilled"
            stories[next_idx]["_notes"] = "Completed"
            print(f"[SIEGE] Story {next_idx + 1} FULFILLED ({completed}/{len(stories)})")

            # Auto-commit if enabled and project has git
            if auto_commit:
                try:
                    git_dir = os.path.join(project_path, ".git")
                    if os.path.isdir(git_dir):
                        subprocess.run(
                            ["git", "add", "-A"],
                            cwd=project_path, capture_output=True, timeout=10
                        )
                        subprocess.run(
                            ["git", "commit", "-m",
                             f"[Siege #{iteration}] {next_story.get('title', f'Story {next_idx+1}')}"],
                            cwd=project_path, capture_output=True, timeout=10
                        )
                        print(f"[SIEGE] Auto-committed iteration {iteration}")
                except Exception as e:
                    print(f"[SIEGE] Auto-commit failed: {e}")
        else:
            blocked_count += 1
            stories[next_idx]["_status"] = "blocked"
            stories[next_idx]["_notes"] = "Failed after max retries"
            print(f"[SIEGE] Story {next_idx + 1} BLOCKED ({blocked_count} total failures)")

            # If 3+ consecutive failures, abort the loop — something is fundamentally broken
            recent_statuses = [s["_status"] for s in stories if s["_status"] != "pending"]
            if len(recent_statuses) >= 3 and all(s == "blocked" for s in recent_statuses[-3:]):
                print(f"[SIEGE] 3 consecutive failures — aborting loop. Something is broken.")
                break

        # Cooldown between iterations
        if not _shutdown_requested:
            time.sleep(SIEGE_COOLDOWN)

    # Final progress write
    siege_write_progress(project_path, completed, len(stories), stories, iteration)

    # Log completion
    conn = get_db()
    summary = (
        f"[SIEGE] Loop complete — tag={tag}, "
        f"{completed}/{len(stories)} fulfilled, {blocked_count} blocked, "
        f"{iteration} iterations"
    )
    log_chronicle(conn, "decision", summary, "SIEGE")
    conn.close()

    print()
    print("=" * 60)
    print("  SIEGE ENGINE — COMPLETE")
    print(f"  Fulfilled: {completed}/{len(stories)}")
    print(f"  Blocked:   {blocked_count}")
    print(f"  Iterations: {iteration}")
    print(f"  Project:   {project_path}")
    print("=" * 60)

    # Push notification
    try:
        from notify import send_notification
        send_notification(
            f"Siege Complete: {completed}/{len(stories)}",
            f"Tag: {tag} — {completed} fulfilled, {blocked_count} blocked in {iteration} iterations",
            priority="default", tags=["repeat"]
        )
    except Exception:
        pass

    return completed == len(stories)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DOOM Worker — Background Decree Executor")
    parser.add_argument("--once", action="store_true", help="Execute one decree and exit")
    parser.add_argument("--dry-run", action="store_true", help="Show what would execute without running")
    parser.add_argument("--interval", type=int, default=5, help="Poll interval in seconds (default: 5)")
    parser.add_argument("--max-bots", type=int, default=3, help="Max concurrent bots (default: 3)")

    # Siege mode
    parser.add_argument("--siege", action="store_true", help="Run in Siege Engine mode")
    parser.add_argument("--objective", type=str, help="Siege: objective text to decompose and execute")
    parser.add_argument("--prd", type=str, help="Siege: path to PRD file (JSON or text)")
    parser.add_argument("--project-path", type=str, help="Siege: project directory path")
    parser.add_argument("--max-iterations", type=int, help="Siege: max iterations (default 50)")
    parser.add_argument("--tag", type=str, help="Siege: tag for grouping decrees")
    parser.add_argument("--no-commit", action="store_true", help="Siege: skip auto-commit after each story")

    args = parser.parse_args()

    if args.siege:
        if not args.objective and not args.prd:
            print("[SIEGE] ERROR: --siege requires --objective or --prd")
            sys.exit(1)
        objective = args.objective or "Execute PRD"
        siege_loop(
            objective=objective,
            project_path=args.project_path,
            max_iterations=args.max_iterations,
            prd_file=args.prd,
            tag=args.tag,
            auto_commit=not args.no_commit,
        )
    else:
        POLL_INTERVAL = args.interval
        main_loop(once=args.once, dry_run=args.dry_run, max_bots=args.max_bots)
