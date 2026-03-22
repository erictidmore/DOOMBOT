#!/usr/bin/env python3
"""
DOOM Watchtower — Autonomous Health Monitor Daemon

Runs every 5 minutes. Checks:
1. All registered project health (port alive, process running)
2. Queries external project API for live change, positions, value
3. Monitors bot/decree state — stale decrees, dead bots, orphans
4. Posts alerts to council table when attention needed
5. Logs all checks to chronicle

Usage:
    python watchtower.py              # Run the daemon loop
    python watchtower.py --once       # Run one check cycle and exit
    python watchtower.py --interval 60  # Custom interval in seconds
"""

import argparse
import fcntl
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone


DOOMBOT_DIR = os.path.dirname(os.path.abspath(__file__))
# If running from projects/dc-b010, resolve to actual DOOMBOT dir
if DOOMBOT_DIR.endswith(os.path.join("projects", "dc-b010")):
    DOOMBOT_DIR = os.path.dirname(os.path.dirname(DOOMBOT_DIR))

DB_PATH = os.path.join(DOOMBOT_DIR, "memory.db")
LOG_DIR = os.path.join(DOOMBOT_DIR, "logs")
LOCK_FILE = os.path.join(LOG_DIR, "watchtower.lock")
CHECK_INTERVAL = 300  # 5 minutes default

# Change alert thresholds
CHANGE_LOSS_THRESHOLD = -500.0     # Alert if unrealized change below this
CHANGE_GAIN_THRESHOLD = 2000.0     # Alert if unrealized change above this (take profit?)
EQUITY_DROP_PCT = 5.0           # Alert if value drops more than this % from last check

# Stale decree threshold (hours)
STALE_DECREE_HOURS = 4

# Graceful shutdown
_shutdown = False


