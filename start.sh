#!/bin/bash
# DOOM Launcher — One command. Everything daemonized. Terminal walks away.
#
# Usage:
#   ./start.sh          Start DOOM (server + worker as background daemons)
#   ./start.sh stop     Kill all DOOM processes
#   ./start.sh status   Check if DOOM is running
#   ./start.sh restart  Stop then start

DOOM_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$DOOM_DIR/.venv/bin/python"
LOG_DIR="$DOOM_DIR/logs"
SERVER_PID="$LOG_DIR/server.pid"
WORKER_PID="$LOG_DIR/worker.pid"
WATCHTOWER_PID="$LOG_DIR/watchtower.pid"
INTROSPECT_PID="$LOG_DIR/introspect.pid"
SCHEDULER_PID="$LOG_DIR/scheduler.pid"
HEALTHMON_PID="$LOG_DIR/healthmon.pid"

mkdir -p "$LOG_DIR"

doom_stop() {
    # Auto-close session before killing processes
    "$PYTHON" "$DOOM_DIR/dm.py" session close 2>/dev/null || true

    local killed=0
    for pidfile in "$SERVER_PID" "$WORKER_PID" "$WATCHTOWER_PID" "$INTROSPECT_PID" "$SCHEDULER_PID" "$HEALTHMON_PID"; do
        if [ -f "$pidfile" ]; then
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null
                killed=1
            fi
            rm -f "$pidfile"
        fi
    done
    # Sweep any orphans — kill watchdog bash shells AND python processes
    pkill -f "python.*server\.py" 2>/dev/null
    pkill -f "python.*worker\.py" 2>/dev/null
    pkill -f "python.*watchtower\.py" 2>/dev/null
    pkill -f "python.*introspect\.py" 2>/dev/null
    pkill -f "doom_healthmon" 2>/dev/null
    pkill -f "python.*healthmon\.py" 2>/dev/null
    # Kill any lingering watchdog bash shells that respawn the server
    pkill -f "WATCHDOG.*Server crashed" 2>/dev/null
    pkill -f "nohup bash.*server\.py" 2>/dev/null
    if [ "$killed" -eq 1 ]; then
        echo "  DOOM processes terminated."
    else
        echo "  No DOOM processes found."
    fi
    # Verify port is freed
    sleep 1
    if lsof -i :5050 -sTCP:LISTEN > /dev/null 2>&1; then
        echo "  WARNING: Port 5050 still in use after stop. Force killing..."
        lsof -i :5050 -sTCP:LISTEN -t | xargs kill -9 2>/dev/null
    fi
}

doom_status() {
    local running=0
    if [ -f "$SERVER_PID" ] && kill -0 "$(cat "$SERVER_PID")" 2>/dev/null; then
        echo "  Server: RUNNING (PID $(cat "$SERVER_PID"))"
        running=1
    else
        echo "  Server: STOPPED"
    fi
    if [ -f "$WORKER_PID" ] && kill -0 "$(cat "$WORKER_PID")" 2>/dev/null; then
        echo "  Worker: RUNNING (PID $(cat "$WORKER_PID"))"
        running=1
    else
        echo "  Worker: STOPPED"
    fi
    if [ -f "$WATCHTOWER_PID" ] && kill -0 "$(cat "$WATCHTOWER_PID")" 2>/dev/null; then
        echo "  Watchtower: RUNNING (PID $(cat "$WATCHTOWER_PID"))"
        running=1
    else
        echo "  Watchtower: STOPPED"
    fi
    if [ -f "$INTROSPECT_PID" ] && kill -0 "$(cat "$INTROSPECT_PID")" 2>/dev/null; then
        echo "  Introspect: RUNNING (PID $(cat "$INTROSPECT_PID"))"
        running=1
    else
        echo "  Introspect: STOPPED"
    fi
    if [ -f "$HEALTHMON_PID" ] && kill -0 "$(cat "$HEALTHMON_PID")" 2>/dev/null; then
        echo "  HealthMon: RUNNING (PID $(cat "$HEALTHMON_PID"))"
        running=1
    else
        echo "  HealthMon: STOPPED"
    fi
    return $running
}

doom_healthmon() {
    # Process health monitor — Python daemon, checks every 30s
    "$PYTHON" -u "$DOOM_DIR/healthmon.py"
}

