#!/usr/bin/env python3
"""DOOM HealthMon — Process health monitor daemon.

Checks server and worker every 30 seconds. Restarts dead processes.
Replaces the old inline bash version that kept getting killed.
"""

import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

DOOM_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(DOOM_DIR, "logs")
DB_PATH = os.path.join(DOOM_DIR, "memory.db")
PYTHON = os.path.join(DOOM_DIR, ".venv", "bin", "python")
CHECK_INTERVAL = 30  # seconds

_shutdown = False


def _handle_shutdown(signum, frame):
    global _shutdown
    print(f"[HEALTHMON] Received signal {signum}, shutting down.")
    _shutdown = True


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] HEALTHMON: {msg}", flush=True)


def chronicle_log(content):
    try:
        db = sqlite3.connect(DB_PATH, timeout=5)
        session = db.execute(
            "SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
        ).fetchone()
        sid = session[0] if session else None
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "INSERT INTO chronicle (session_id, event_type, agent_id, content, timestamp) VALUES (?, 'warning', 'HEALTHMON', ?, ?)",
            (sid, content, ts),
        )
        db.commit()
        db.close()
    except Exception:
        pass


def is_alive(pidfile):
    """Check if the process in a PID file is running."""
    if not os.path.isfile(pidfile):
        return False
    try:
        pid = int(open(pidfile).read().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return False


def restart_process(name, script, pidfile, logfile):
    """Restart a dead daemon process."""
    old_pid = "unknown"
    try:
        old_pid = open(pidfile).read().strip()
    except Exception:
        pass

    log(f"{name} died (PID was {old_pid}). Restarting...")

    try:
        os.remove(pidfile)
    except OSError:
        pass

    with open(logfile, "a") as lf:
        proc = subprocess.Popen(
            [PYTHON, "-u", os.path.join(DOOM_DIR, script)],
            stdout=lf,
            stderr=lf,
            start_new_session=True,
        )

    with open(pidfile, "w") as f:
        f.write(str(proc.pid))

    time.sleep(2)

    if proc.poll() is None:
        log(f"{name} restarted successfully (new PID {proc.pid})")
        chronicle_log(f"{name} process died and was restarted by HEALTHMON (new PID {proc.pid})")
    else:
        log(f"{name} failed to restart — check {logfile}")
        chronicle_log(f"{name} process died and HEALTHMON failed to restart it")


def main():
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    log("Health monitor started (checking every 30s)")
    print(f"  PID: {os.getpid()}", flush=True)

    while not _shutdown:
        time.sleep(CHECK_INTERVAL)
        if _shutdown:
            break

        # Check server (watchdog PID)
        server_pid = os.path.join(LOG_DIR, "server.pid")
        if os.path.isfile(server_pid) and not is_alive(server_pid):
            # Server watchdog died — restart the whole server via start.sh would be ideal
            # but for now just log it; the watchdog manages the actual server process
            log("Server watchdog PID is dead — server may need manual restart")
            chronicle_log("Server watchdog process died — may need ./start.sh restart")

        # Check worker
        worker_pid = os.path.join(LOG_DIR, "worker.pid")
        if os.path.isfile(worker_pid) and not is_alive(worker_pid):
            restart_process(
                "Worker", "worker.py",
                worker_pid,
                os.path.join(LOG_DIR, "worker.log"),
            )

    log("Health monitor stopped.")


if __name__ == "__main__":
    main()