def now():
    """Always return UTC timestamp — matches SQLite datetime('now')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    if not os.path.exists(DB_PATH):
        print(f"[WATCHTOWER] ERROR: {DB_PATH} not found")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def log_chronicle(conn, event_type, content, agent_id="WATCHTOWER", transient=0):
    """Write an event to the chronicle table. transient=1 for auto-purging entries."""
    session = conn.execute(
        "SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
    ).fetchone()
    session_id = session["id"] if session else None
    conn.execute(
        "INSERT INTO chronicle (session_id, event_type, agent_id, content, timestamp, transient) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, event_type, agent_id, content, now(), transient),
    )
    conn.commit()


def post_alert(conn, content, role="watchtower"):
    """Log an alert to chronicle + push notification. Council is reserved for human <-> DOOM conversation only."""
    log_chronicle(conn, "warning", content, "WATCHTOWER")
    print(f"[WATCHTOWER] {content[:100]}")
    try:
        from notify import send_notification
        send_notification("DOOM Alert", content[:200], priority="high", tags=["warning"])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CHECK 1: Project Health
# ---------------------------------------------------------------------------

def check_port_alive(port, host="localhost", timeout=3):
    """Check if a TCP port is accepting connections."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def check_process_running(pid):
    """Check if a process with given PID is alive."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def check_project_health(conn):
    """Check all registered projects for health."""
    projects = conn.execute("SELECT * FROM projects").fetchall()
    results = []

    for proj in projects:
        proj_id = proj["id"]
        proj_name = proj["name"]
        port = proj["port"]
        pid = proj["pid"]
        status = proj["status"]
        path = proj["path"]

        health = {
            "id": proj_id,
            "name": proj_name,
            "port": port,
            "port_alive": False,
            "process_alive": False,
            "path_exists": os.path.isdir(path) if path else False,
            "status_db": status,
        }

        # Check port
        if port:
            health["port_alive"] = check_port_alive(port)

        # Check process
        if pid:
            health["process_alive"] = check_process_running(pid)

        results.append(health)

        # Alert conditions
        if status == "running":
            if port and not health["port_alive"]:
                post_alert(conn,
                    f"🔴 PROJECT DOWN: {proj_name} (port {port} not responding)",
                    role="watchtower"
                )
                log_chronicle(conn, "warning",
                    f"Project {proj_name} ({proj_id}) port {port} not responding",
                    "WATCHTOWER"
                )
            if pid and not health["process_alive"]:
                post_alert(conn,
                    f"🔴 PROCESS DEAD: {proj_name} (PID {pid} not found)",
                    role="watchtower"
                )
                log_chronicle(conn, "warning",
                    f"Project {proj_name} ({proj_id}) PID {pid} not found",
                    "WATCHTOWER"
                )
                # Update status in DB
                conn.execute(
                    "UPDATE projects SET status='stopped', pid=NULL, updated_at=? WHERE id=?",
                    (now(), proj_id)
                )
                conn.commit()

    return results


# ---------------------------------------------------------------------------
# CHECK 2: external project API Status
# ---------------------------------------------------------------------------

def check_ext_project_api(conn):
    """Query external project API for bot status, change, positions, value."""
    ext_project = conn.execute(
        "SELECT * FROM projects WHERE type IS NOT NULL AND id != 'proj-doom' LIMIT 1"
    ).fetchone()

    if not ext_project:
        return None

    port = ext_project["port"] or 8070

    # First check if external project is even running
    if not check_port_alive(port):
        return {"status": "offline", "port": port}

    result = {"status": "online", "port": port}

    # Try the bot status endpoint
    endpoints = [
        (f"http://localhost:{port}/api/bot/status", "bot_status"),
        (f"http://localhost:{port}/api/bot/positions", "positions"),
        (f"http://localhost:{port}/api/bot/account", "account"),
    ]

    for url, key in endpoints:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read().decode())
            result[key] = data
        except urllib.error.HTTPError as e:
            result[key] = {"error": f"HTTP {e.code}"}
        except urllib.error.URLError:
            result[key] = {"error": "connection refused"}
        except json.JSONDecodeError:
            result[key] = {"error": "invalid JSON"}
        except Exception as e:
            result[key] = {"error": str(e)[:200]}

    # Analyze and alert on change thresholds
    bot_status = result.get("bot_status", {})
    if isinstance(bot_status, dict) and "error" not in bot_status:
        # Check unrealized change
        unrealized_pnl = bot_status.get("unrealized_pnl") or bot_status.get("pnl") or bot_status.get("unrealized_pl")
        if unrealized_pnl is not None:
            try:
                pnl = float(unrealized_pnl)
                if pnl < CHANGE_LOSS_THRESHOLD:
                    post_alert(conn,
                        f"🔴 change ALERT: Unrealized loss ${pnl:,.2f} exceeds threshold ${CHANGE_LOSS_THRESHOLD:,.2f}",
                        role="watchtower"
                    )
                elif pnl > CHANGE_GAIN_THRESHOLD:
                    post_alert(conn,
                        f"🟢 change ALERT: Unrealized gain ${pnl:,.2f} — consider taking profit (threshold ${CHANGE_GAIN_THRESHOLD:,.2f})",
                        role="watchtower"
                    )
            except (ValueError, TypeError):
                pass

        # Check value
        value = bot_status.get("value") or bot_status.get("account_value")
        if value is not None:
            try:
                value_val = float(value)
                result["value"] = value_val
            except (ValueError, TypeError):
                pass

    # Check positions
    positions = result.get("positions", {})
    if isinstance(positions, dict) and "error" not in positions:
        pos_list = positions.get("positions", positions.get("data", []))
        if isinstance(pos_list, list):
            result["open_positions_count"] = len(pos_list)
            # Check individual position change
            for pos in pos_list:
                if isinstance(pos, dict):
                    sym = pos.get("symbol", "???")
                    pos_pnl = pos.get("unrealized_pl") or pos.get("pnl") or pos.get("unrealized_pnl")
                    if pos_pnl is not None:
                        try:
                            pval = float(pos_pnl)
                            if pval < CHANGE_LOSS_THRESHOLD / 2:  # Per-position threshold is half
                                post_alert(conn,
                                    f"🔴 POSITION ALERT: {sym} unrealized ${pval:,.2f}",
                                    role="watchtower"
                                )
                        except (ValueError, TypeError):
                            pass

    return result


# ---------------------------------------------------------------------------
# CHECK 3: Bot & Decree State
# ---------------------------------------------------------------------------

def check_bot_decree_state(conn):
    """Monitor for stale decrees, dead bots, orphaned processes."""
    issues = []

    # Stale active decrees (active for too long without updates)
    stale_decrees = conn.execute(
        f"SELECT id, title, assigned_to, updated_at FROM decrees "
        f"WHERE status='active' "
        f"AND datetime(updated_at) < datetime('now', '-{STALE_DECREE_HOURS} hours')"
    ).fetchall()

    for d in stale_decrees:
        msg = f"Stale decree {d['id']}: '{d['title']}' active for >{STALE_DECREE_HOURS}h (assigned to {d['assigned_to'] or 'nobody'})"
        issues.append(msg)
        post_alert(conn,
            f"⚠️ STALE DECREE: {d['id']} '{d['title']}' — no updates for >{STALE_DECREE_HOURS}h",
            role="watchtower"
        )

    # Dead bots (status=active but no heartbeat)
    dead_bots = conn.execute(
        "SELECT id, current_decree, last_active FROM agents "
        "WHERE status='active' "
        "AND datetime(last_active) < datetime('now', '-1 hour')"
    ).fetchall()

    for bot in dead_bots:
        msg = f"Dead bot {bot['id']}: last active {bot['last_active']}, decree {bot['current_decree']}"
        issues.append(msg)
        post_alert(conn,
            f"💀 DEAD BOT: {bot['id']} — no heartbeat for >1h (decree: {bot['current_decree']})",
            role="watchtower"
        )

    # Orphaned active decrees (active but no matching active bot)
    orphaned = conn.execute(
        "SELECT d.id, d.title, d.assigned_to FROM decrees d "
        "WHERE d.status='active' "
        "AND d.assigned_to IS NOT NULL "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM agents a WHERE a.id=d.assigned_to AND a.status='active'"
        ")"
    ).fetchall()

    for d in orphaned:
        msg = f"Orphaned decree {d['id']}: '{d['title']}' assigned to {d['assigned_to']} but bot is not active"
        issues.append(msg)
        post_alert(conn,
            f"👻 ORPHANED DECREE: {d['id']} '{d['title']}' — assigned bot {d['assigned_to']} is gone",
            role="watchtower"
        )

    # Check for blocked decrees that might be unblockable
    blocked = conn.execute(
        "SELECT id, title, blocked_by FROM decrees WHERE status='blocked'"
    ).fetchall()

    for d in blocked:
        if d["blocked_by"]:
            blockers = [b.strip() for b in d["blocked_by"].split(",") if b.strip()]
            failed_blockers = conn.execute(
                f"SELECT id FROM decrees WHERE id IN ({','.join('?' * len(blockers))}) AND status='blocked'",
                blockers
            ).fetchall()
            if failed_blockers:
                failed_ids = [f["id"] for f in failed_blockers]
                issues.append(f"Decree {d['id']} blocked by failed decrees: {failed_ids}")

    # Summary stats
    stats = {
        "total_decrees": conn.execute("SELECT COUNT(*) as c FROM decrees").fetchone()["c"],
        "open_decrees": conn.execute("SELECT COUNT(*) as c FROM decrees WHERE status='open'").fetchone()["c"],
        "active_decrees": conn.execute("SELECT COUNT(*) as c FROM decrees WHERE status='active'").fetchone()["c"],
        "blocked_decrees": conn.execute("SELECT COUNT(*) as c FROM decrees WHERE status='blocked'").fetchone()["c"],
        "fulfilled_decrees": conn.execute("SELECT COUNT(*) as c FROM decrees WHERE status IN ('fulfilled','sealed')").fetchone()["c"],
        "active_bots": conn.execute("SELECT COUNT(*) as c FROM agents WHERE status='active'").fetchone()["c"],
        "total_bots": conn.execute("SELECT COUNT(*) as c FROM agents").fetchone()["c"],
        "stale_decrees": len(stale_decrees),
        "dead_bots": len(dead_bots),
        "orphaned_decrees": len(orphaned),
        "issues": issues,
    }

    return stats


# ---------------------------------------------------------------------------
# CHECK 4: Orphaned OS Processes
# ---------------------------------------------------------------------------

def _get_process_cwd(pid):
    """Get the working directory of a process (macOS)."""
    try:
        result = subprocess.run(
            ["lsof", "-p", str(pid), "-Fn"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if line.startswith("n") and line.endswith("cwd"):
                    # lsof format: "ncwd" on name line, but we need the actual path
                    pass
        # Fallback: use /proc or lsof -d cwd
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if line.startswith("n/"):
                    return line[1:]  # Strip the 'n' prefix
    except Exception:
        pass
    return None


def _is_doom_process(pid):
    """Check if a process belongs to DOOMBOT (not external_project or other projects)."""
    cwd = _get_process_cwd(pid)
    if cwd is None:
        return False  # Can't determine — don't kill
    # Only consider it a DOOM process if it's running from the DOOMBOT directory
    return DOOMBOT_DIR in cwd


def check_orphaned_processes(conn):
    """Look for DOOM-related processes that aren't tracked.

    IMPORTANT: Only kills processes running from the DOOMBOT directory.
    Processes from other projects (external_project, etc.) are never touched.
    """
    orphans = []
    try:
        # Check for python processes running worker.py or watchtower.py
        # NOTE: server.py is excluded — its PID file tracks the watchdog bash
        # process, not the Python server itself, so the server always looks
        # "orphaned" to this check. The start.sh watchdog manages server restarts.
        for proc_name in ["worker.py", "watchtower.py"]:
            result = subprocess.run(
                ["pgrep", "-f", f"python.*{proc_name}"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
                # Check against known PIDs
                pid_file = os.path.join(LOG_DIR, f"{proc_name.replace('.py', '')}.pid")
                known_pid = None
                if os.path.isfile(pid_file):
                    try:
                        known_pid = int(open(pid_file).read().strip())
                    except (ValueError, IOError):
                        pass

                for pid in pids:
                    try:
                        pid_int = int(pid)
                        if pid_int != os.getpid() and pid_int != known_pid:
                            # Only flag if it's actually a DOOM process
                            if _is_doom_process(pid_int):
                                orphans.append({
                                    "pid": pid_int,
                                    "process": proc_name,
                                    "known_pid": known_pid,
                                    "is_doom": True,
                                })
                            else:
                                # Not a DOOM process — log but never kill
                                print(f"[WATCHTOWER] Skipping non-DOOM {proc_name} PID {pid_int} (belongs to another project)")
                    except ValueError:
                        pass

        # Check for orphaned claude processes — LOG ONLY, never auto-kill
        # Claude processes may belong to other projects or interactive sessions
        result = subprocess.run(
            ["pgrep", "-f", "claude.*--dangerously-skip-permissions"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            claude_pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
            active_bots = conn.execute("SELECT COUNT(*) as c FROM agents WHERE status='active'").fetchone()["c"]
            if len(claude_pids) > active_bots + 1:
                orphans.append({
                    "pid": "multiple",
                    "process": "claude (excess, log-only)",
                    "detail": f"{len(claude_pids)} claude processes but only {active_bots} active DOOM bots",
                })

    except FileNotFoundError:
        pass  # pgrep not available
    except Exception as e:
        print(f"[WATCHTOWER] Process check error: {e}")

    if orphans:
        for o in orphans:
            pid = o.get("pid")
            proc = o.get("process", "unknown")
            is_doom = o.get("is_doom", False)
            # Only auto-kill orphaned DOOM processes confirmed to be from DOOMBOT dir
            if isinstance(pid, int) and is_doom and proc in ("server.py", "worker.py", "watchtower.py"):
                try:
                    os.kill(pid, signal.SIGTERM)
                    print(f"[WATCHTOWER] Killed orphan: {proc} PID {pid}")
                    log_chronicle(conn, "warning",
                        f"Auto-killed orphaned {proc} PID {pid}",
                        "WATCHTOWER", transient=1
                    )
                except ProcessLookupError:
                    pass  # Already dead
                except PermissionError:
                    print(f"[WATCHTOWER] Cannot kill {proc} PID {pid} — permission denied")
                    log_chronicle(conn, "warning",
                        f"Orphaned process: {proc} PID {pid} (cannot kill)",
                        "WATCHTOWER", transient=1
                    )
            else:
                # Log only for claude/non-DOOM — never auto-kill
                log_chronicle(conn, "warning",
                    f"Orphaned process: {proc} PID {pid}",
                    "WATCHTOWER", transient=1
                )
                print(f"[WATCHTOWER] Orphaned: {proc} PID {pid}")

    return orphans


# ---------------------------------------------------------------------------
# CHECK 5: Cross-Project Intelligence
# ---------------------------------------------------------------------------

_last_intel_brief = 0  # timestamp of last intelligence brief
INTEL_INTERVAL = 1800  # 30 minutes between briefs

DOOMBOT_DIR_RESOLVED = os.path.dirname(os.path.abspath(__file__))
# If running from projects/dc-b010, resolve to actual DOOMBOT dir
if DOOMBOT_DIR_RESOLVED.endswith(os.path.join("projects", "dc-b010")):
    DOOMBOT_DIR_RESOLVED = os.path.dirname(os.path.dirname(DOOMBOT_DIR_RESOLVED))

_claude_candidates = [
    os.path.expanduser("~/.local/bin/claude"),
    os.path.expanduser("~/.claude/local/claude"),
    "/usr/local/bin/claude",
]
CLAUDE_PATH = next((p for p in _claude_candidates if os.path.isfile(p)), _claude_candidates[0])


def gather_ext_project_data(conn):
    """Gather project data from external project for intelligence analysis."""
    ext_project = conn.execute(
        "SELECT * FROM projects WHERE type IS NOT NULL AND id != 'proj-doom' LIMIT 1"
    ).fetchone()
    if not ext_project:
        return None

    port = ext_project["port"] or 8070
    if not check_port_alive(port):
        return None

    data = {"port": port}
    endpoints = [
        (f"http://localhost:{port}/api/bot/status", "status"),
        (f"http://localhost:{port}/api/bot/positions", "positions"),
        (f"http://localhost:{port}/api/bot/trades", "trades"),
        (f"http://localhost:{port}/api/bot/account", "account"),
        (f"http://localhost:{port}/api/bot/performance", "performance"),
    ]

    for url, key in endpoints:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            resp = urllib.request.urlopen(req, timeout=5)
            data[key] = json.loads(resp.read().decode())
        except Exception:
            data[key] = None

    return data


def analyze_with_claude(data_summary):
    """Use Claude to generate an intelligence brief from project data."""
    prompt = f"""You are DOOM's intelligence analyst. Analyze this project data and give a brief (5-8 lines max).