doom_start() {
    # Pre-flight checks
    if [ ! -d "$DOOM_DIR/.venv" ]; then
        echo "  ERROR: Python venv not found. Run setup first:"
        echo "    python3 -m venv .venv"
        echo "    source .venv/bin/activate"
        echo "    pip install -r requirements.txt"
        exit 1
    fi
    if [ ! -f "$PYTHON" ]; then
        echo "  ERROR: Python not found at $PYTHON"
        exit 1
    fi
    if ! "$PYTHON" -c "import flask" 2>/dev/null; then
        echo "  ERROR: Flask not installed. Run:"
        echo "    source .venv/bin/activate && pip install -r requirements.txt"
        exit 1
    fi
    if [ ! -f "$DOOM_DIR/memory.db" ]; then
        echo "  No memory.db found — initializing fresh database..."
        "$PYTHON" "$DOOM_DIR/init_db.py"
    fi

    # Stale PID cleanup: remove pidfiles whose processes no longer exist
    for pidfile in "$SERVER_PID" "$WORKER_PID" "$WATCHTOWER_PID" "$INTROSPECT_PID" "$SCHEDULER_PID" "$HEALTHMON_PID"; do
        if [ -f "$pidfile" ]; then
            stale_pid=$(cat "$pidfile")
            if ! kill -0 "$stale_pid" 2>/dev/null; then
                echo "  Stale PID detected ($stale_pid in $(basename "$pidfile")) — removing pidfile."
                rm -f "$pidfile"
            fi
        fi
    done

    # Check if already running (after stale cleanup)
    if [ -f "$SERVER_PID" ] && kill -0 "$(cat "$SERVER_PID")" 2>/dev/null; then
        echo "  DOOM is already running. Use './start.sh restart' to restart."
        doom_status
        return
    fi

    # Log rotation: if logs exceed 10MB, rotate to .log.old
    for logfile in "$LOG_DIR/server.log" "$LOG_DIR/worker.log" "$LOG_DIR/watchtower.log" "$LOG_DIR/introspect.log" "$LOG_DIR/healthmon.log"; do
        if [ -f "$logfile" ]; then
            size=$(stat -f%z "$logfile" 2>/dev/null || stat --printf="%s" "$logfile" 2>/dev/null || echo 0)
            if [ "$size" -gt 10485760 ]; then
                mv "$logfile" "${logfile}.old"
                echo "  Rotated $(basename "$logfile") (was ${size} bytes)"
            fi
        fi
    done

    echo ""
    echo "  ╔══════════════════════════════════════╗"
    echo "  ║           DOOM AWAKENS               ║"
    echo "  ╚══════════════════════════════════════╝"
    echo ""

    # Clear port 5050 if occupied (auto-kill leftover processes instead of failing)
    if lsof -i :5050 -sTCP:LISTEN > /dev/null 2>&1; then
        echo "  Port 5050 occupied — clearing leftover processes..."
        lsof -i :5050 -sTCP:LISTEN -t | xargs kill -9 2>/dev/null
        sleep 1
        if lsof -i :5050 -sTCP:LISTEN > /dev/null 2>&1; then
            echo "  ERROR: Could not free port 5050"
            exit 1
        fi
        echo "  Port 5050 cleared."
    fi

    # Launch server inside watchdog loop (auto-respawn on crash with backoff)
    nohup bash -c '
        backoff=2
        max_backoff=30
        respawn_count=0
        while true; do
            # Clear port before each attempt
            port_pid=$(lsof -i :5050 -sTCP:LISTEN -t 2>/dev/null)
            if [ -n "$port_pid" ]; then
                echo "[WATCHDOG] Killing leftover PID $port_pid on :5050"
                kill -9 $port_pid 2>/dev/null
                sleep 0.5
            fi

            "'"$PYTHON"'" -u "'"$DOOM_DIR"'/server.py"
            exit_code=$?

            # Clean shutdown signals (SIGTERM=143, SIGINT=130) — do not respawn
            if [ $exit_code -eq 143 ] || [ $exit_code -eq 130 ]; then
                echo "[WATCHDOG] Server stopped cleanly (exit $exit_code)."
                break
            fi

            # Stop requested externally (PID file removed by doom_stop)
            if [ ! -f "'"$SERVER_PID"'" ]; then
                echo "[WATCHDOG] PID file removed — shutdown requested."
                break
            fi

            respawn_count=$((respawn_count + 1))
            echo "[WATCHDOG] Server crashed (exit $exit_code). Respawn #$respawn_count in ${backoff}s..."
            sleep "$backoff"

            # Exponential backoff capped at max_backoff
            backoff=$((backoff * 2))
            if [ "$backoff" -gt "$max_backoff" ]; then
                backoff=$max_backoff
            fi
        done
        echo "[WATCHDOG] Server watchdog exiting after $respawn_count respawns."
    ' >> "$LOG_DIR/server.log" 2>&1 &
    echo $! > "$SERVER_PID"
    echo "  Server: PID $! → 0.0.0.0:5050 (watchdog-protected, auto-respawn)"

    # Validate watchdog process is alive
    if ! kill -0 $! 2>/dev/null; then
        echo "  ERROR: Server watchdog died immediately"
        tail -20 "$LOG_DIR/server.log"
        exit 1
    fi

    # Wait for server to be ready (max 10 seconds)
    echo "  Waiting for server..."
    for i in $(seq 1 20); do
        if curl -s --max-time 5 http://localhost:5050/ > /dev/null 2>&1; then
            break
        fi
        if [ $i -eq 20 ]; then
            echo "  ERROR: Server failed to start on port 5050"
            tail -20 "$LOG_DIR/server.log"
            exit 1
        fi
        sleep 0.5
    done

    # Launch worker as daemon (unbuffered output for real-time logs)
    nohup "$PYTHON" -u "$DOOM_DIR/worker.py" \
        >> "$LOG_DIR/worker.log" 2>&1 &
    echo $! > "$WORKER_PID"
    echo "  Worker: PID $!"

    # Validate worker process is alive
    if ! kill -0 $! 2>/dev/null; then
        echo "  ERROR: Worker process died immediately"
        tail -20 "$LOG_DIR/worker.log"
        exit 1
    fi

    # Launch watchtower as daemon (5-minute health checks, unbuffered)
    nohup "$PYTHON" -u "$DOOM_DIR/watchtower.py" \
        >> "$LOG_DIR/watchtower.log" 2>&1 &
    echo $! > "$WATCHTOWER_PID"
    echo "  Watchtower: PID $! (checks every 5m)"

    # Validate watchtower process is alive
    if ! kill -0 $! 2>/dev/null; then
        echo "  WARNING: Watchtower process died immediately (non-fatal)"
        tail -10 "$LOG_DIR/watchtower.log"
    fi

    # Launch introspect as daemon (self-improvement every 2 hours, unbuffered)
    nohup "$PYTHON" -u "$DOOM_DIR/introspect.py" \
        >> "$LOG_DIR/introspect.log" 2>&1 &
    echo $! > "$INTROSPECT_PID"
    echo "  Introspect: PID $! (self-improvement every 2h)"

    # Validate introspect process is alive
    if ! kill -0 $! 2>/dev/null; then
        echo "  WARNING: Introspect process died immediately (non-fatal)"
        tail -10 "$LOG_DIR/introspect.log"
    fi

    # Launch scheduler as daemon (checks scheduled decrees every 60s)
    nohup "$PYTHON" -u "$DOOM_DIR/scheduler.py" \
        >> "$LOG_DIR/scheduler.log" 2>&1 &
    SCHEDULER_PID_VAL=$!
    echo $SCHEDULER_PID_VAL > "$LOG_DIR/scheduler.pid"
    echo "  Scheduler: PID $SCHEDULER_PID_VAL (cron decrees every 60s)"

    # Launch health monitor as daemon (checks server/worker every 30s, auto-restarts)
    doom_healthmon >> "$LOG_DIR/healthmon.log" 2>&1 &
    echo $! > "$HEALTHMON_PID"
    echo "  HealthMon: PID $! (health checks every 30s)"

    echo ""

    # Detect Tailscale IP
    TAILSCALE_IP=""
    if command -v tailscale &>/dev/null; then
        TAILSCALE_IP=$(tailscale ip -4 2>/dev/null | head -1)
    fi
    if [ -z "$TAILSCALE_IP" ]; then
        # Fallback: look for 100.x Tailscale IP in ifconfig
        TAILSCALE_IP=$(ifconfig 2>/dev/null | grep 'inet 100\.' | awk '{print $2}' | head -1)
    fi

    echo "  Local:     http://localhost:5050/desktop"
    if [ -n "$TAILSCALE_IP" ]; then
        echo "  Tailscale: http://$TAILSCALE_IP:5050/mobile (mobile) | /desktop"
    else
        echo "  Tailscale: (not detected — install Tailscale for remote access)"
    fi
    echo ""
    echo "  Logs:   $LOG_DIR/"
    echo "  Stop:   ./start.sh stop"
    echo ""

    # Auto-open session
    "$PYTHON" "$DOOM_DIR/dm.py" session open 2>/dev/null || true

    echo "  DOOM is operational. Close this terminal."
    echo ""
}

case "${1:-start}" in
    start)   doom_start ;;
    stop)    doom_stop ;;
    status)  doom_status ;;
    restart) doom_stop; sleep 1; doom_start ;;
    *)       echo "Usage: ./start.sh [start|stop|status|restart]" ;;
esac
