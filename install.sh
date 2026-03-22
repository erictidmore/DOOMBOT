#!/bin/bash
# ============================================================
#  DOOM INSTALLER
#  Sovereign Multi-Agent Framework
# ============================================================
#
#  One-liner:
#    curl -sL https://raw.githubusercontent.com/erictidmore/DOOMBOT/main/install.sh | bash
#
#  Or clone first:
#    git clone https://github.com/erictidmore/DOOMBOT.git
#    cd DOOMBOT && ./install.sh
#
# ============================================================

set -e

REPO="https://github.com/erictidmore/DOOMBOT.git"
PYTHON="python3"

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║         DOOM INSTALLER               ║"
echo "  ║   Sovereign Multi-Agent Framework    ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# ── 0. Clone if running via curl pipe ──
# Detect: if we're not inside a DOOMBOT repo, clone it first
if [ ! -f "dm.py" ] && [ ! -f "$(dirname "$0")/dm.py" ]; then
    echo "  [0/7] Cloning DOOMBOT..."

    if ! command -v git &>/dev/null; then
        echo "  ERROR: git not found. Install git first."
        exit 1
    fi

    INSTALL_DIR="${DOOM_INSTALL_DIR:-$HOME/DOOMBOT}"

    if [ -d "$INSTALL_DIR/.git" ]; then
        echo "  $INSTALL_DIR already exists — pulling latest..."
        cd "$INSTALL_DIR"
        git pull --ff-only 2>/dev/null || true
    else
        git clone "$REPO" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
        echo "  Cloned to $INSTALL_DIR"
    fi

    DOOM_DIR="$INSTALL_DIR"
else
    # Running from inside the repo
    DOOM_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || pwd)"
fi

echo ""

# ── 1. Check Python ──
echo "  [1/7] Checking Python..."
if ! command -v $PYTHON &>/dev/null; then
    echo ""
    echo "  ERROR: Python 3 not found."
    echo ""
    echo "  Install it:"
    echo "    macOS:  brew install python3"
    echo "    Ubuntu: sudo apt install python3 python3-venv"
    echo "    Windows: https://python.org/downloads"
    echo ""
    exit 1
fi
PY_VERSION=$($PYTHON --version 2>&1 | awk '{print $2}')
echo "  Python $PY_VERSION"

# ── 2. Check Node/npm (for Claude CLI) ──
echo "  [2/7] Checking Node.js..."
if ! command -v npm &>/dev/null; then
    echo ""
    echo "  WARNING: npm not found. You'll need it to install Claude CLI."
    echo "  Install Node.js: https://nodejs.org"
    echo ""
else
    echo "  npm $(npm --version 2>/dev/null)"
fi

# ── 3. Check Claude CLI ──
echo "  [3/7] Checking Claude CLI..."
CLAUDE_PATH=""
for candidate in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" "/usr/local/bin/claude" "$(which claude 2>/dev/null)"; do
    if [ -n "$candidate" ] && [ -f "$candidate" ]; then
        CLAUDE_PATH="$candidate"
        break
    fi
done

if [ -z "$CLAUDE_PATH" ]; then
    echo ""
    echo "  Claude CLI not found."
    echo "  DOOM requires Claude Code (Anthropic's CLI)."
    echo ""
    echo "  Install it:"
    echo "    npm install -g @anthropic-ai/claude-code"
    echo ""
    echo "  Then re-run this installer, or continue without it."
    echo "  (DOOM will work but cannot spawn autonomous bots)"
    echo ""
    echo "  Press Enter to continue anyway, or Ctrl-C to install Claude first..."
    read -r
else
    echo "  Claude CLI: $CLAUDE_PATH"

    # ── 4. Check Claude auth ──
    echo "  [4/7] Checking Claude authentication..."
    if $CLAUDE_PATH --version &>/dev/null; then
        echo "  Claude CLI is responding."
        echo ""
        echo "  If you haven't logged in yet, run:"
        echo "    claude login"
        echo ""
        echo "  DOOM requires a Claude Max plan for autonomous bot spawning."
        echo "  Press Enter to continue (or Ctrl-C to log in first)..."
        read -r
    else
        echo "  WARNING: Claude CLI found but not responding."
        echo "  Run 'claude login' after installation."
    fi
fi

# ── 5. Create virtual environment ──
echo "  [5/7] Setting up Python environment..."
if [ ! -d "$DOOM_DIR/.venv" ]; then
    $PYTHON -m venv "$DOOM_DIR/.venv"
    echo "  Created .venv/"
else
    echo "  .venv/ already exists"
fi

source "$DOOM_DIR/.venv/bin/activate"
pip install -q -r "$DOOM_DIR/requirements.txt"
echo "  Dependencies installed (Flask, requests)"

# ── 6. Initialize database ──
echo "  [6/7] Initializing memory.db..."
if [ -f "$DOOM_DIR/memory.db" ]; then
    echo "  memory.db already exists — skipping"
else
    "$DOOM_DIR/.venv/bin/python" "$DOOM_DIR/init_db.py"
fi

# ── 7. Configure API keys (.env) ──
echo "  [7/8] Configuring API keys..."
ENV_FILE="$DOOM_DIR/.env"

if [ -f "$ENV_FILE" ]; then
    echo "  .env already exists — skipping"
else
    echo ""
    echo "  DOOM supports optional API integrations."
    echo "  Leave blank to skip — you can add keys later to .env"
    echo ""

    echo -n "  ElevenLabs API key (for DOOM voice): "
    read -r ELEVENLABS_KEY

    # Write .env
    cat > "$ENV_FILE" << ENVEOF
# DOOM Environment Variables
# Add your API keys here. This file is gitignored — never committed.
# Add any project-specific keys as you build.

# ElevenLabs — Text-to-speech for DOOM voice
ELEVENLABS_API_KEY=${ELEVENLABS_KEY}
ENVEOF

    if [ -n "$ELEVENLABS_KEY" ]; then
        echo "  .env created with your key"
    else
        echo "  .env created (add keys later)"
    fi
fi

# ── 8. Setup directories and permissions ──
echo "  [8/8] Setting up directories..."
mkdir -p "$DOOM_DIR/logs"
mkdir -p "$DOOM_DIR/projects"

chmod +x "$DOOM_DIR/start.sh" 2>/dev/null || true
chmod +x "$DOOM_DIR/spawn.sh" 2>/dev/null || true
chmod +x "$DOOM_DIR/install.sh" 2>/dev/null || true

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║        DOOM IS INSTALLED             ║"
echo "  ╚══════════════════════════════════════╝"
echo ""
echo "  Location: $DOOM_DIR"
echo ""
echo "  Start DOOM:"
echo "    cd $DOOM_DIR && ./start.sh"
echo ""
echo "  War Room:"
echo "    http://localhost:5050/desktop"
echo "    http://localhost:5050/mobile"
echo ""
echo "  CLI:"
echo "    cd $DOOM_DIR"
echo "    source .venv/bin/activate"
echo "    python dm.py wake"
echo ""
echo "  One-liner start:"
echo "    cd $DOOM_DIR && ./start.sh"
echo ""
echo "  DOOM awaits your first decree."
echo ""