Focus on:
1. Current change and risk assessment
2. Pattern observations (what's working, what's not)
3. One actionable recommendation

Be direct. No fluff. Use $ amounts and percentages.

DATA:
{data_summary}"""

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        result = subprocess.run(
            [CLAUDE_PATH, "-p", "--model", "haiku",
             "--dangerously-skip-permissions", prompt],
            capture_output=True, text=True, timeout=45, env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        print(f"[WATCHTOWER] Claude analysis failed: {e}")
    return None


def run_intelligence_brief(conn):
    """Generate and post a cross-project intelligence brief."""
    global _last_intel_brief

    data = gather_ext_project_data(conn)
    if not data:
        print("    external project offline — skipping intel brief")
        return

    # Build a summary string for Claude
    parts = []
    status = data.get("status")
    if isinstance(status, dict):
        parts.append(f"Bot running: {status.get('bot_running', '?')}")
        positions = status.get("positions", [])
        if isinstance(positions, list):
            for p in positions:
                if isinstance(p, dict):
                    parts.append(f"Position: {p.get('symbol','?')} {p.get('qty','?')}x @ ${p.get('avg_entry',0):.2f} → ${p.get('current_price',0):.2f} change: ${p.get('unrealized_pl',0):.2f} ({p.get('unrealized_plpc',0):.1%})")

    account = data.get("account")
    if isinstance(account, dict):
        parts.append(f"Equity: ${account.get('value', '?')}")
        parts.append(f"Cash: ${account.get('cash', '?')}")
        parts.append(f"Buying power: ${account.get('buying_power', '?')}")

    trades = data.get("trades")
    if isinstance(trades, list) and trades:
        winners = sum(1 for t in trades if isinstance(t, dict) and (t.get("pnl_dollar") or 0) > 0)
        losers = sum(1 for t in trades if isinstance(t, dict) and (t.get("pnl_dollar") or 0) < 0)
        open_trades = sum(1 for t in trades if isinstance(t, dict) and t.get("status") == "open")
        total_pnl = sum(t.get("pnl_dollar", 0) or 0 for t in trades if isinstance(t, dict))
        parts.append(f"Trades today: {len(trades)} total, {open_trades} open, {winners}W/{losers}L, total change ${total_pnl:.2f}")

    perf = data.get("performance")
    if isinstance(perf, dict):
        parts.append(f"Performance: {json.dumps(perf)[:200]}")

    # Decree/bot state
    state = conn.execute("SELECT COUNT(*) as c FROM decrees WHERE status='open'").fetchone()["c"]
    active = conn.execute("SELECT COUNT(*) as c FROM decrees WHERE status='active'").fetchone()["c"]
    blocked = conn.execute("SELECT COUNT(*) as c FROM decrees WHERE status='blocked'").fetchone()["c"]
    bots = conn.execute("SELECT COUNT(*) as c FROM agents WHERE status='active'").fetchone()["c"]
    parts.append(f"DOOM state: {state} open decrees, {active} active, {blocked} blocked, {bots} active bots")

    data_summary = "\n".join(parts)
    if not data_summary.strip():
        print("    No data to analyze")
        return

    print("    Generating intelligence brief via Claude...")
    brief = analyze_with_claude(data_summary)
    if brief:
        post_alert(conn, f"INTELLIGENCE BRIEF:\n{brief}", role="watchtower")
        log_chronicle(conn, "discovery", f"Intelligence brief posted: {brief[:200]}", "WATCHTOWER")
        _last_intel_brief = time.time()
        print(f"    Brief posted to council")
    else:
        print("    Claude analysis returned empty")


# ---------------------------------------------------------------------------
# MAIN CHECK CYCLE
# ---------------------------------------------------------------------------

def run_check_cycle():
    """Execute one full watchtower check cycle."""
    ts_start = time.time()
    print(f"\n{'='*50}")
    print(f"  WATCHTOWER CHECK — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    conn = get_db()
    report_lines = []

    # 1. Project Health
    print("\n  [1/4] Project Health...")
    try:
        projects = check_project_health(conn)
        for p in projects:
            status_icon = "🟢" if p["port_alive"] else "🔴" if p["status_db"] == "running" else "⚪"
            line = f"    {status_icon} {p['name']}: port {p['port']} {'UP' if p['port_alive'] else 'DOWN'}"
            if p.get("process_alive"):
                line += " | process OK"
            print(line)
            report_lines.append(line.strip())
    except Exception as e:
        print(f"    ERROR: {e}")
        report_lines.append(f"Project check error: {e}")

    # 2. external project API
    print("\n  [2/4] external project API...")
    try:
        ext_project = check_ext_project_api(conn)
        if ext_project is None:
            print("    No external project project registered")
            report_lines.append("external project: not registered")
        elif ext_project["status"] == "offline":
            print(f"    external project offline (port {ext_project['port']})")
            report_lines.append(f"external project: offline (port {ext_project['port']})")
        else:
            print(f"    external project online (port {ext_project['port']})")
            if "value" in ext_project:
                print(f"    Equity: ${ext_project['value']:,.2f}")
                report_lines.append(f"external project: online, value ${ext_project['value']:,.2f}")
            else:
                report_lines.append("external project: online")
            if "open_positions_count" in ext_project:
                print(f"    Open positions: {ext_project['open_positions_count']}")
                report_lines.append(f"Open positions: {ext_project['open_positions_count']}")
            # Log bot status details
            bot_status = ext_project.get("bot_status", {})
            if isinstance(bot_status, dict) and "error" not in bot_status:
                for k, v in bot_status.items():
                    if k not in ("error",):
                        print(f"    {k}: {v}")
    except Exception as e:
        print(f"    ERROR: {e}")
        report_lines.append(f"external project check error: {e}")

    # 3. Bot & Decree State
    print("\n  [3/4] Bot & Decree State...")
    try:
        state = check_bot_decree_state(conn)
        print(f"    Decrees: {state['open_decrees']} open, {state['active_decrees']} active, "
              f"{state['blocked_decrees']} blocked, {state['fulfilled_decrees']} fulfilled")
        print(f"    Bots: {state['active_bots']} active / {state['total_bots']} total")
        if state["stale_decrees"]:
            print(f"    ⚠️  Stale decrees: {state['stale_decrees']}")
        if state["dead_bots"]:
            print(f"    💀 Dead bots: {state['dead_bots']}")
        if state["orphaned_decrees"]:
            print(f"    👻 Orphaned decrees: {state['orphaned_decrees']}")
        report_lines.append(
            f"Decrees: {state['open_decrees']}o/{state['active_decrees']}a/"
            f"{state['blocked_decrees']}b/{state['fulfilled_decrees']}f | "
            f"Bots: {state['active_bots']}/{state['total_bots']} | "
            f"Issues: {len(state['issues'])}"
        )
    except Exception as e:
        print(f"    ERROR: {e}")
        report_lines.append(f"State check error: {e}")

    # 4. Orphaned Processes
    print("\n  [4/4] Orphaned Processes...")
    try:
        orphans = check_orphaned_processes(conn)
        if orphans:
            print(f"    Found {len(orphans)} orphaned process(es)")
            report_lines.append(f"Orphaned processes: {len(orphans)}")
        else:
            print("    No orphaned processes")
            report_lines.append("Orphaned processes: none")
    except Exception as e:
        print(f"    ERROR: {e}")
        report_lines.append(f"Process check error: {e}")

    # Log the check to chronicle BEFORE intelligence brief (release DB lock)
    elapsed_check = time.time() - ts_start
    summary = " | ".join(report_lines)
    log_chronicle(conn, "discovery", f"Watchtower check ({elapsed_check:.1f}s): {summary[:500]}", "WATCHTOWER", transient=1)
    conn.close()  # Release DB lock before long-running intel brief

    # 5. Intelligence Brief (every 30 minutes) — runs AFTER DB is released
    global _last_intel_brief
    if time.time() - _last_intel_brief >= INTEL_INTERVAL:
        print(f"\n  [5/5] Intelligence Brief...")
        try:
            conn2 = get_db()
            run_intelligence_brief(conn2)
            conn2.close()
        except Exception as e:
            print(f"    ERROR: {e}")
    else:
        mins_until = int((INTEL_INTERVAL - (time.time() - _last_intel_brief)) / 60)
        print(f"\n  [5/5] Intelligence Brief... (next in {mins_until}m)")

    print(f"\n  Check complete in {time.time() - ts_start:.1f}s")
    print(f"{'='*50}\n")


# ---------------------------------------------------------------------------
# DAEMON LOOP
# ---------------------------------------------------------------------------

def main_loop(once=False, interval=CHECK_INTERVAL):
    """Run the watchtower daemon loop."""
    os.makedirs(LOG_DIR, exist_ok=True)

    # Prevent duplicate watchtowers
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("[WATCHTOWER] ERROR: Another watchtower is already running. Exiting.")
        sys.exit(1)

    lock_fd.write(str(os.getpid()))
    lock_fd.flush()

    def _handle_shutdown(signum, frame):
        global _shutdown
        sig_name = signal.Signals(signum).name
        print(f"\n[WATCHTOWER] Received {sig_name}. Shutting down...")
        _shutdown = True

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    print("=" * 50)
    print("  DOOM WATCHTOWER — Autonomous Health Monitor")
    print(f"  Database: {DB_PATH}")
    print(f"  Interval: {interval}s ({interval // 60}m)")
    print(f"  PID: {os.getpid()}")
    print("=" * 50)
    print()

    # Log startup to chronicle
    conn = get_db()
    log_chronicle(conn, "spawn", f"Watchtower daemon started (interval={interval}s, PID={os.getpid()})", "WATCHTOWER", transient=1)
    conn.close()

    try:
        while not _shutdown:
            try:
                run_check_cycle()
            except Exception as e:
                print(f"[WATCHTOWER] Check cycle error: {e}")
                import traceback
                traceback.print_exc()

            if once:
                break

            # Sleep in small increments for responsive shutdown
            for _ in range(interval):
                if _shutdown:
                    break
                time.sleep(1)

    finally:
        # Clean up
        try:
            conn = get_db()
            log_chronicle(conn, "retire", "Watchtower daemon stopped", "WATCHTOWER", transient=1)
            conn.close()
        except Exception:
            pass

        try:
            lock_fd.close()
            os.remove(LOCK_FILE)
        except OSError:
            pass

        print("[WATCHTOWER] Shutdown complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DOOM Watchtower — Autonomous Health Monitor")
    parser.add_argument("--once", action="store_true", help="Run one check cycle and exit")
    parser.add_argument("--interval", type=int, default=CHECK_INTERVAL,
                        help=f"Check interval in seconds (default: {CHECK_INTERVAL})")
    args = parser.parse_args()

    main_loop(once=args.once, interval=args.interval)
