#!/bin/bash
# =============================================================================
# DOOM — Spawn Mechanism
# Launches a Doom Bot with identity and decree context injected.
#
# Usage: ./spawn.sh <BOT_ID> <DECREE_ID>
# Example: ./spawn.sh DOOM-BOT-V DC-0001c
#
# Decree: DC-0001c — Build the spawn mechanism
# =============================================================================

set -euo pipefail

DOOMBOT_DIR="$(cd "$(dirname "$0")" && pwd)"
DB_PATH="$DOOMBOT_DIR/memory.db"
TEMPLATE_PATH="$DOOMBOT_DIR/DOOM_BOT.md"
VENV_PATH="$DOOMBOT_DIR/.venv"
DM="$VENV_PATH/bin/python $DOOMBOT_DIR/dm.py"

# --- Argument validation ---
if [ $# -lt 2 ]; then
    echo "[DOOM] ERROR: Missing arguments."
    echo "Usage: ./spawn.sh <BOT_ID> <DECREE_ID>"
    echo "Example: ./spawn.sh DOOM-BOT-V DC-0001c"
    exit 1
fi

BOT_ID="$1"
DECREE_ID="$2"

echo "=============================================="
echo " DOOM — SPAWNING $BOT_ID"
echo " Decree: $DECREE_ID"
echo "=============================================="

# --- Verify prerequisites ---
if [ ! -f "$DB_PATH" ]; then
    echo "[DOOM] ERROR: memory.db not found at $DB_PATH"
    echo "[DOOM] Run init_db.py first."
    exit 1
fi

if [ ! -f "$TEMPLATE_PATH" ]; then
    echo "[DOOM] ERROR: DOOM_BOT.md template not found at $TEMPLATE_PATH"
    exit 1
fi

if [ ! -d "$VENV_PATH" ]; then
    echo "[DOOM] ERROR: Virtual environment not found at $VENV_PATH"
    echo "[DOOM] Run: python3 -m venv $VENV_PATH"
    exit 1
fi

# --- Check if dm.py exists ---
if [ ! -f "$DOOMBOT_DIR/dm.py" ]; then
    echo "[DOOM] WARNING: dm.py not found. Using direct SQLite queries as fallback."
    USE_DM=false
else
    USE_DM=true
fi

# --- Look up decree details ---
echo "[DOOM] Looking up decree $DECREE_ID..."

if [ "$USE_DM" = true ]; then
    # Use dm.py to get decree info
    DECREE_INFO=$($DM decree list --status all 2>/dev/null | grep -i "$DECREE_ID" || true)
else
    # Fallback: query SQLite directly
    DECREE_INFO=$("$VENV_PATH/bin/python" -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
cursor.execute('SELECT id, title, description, status FROM decrees WHERE id = ?', ('$DECREE_ID',))
row = cursor.fetchone()
if row:
    print(f'{row[0]} | {row[1]} | {row[2]} | status={row[3]}')
else:
    # Try case-insensitive match
    cursor.execute('SELECT id, title, description, status FROM decrees WHERE LOWER(id) = LOWER(?)', ('$DECREE_ID',))
    row = cursor.fetchone()
    if row:
        print(f'{row[0]} | {row[1]} | {row[2]} | status={row[3]}')
conn.close()
" 2>/dev/null || true)
fi

if [ -z "$DECREE_INFO" ]; then
    echo "[DOOM] ERROR: Decree $DECREE_ID not found in memory.db"
    exit 1
fi

echo "[DOOM] Decree found: $DECREE_INFO"

# --- Extract decree title and description ---
DECREE_TITLE=$("$VENV_PATH/bin/python" -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
cursor.execute('SELECT title FROM decrees WHERE id = ? OR LOWER(id) = LOWER(?)', ('$DECREE_ID', '$DECREE_ID'))
row = cursor.fetchone()
print(row[0] if row else 'Unknown Decree')
conn.close()
")

DECREE_DESCRIPTION=$("$VENV_PATH/bin/python" -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
cursor.execute('SELECT description FROM decrees WHERE id = ? OR LOWER(id) = LOWER(?)', ('$DECREE_ID', '$DECREE_ID'))
row = cursor.fetchone()
print(row[0] if row else 'No description available.')
conn.close()
")

echo "[DOOM] Title: $DECREE_TITLE"

# --- Recall relevant archives ---
echo "[DOOM] Loading archives..."

if [ "$USE_DM" = true ]; then
    ARCHIVES=$($DM archive recall 2>/dev/null || echo "No archives available.")
else
    ARCHIVES=$("$VENV_PATH/bin/python" -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
cursor.execute('SELECT topic, content FROM archives ORDER BY importance ASC, created_at DESC LIMIT 10')
rows = cursor.fetchall()
if rows:
    for row in rows:
        print(f'### {row[0]}')
        print(row[1])
        print()
else:
    print('No archives available yet. You are among the first.')
conn.close()
" 2>/dev/null || echo "No archives available.")
fi

# --- Register the bot in memory.db ---
echo "[DOOM] Registering $BOT_ID..."

if [ "$USE_DM" = true ]; then
    $DM bot spawn "$BOT_ID" --decree "$DECREE_ID" 2>/dev/null || true
else
    "$VENV_PATH/bin/python" -c "
import sqlite3
from datetime import datetime
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
now = datetime.now().isoformat()
cursor.execute('''INSERT OR REPLACE INTO agents (id, type, status, current_decree, context_pct, spawned_at, last_active, notes)
                  VALUES (?, 'doom_bot', 'active', ?, 0, ?, ?, 'Spawned via spawn.sh')''',
               ('$BOT_ID', '$DECREE_ID', now, now))
conn.commit()
conn.close()
print('[DOOM] Bot registered in agents table.')
" 2>/dev/null || echo "[DOOM] WARNING: Failed to register bot."
fi

# --- Claim the decree ---
echo "[DOOM] Claiming decree $DECREE_ID for $BOT_ID..."

if [ "$USE_DM" = true ]; then
    $DM decree claim "$DECREE_ID" --bot "$BOT_ID" 2>/dev/null || true
else
    "$VENV_PATH/bin/python" -c "
import sqlite3
from datetime import datetime
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
now = datetime.now().isoformat()
cursor.execute('''UPDATE decrees SET status = 'active', assigned_to = ?, updated_at = ?
                  WHERE id = ? OR LOWER(id) = LOWER(?)''',
               ('$BOT_ID', now, '$DECREE_ID', '$DECREE_ID'))
conn.commit()
conn.close()
print('[DOOM] Decree claimed.')
" 2>/dev/null || echo "[DOOM] WARNING: Failed to claim decree."
fi

# --- Log spawn event to chronicle ---
echo "[DOOM] Logging spawn to chronicle..."

if [ "$USE_DM" = true ]; then
    $DM chronicle log --event-type "spawn" --content "$BOT_ID spawned and assigned $DECREE_ID: $DECREE_TITLE" --agent-id "DOOM" 2>/dev/null || true
else
    "$VENV_PATH/bin/python" -c "
import sqlite3
from datetime import datetime
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
# Find current session
cursor.execute(\"SELECT id FROM sessions WHERE status = 'open' ORDER BY session_number DESC LIMIT 1\")
session_row = cursor.fetchone()
session_id = session_row[0] if session_row else 'session-001'
cursor.execute('''INSERT INTO chronicle (session_id, event_type, agent_id, content, timestamp)
                  VALUES (?, 'spawn', 'DOOM', ?, ?)''',
               (session_id, '$BOT_ID spawned and assigned $DECREE_ID: $DECREE_TITLE', datetime.now().isoformat()))
conn.commit()
conn.close()
print('[DOOM] Spawn logged to chronicle.')
" 2>/dev/null || echo "[DOOM] WARNING: Failed to log to chronicle."
fi

# --- Build context from DOOM_BOT.md template ---
echo "[DOOM] Building context block..."

CONTEXT=$(cat "$TEMPLATE_PATH")
CONTEXT="${CONTEXT//\{BOT_ID\}/$BOT_ID}"
CONTEXT="${CONTEXT//\{DECREE_TITLE\}/$DECREE_TITLE}"
CONTEXT="${CONTEXT//\{DECREE_DESCRIPTION\}/$DECREE_DESCRIPTION}"
CONTEXT="${CONTEXT//\{DECREE_ID\}/$DECREE_ID}"
CONTEXT="${CONTEXT//\{ARCHIVES\}/$ARCHIVES}"

echo ""
echo "=============================================="
echo " CONTEXT BLOCK PREPARED"
echo "=============================================="
echo "$CONTEXT"
echo "=============================================="
echo ""

# --- Create project directory ---
PROJECT_DIR="$DOOMBOT_DIR/projects/$DECREE_ID"
mkdir -p "$PROJECT_DIR"
echo "[DOOM] Project directory: $PROJECT_DIR"

# --- Launch Claude Code session ---
echo "[DOOM] Launching Claude Code session for $BOT_ID..."
echo ""

cd "$PROJECT_DIR"
claude --print "$CONTEXT"

echo ""
echo "=============================================="
echo " $BOT_ID SESSION COMPLETE"
echo "=============================================="
