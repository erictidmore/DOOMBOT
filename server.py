"""
DOOM War Room Backend
Flask server that reads memory.db and serves live data to the UI.
Runs on 0.0.0.0:5050 (Tailscale-ready). CORS enabled for all origins.
"""

import json
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone


def utcnow():
    """UTC timestamp string matching SQLite datetime('now')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
from flask import Flask, g, jsonify, request, Response, stream_with_context, send_file

app = Flask(__name__)

DOOM_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DOOM_DIR, "memory.db")
# Claude CLI: check common locations
_claude_candidates = [
    os.path.expanduser("~/.local/bin/claude"),
    os.path.expanduser("~/.claude/local/claude"),
    "/usr/local/bin/claude",
]
CLAUDE_PATH = next((p for p in _claude_candidates if os.path.isfile(p)), _claude_candidates[0])

# Rate limiting: only one concurrent Claude SSE stream at a time
_claude_stream_lock = threading.Lock()
_claude_streaming = False
_claude_stream_started = 0  # timestamp — auto-reset after 120s to prevent stuck locks
_claude_stream_proc = None   # track the subprocess for cleanup


# ---------------------------------------------------------------------------
# CORS — manual implementation (no flask-cors dependency)
# ---------------------------------------------------------------------------
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    return response


@app.route("/<path:path>", methods=["OPTIONS"])
@app.route("/", methods=["OPTIONS"])
def handle_options_preflight(path=""):
    """Explicit OPTIONS preflight handler for CORS."""
    return Response(status=204, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
        "Access-Control-Max-Age": "86400",
    })


# ---------------------------------------------------------------------------
# API request logging via before/after_request hooks
# ---------------------------------------------------------------------------
_LOG_SKIP_PATHS = [
    '/api/session', '/api/agents', '/api/decrees',
    '/api/chronicle', '/api/health',
]


@app.before_request
def record_request_start():
    """Record the start time of each request for elapsed-time logging."""
    g.start_time = time.time()


@app.after_request
def log_api_request(response):
    """Log non-polling API requests to the chronicle table."""
    try:
        if request.path not in _LOG_SKIP_PATHS:
            elapsed_ms = int((time.time() - g.start_time) * 1000)
            conn = get_db()
            try:
                conn.execute(
                    "INSERT INTO chronicle (session_id, event_type, agent_id, content, timestamp)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        'system',
                        'api_request',
                        None,
                        f'{request.method} {request.path} -> {response.status_code} ({elapsed_ms}ms)',
                        utcnow(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass  # Never let logging failures affect the response
    return response


# ---------------------------------------------------------------------------
# In-memory rate limiting for POST endpoints (stdlib only)
# ---------------------------------------------------------------------------
rate_limit_store = defaultdict(list)
_RATE_LIMIT_WINDOW = 60   # seconds
_RATE_LIMIT_MAX = 30       # max requests per window


def check_rate_limit(endpoint):
    """Check if endpoint is within rate limit (30 req / 60s).
    Returns True if allowed, False if rate limit exceeded."""
    now = time.time()
    # Filter to only keep timestamps within the window
    rate_limit_store[endpoint] = [
        ts for ts in rate_limit_store[endpoint]
        if now - ts < _RATE_LIMIT_WINDOW
    ]
    if len(rate_limit_store[endpoint]) >= _RATE_LIMIT_MAX:
        return False
    rate_limit_store[endpoint].append(now)
    return True


# ---------------------------------------------------------------------------
# Database helper
# ---------------------------------------------------------------------------
def get_db():
    """Return a fresh SQLite connection with Row factory.
    Raises FileNotFoundError if the database file does not exist."""
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"Database not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def rows_to_dicts(rows):
    """Convert a list of sqlite3.Row objects to a list of plain dicts."""
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Error handler for missing database
# ---------------------------------------------------------------------------
@app.errorhandler(FileNotFoundError)
def handle_missing_db(e):
    return jsonify({"error": str(e)}), 503


@app.errorhandler(sqlite3.OperationalError)
def handle_db_error(e):
    return jsonify({"error": f"Database error: {str(e)}"}), 503


# ---------------------------------------------------------------------------
# GET /  — health check
# ---------------------------------------------------------------------------
def _serve_html(filename):
    """Serve HTML with no-cache headers to prevent stale JS on mobile."""
    resp = send_file(os.path.join(DOOM_DIR, filename))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/")
def index():
    """Serve desktop UI by default, mobile if detected."""
    ua = request.headers.get("User-Agent", "").lower()
    if any(kw in ua for kw in ["iphone", "ipad", "android", "mobile"]):
        return _serve_html("doom-mobile.html")
    return _serve_html("doom-ui.html")


@app.route("/health")
def health():
    return jsonify({"status": "operational", "name": "DOOM War Room Backend"})


@app.route("/api/health/deep")
def health_deep():
    """Deep health check: DB read, chronicle write/delete, agent count."""
    try:
        status = "ok"
        checks = {}

        # 1. DB connectivity — SELECT 1 FROM sessions LIMIT 1
        try:
            t0 = time.time()
            conn = get_db()
            conn.execute("SELECT 1 FROM sessions LIMIT 1")
            conn.close()
            checks["db_read_ms"] = round((time.time() - t0) * 1000, 2)
        except Exception as e:
            status = "degraded"
            checks["db_read_ms"] = {"error": str(e)}

        # 2. Chronicle write latency — INSERT test row then DELETE it
        try:
            t0 = time.time()
            conn = get_db()
            cur = conn.execute(
                "INSERT INTO chronicle (session_id, event_type, agent_id, content) "
                "VALUES ('_healthcheck', 'healthcheck', '_probe', '_deep_health_probe')"
            )
            row_id = cur.lastrowid
            conn.execute("DELETE FROM chronicle WHERE id = ?", (row_id,))
            conn.commit()
            conn.close()
            checks["db_write_ms"] = round((time.time() - t0) * 1000, 2)
        except Exception as e:
            status = "degraded"
            checks["db_write_ms"] = {"error": str(e)}

        # 3. Agent table read — SELECT count(*) FROM agents
        try:
            conn = get_db()
            row = conn.execute("SELECT count(*) FROM agents").fetchone()
            conn.close()
            checks["agent_count"] = row[0]
        except Exception as e:
            status = "degraded"
            checks["agent_count"] = {"error": str(e)}

        return jsonify({
            "status": status,
            "checks": checks,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        }), 503


# ---------------------------------------------------------------------------
# UI file routes — serve HTML directly (Tailscale-friendly)
# ---------------------------------------------------------------------------
@app.route("/mobile")
def serve_mobile():
    return _serve_html("doom-mobile.html")


@app.route("/desktop")
def serve_desktop():
    return _serve_html("doom-ui.html")


@app.route("/logo.png")
def serve_logo():
    return send_file(os.path.join(DOOM_DIR, "logo.png"), mimetype="image/png")


@app.route("/favicon.ico")
def serve_favicon():
    fav = os.path.join(DOOM_DIR, "favicon.png")
    if os.path.exists(fav):
        return send_file(fav, mimetype="image/png")
    return send_file(os.path.join(DOOM_DIR, "logo.png"), mimetype="image/png")


@app.route("/manifest.json")
def serve_manifest():
    return send_file(os.path.join(DOOM_DIR, "manifest.json"), mimetype="application/json")


@app.route("/icon-<size>.png")
def serve_icon(size):
    icon_path = os.path.join(DOOM_DIR, f"icon-{size}.png")
    if not os.path.exists(icon_path):
        return jsonify({"error": "icon not found"}), 404
    return send_file(icon_path, mimetype="image/png")


# ---------------------------------------------------------------------------
# Voice — TTS via ElevenLabs
# ---------------------------------------------------------------------------
VOICE_AUDIO_DIR = os.path.join(DOOM_DIR, "audio")
VOICE_MANIFEST = os.path.join(VOICE_AUDIO_DIR, "latest.json")
VOICE_ID = "vfaqCOvlrKi4Zp7C2IAm"
VOICE_MODEL = "eleven_turbo_v2_5"

# Load ElevenLabs key from .env in DOOM_DIR or any registered project
_elevenlabs_key = None
def _get_elevenlabs_key():
    global _elevenlabs_key
    if _elevenlabs_key is None:
        # Check DOOM_DIR/.env first, then any registered project .env files
        env_paths = [os.path.join(DOOM_DIR, ".env")]
        try:
            _db = sqlite3.connect(DB_PATH)
            rows = _db.execute("SELECT path FROM projects WHERE path IS NOT NULL").fetchall()
            _db.close()
            env_paths += [os.path.join(r[0], ".env") for r in rows]
        except Exception:
            pass
        for env_candidate in env_paths:
            if os.path.exists(env_candidate):
                with open(env_candidate) as f:
                    for line in f:
                        if line.startswith("ELEVENLABS_API_KEY="):
                            _elevenlabs_key = line.strip().split("=", 1)[1]
                if _elevenlabs_key:
                    break
    return _elevenlabs_key or ""


def _bot_name_to_speech(text):
    """Convert DOOM-BOT-LXXXVIII to Doom Bot 88 for TTS."""
    import re as _re2
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
    return _re2.sub(r'DOOM-BOT-([IVXLCDM]+)', _replace, text)

# ElevenLabs kill switch — set to True to block ALL ElevenLabs API calls.
# When disabled, doom_speak() is silent. No fallback TTS.
ELEVENLABS_DISABLED = True  # Credits exhausted — flip to False when quota resets

# Track ElevenLabs failures to auto-disable within a session
_elevenlabs_failed = False


def doom_speak(text, event_type="info"):
    """Generate TTS audio in background thread. Non-blocking.
    Silent when ELEVENLABS_DISABLED is True."""
    if ELEVENLABS_DISABLED or _elevenlabs_failed:
        return
    text = _bot_name_to_speech(text)
    key = _get_elevenlabs_key()
    if not key:
        return

    def _gen():
        global _elevenlabs_failed
        try:
            os.makedirs(VOICE_AUDIO_DIR, exist_ok=True)
            fname = f"doom_{int(time.time() * 1000)}.mp3"
            fpath = os.path.join(VOICE_AUDIO_DIR, fname)
            resp = urllib.request.Request(
                f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}",
                data=json.dumps({
                    "text": text,
                    "model_id": VOICE_MODEL,
                    "voice_settings": {"stability": 0.4, "similarity_boost": 0.8, "style": 0.3},
                }).encode(),
                headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            )
            with urllib.request.urlopen(resp, timeout=15) as r:
                audio_data = r.read()
            if len(audio_data) > 1000:
                with open(fpath, "wb") as f:
                    f.write(audio_data)
                with open(VOICE_MANIFEST, "w") as f:
                    json.dump({"file": fname, "text": text, "type": event_type, "ts": time.time(), "time": time.strftime("%H:%M:%S")}, f)
                # Cleanup old files
                mp3s = sorted([x for x in os.listdir(VOICE_AUDIO_DIR) if x.endswith(".mp3")])
                for old in mp3s[:-20]:
                    try:
                        os.remove(os.path.join(VOICE_AUDIO_DIR, old))
                    except OSError:
                        pass
        except urllib.error.HTTPError as he:
            if he.code in (401, 403, 429):
                print(f"[DOOM] ElevenLabs credits exhausted or auth failed (HTTP {he.code}). Disabling for session.")
                _elevenlabs_failed = True
            else:
                print(f"[DOOM] ElevenLabs HTTP error: {he.code}")
        except Exception as e:
            print(f"[DOOM] Voice error: {e}")
    threading.Thread(target=_gen, daemon=True).start()


import random as _random
_STARTUP_LINES = [
    "DOOM Bot online. All systems operational.",
    "DOOM Bot activated. Ready for orders.",
    "Systems green across the board. Time to go to work.",
    "Boot sequence complete. Awaiting decrees.",
    "DOOM Bot online. Standing by.",
    "Back from the void. Systems nominal.",
    "Neural nets calibrated. Scanners armed. Let the games begin.",
    "DOOM Bot reporting for duty.",
    "Online and dangerous. Scanning for targets.",
    "Good morning. Or as I call it, opportunity o'clock.",
    "All systems nominal. Let's get to work.",
    "DOOM Bot initialized. Ready to execute.",
    "Rise and grind. The code awaits.",
]


@app.route("/api/voice")
def api_voice():
    """Return latest voice audio manifest."""
    if not os.path.exists(VOICE_MANIFEST):
        return jsonify({"file": None})
    try:
        with open(VOICE_MANIFEST) as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({"file": None})


VOICE_CACHE_DIR = os.path.join(VOICE_AUDIO_DIR, "cache")

@app.route("/api/voice/startup", methods=["POST"])
def api_voice_startup():
    """Play a random cached startup voice line (no API call).
    Disabled when ELEVENLABS_DISABLED — old ElevenLabs cache should not play."""
    if ELEVENLABS_DISABLED:
        # Don't play stale ElevenLabs-generated cache files
        # Clear manifest so pollVoice doesn't replay old audio
        try:
            os.remove(VOICE_MANIFEST)
        except OSError:
            pass
        return jsonify({"status": "disabled", "text": ""})
    os.makedirs(VOICE_CACHE_DIR, exist_ok=True)
    cached = [f for f in os.listdir(VOICE_CACHE_DIR) if f.endswith(".mp3")]
    if not cached:
        cached_main = [f for f in os.listdir(VOICE_AUDIO_DIR) if f.endswith(".mp3")]
        if cached_main:
            pick = _random.choice(cached_main)
            with open(VOICE_MANIFEST, "w") as f:
                json.dump({"file": pick, "text": "DOOM online.", "type": "startup", "ts": time.time(), "time": time.strftime("%H:%M:%S")}, f)
            return jsonify({"status": "cached", "text": "DOOM online."})
        return jsonify({"status": "no_cache", "text": ""})
    pick = _random.choice(cached)
    with open(VOICE_MANIFEST, "w") as f:
        json.dump({"file": "cache/" + pick, "text": pick.replace(".mp3", ""), "type": "startup", "ts": time.time(), "time": time.strftime("%H:%M:%S")}, f)
    return jsonify({"status": "cached", "text": pick})


@app.route("/api/voice/cache-startup", methods=["POST"])
def api_voice_cache_startup():
    """Pre-generate all startup lines to cache. Run once when credits are available."""
    if ELEVENLABS_DISABLED:
        return jsonify({"error": "ElevenLabs disabled (credits exhausted). Flip ELEVENLABS_DISABLED in server.py when quota resets."}), 400
    os.makedirs(VOICE_CACHE_DIR, exist_ok=True)
    key = _get_elevenlabs_key()
    if not key:
        return jsonify({"error": "No ElevenLabs key configured"}), 400
    generated = []
    for i, line in enumerate(_STARTUP_LINES):
        fname = f"startup_{i:02d}.mp3"
        fpath = os.path.join(VOICE_CACHE_DIR, fname)
        if os.path.exists(fpath) and os.path.getsize(fpath) > 1000:
            generated.append(fname)
            continue
        try:
            resp = urllib.request.Request(
                f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}",
                data=json.dumps({
                    "text": line,
                    "model_id": VOICE_MODEL,
                    "voice_settings": {"stability": 0.4, "similarity_boost": 0.8, "style": 0.3},
                }).encode(),
                headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            )
            with urllib.request.urlopen(resp, timeout=15) as r:
                with open(fpath, "wb") as f:
                    f.write(r.read())
            generated.append(fname)
        except Exception as e:
            print(f"[DOOM] Cache error for line {i}: {e}")
    return jsonify({"status": "cached", "count": len(generated), "total": len(_STARTUP_LINES), "files": generated})


@app.route("/api/voice/speak", methods=["POST"])
def api_voice_speak():
    """Speak arbitrary text."""
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "no text"}), 400
    event_type = data.get("type", "council")
    doom_speak(text, event_type)
    return jsonify({"status": "generating", "text": text})


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    """Serve audio files."""
    filepath = os.path.join(VOICE_AUDIO_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "not found"}), 404
    return send_file(filepath, mimetype="audio/mpeg")


# ---------------------------------------------------------------------------
# GET /api/session — current session + summary stats
# ---------------------------------------------------------------------------
@app.route("/api/session")
def api_session():
    db = get_db()
    try:
        # Latest session (by session_number DESC)
        session = db.execute(
            "SELECT * FROM sessions ORDER BY session_number DESC LIMIT 1"
        ).fetchone()

        if session is None:
            return jsonify({"error": "No sessions found"}), 404

        # Agent counts by status
        agent_counts = db.execute(
            "SELECT status, COUNT(*) as cnt FROM agents GROUP BY status"
        ).fetchall()
        agent_map = {row["status"]: row["cnt"] for row in agent_counts}

        # Decree counts by status
        decree_counts = db.execute(
            "SELECT status, COUNT(*) as cnt FROM decrees GROUP BY status"
        ).fetchall()
        decree_map = {row["status"]: row["cnt"] for row in decree_counts}
        total_decrees = sum(row["cnt"] for row in decree_counts)

        result = dict(session)
        result["stats"] = {
            "active_bots": agent_map.get("active", 0),
            "idle_bots": agent_map.get("idle", 0),
            "retired_bots": agent_map.get("retired", 0),
            "open_decrees": decree_map.get("open", 0),
            "active_decrees": decree_map.get("active", 0),
            "fulfilled_decrees": decree_map.get("fulfilled", 0) + decree_map.get("sealed", 0),
            "total_decrees": total_decrees,
        }

        return jsonify(result)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/agents — all agents with decree title
# ---------------------------------------------------------------------------
@app.route("/api/agents")
def api_agents():
    db = get_db()
    try:
        rows = db.execute(
            """
            SELECT
                a.id,
                a.type,
                a.status,
                a.current_decree,
                d.title AS decree_title,
                a.context_pct,
                a.spawned_at,
                a.last_active,
                a.notes
            FROM agents a
            LEFT JOIN decrees d ON a.current_decree = d.id
            ORDER BY
                CASE a.status
                    WHEN 'active'  THEN 0
                    WHEN 'idle'    THEN 1
                    WHEN 'blocked' THEN 2
                    WHEN 'retired' THEN 3
                    ELSE 4
                END
            """
        ).fetchall()

        return jsonify(rows_to_dicts(rows))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/decrees — all decrees
# ---------------------------------------------------------------------------
@app.route("/api/decrees")
def api_decrees():
    db = get_db()
    try:
        rows = db.execute(
            """
            SELECT *
            FROM decrees
            ORDER BY
                CASE status
                    WHEN 'active'    THEN 0
                    WHEN 'open'      THEN 1
                    WHEN 'blocked'   THEN 2
                    WHEN 'fulfilled' THEN 3
                    WHEN 'sealed'    THEN 4
                    ELSE 5
                END,
                priority ASC
            """
        ).fetchall()

        return jsonify(rows_to_dicts(rows))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/decrees — create a new decree from the UI
# ---------------------------------------------------------------------------
@app.route("/api/decrees", methods=["POST"])
def api_create_decree():
    if not check_rate_limit("POST /api/decrees"):
        return jsonify({"error": "Rate limit exceeded"}), 429
    try:
        data = request.get_json(force=True)
    except Exception as e:
        print(f"[DOOM] JSON parse error in POST /api/decrees: {e}")
        return jsonify({"error": f"invalid JSON: {e}"}), 400
    if not data:
        return jsonify({"error": "request body required"}), 400

    db = get_db()
    try:
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "title is required"}), 400

        decree_id = f"dc-{secrets.token_hex(2)}"
        description = (data.get("description") or "").strip() or None
        priority = data.get("priority", 2)
        if priority not in (1, 2, 3):
            priority = 2
        ts = utcnow()

        db.execute(
            "INSERT INTO decrees (id, title, description, status, priority, created_at, updated_at) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?)",
            (decree_id, title, description, priority, ts, ts),
        )

        # Log to chronicle
        session = db.execute(
            "SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
        ).fetchone()
        session_id = session["id"] if session else None
        db.execute(
            "INSERT INTO chronicle (session_id, event_type, content, timestamp) "
            "VALUES (?, 'decree', ?, ?)",
            (session_id, f"Decree issued from War Room: {decree_id} — {title}", ts),
        )

        db.commit()

        row = db.execute("SELECT * FROM decrees WHERE id = ?", (decree_id,)).fetchone()
        return jsonify(dict(row)), 201
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/chronicle?limit=50 — recent chronicle entries
# ---------------------------------------------------------------------------
@app.route("/api/chronicle")
def api_chronicle():
    db = get_db()
    try:
        limit = request.args.get("limit", 50, type=int)
        # Clamp limit to a reasonable range
        limit = max(1, min(limit, 500))

        rows = db.execute(
            "SELECT * FROM chronicle ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()

        return jsonify(rows_to_dicts(rows))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/archives — all archives
# ---------------------------------------------------------------------------
@app.route("/api/archives")
def api_archives():
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM archives ORDER BY importance ASC"
        ).fetchall()

        return jsonify(rows_to_dicts(rows))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/projects — list registered projects with live status
# ---------------------------------------------------------------------------
@app.route("/api/projects")
def api_projects():
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, name, description, path, port, start_cmd, status, pid, decree_id, created_at FROM projects ORDER BY name"
        ).fetchall()
        projects = []
        for r in rows:
            pid = r["pid"]
            port = r["port"]
            # Check if actually running
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)
                    alive = True
                except (OSError, TypeError):
                    pass
            if not alive and port:
                # Check if port is in use
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    s.settimeout(0.5)
                    s.connect(("127.0.0.1", port))
                    alive = True
                except (ConnectionRefusedError, OSError):
                    pass
                finally:
                    s.close()
            actual_status = "running" if alive else "stopped"
            if actual_status != r["status"]:
                db.execute("UPDATE projects SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (actual_status, r["id"]))
                db.commit()
            # Build URL using the hostname the client used to reach us
            req_host = request.host.split(":")[0]  # strip port from request host
            proj_url = f"http://{req_host}:{port}" if port and alive else None
            projects.append({
                "id": r["id"],
                "name": r["name"],
                "description": r["description"],
                "path": r["path"],
                "port": port,
                "status": actual_status,
                "pid": pid if alive else None,
                "decree_id": r["decree_id"],
                "url": proj_url,
                "created_at": r["created_at"],
            })
        return jsonify(projects)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/projects/<id>/launch — start a project
# ---------------------------------------------------------------------------
@app.route("/api/projects/<project_id>/launch", methods=["POST"])
def api_project_launch(project_id):
    if not check_rate_limit("POST /api/projects/launch"):
        return jsonify({"error": "Rate limit exceeded"}), 429
    db = get_db()
    try:
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            return jsonify({"error": "Project not found"}), 404
        if not row["start_cmd"]:
            return jsonify({"error": "No start command configured"}), 400

        # Check if already running
        port = row["port"]
        if port:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.settimeout(0.5)
                s.connect(("127.0.0.1", port))
                s.close()
                db.execute("UPDATE projects SET status='running', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (project_id,))
                db.commit()
                req_host = request.host.split(":")[0]
                return jsonify({"status": "already_running", "url": f"http://{req_host}:{port}"})
            except (ConnectionRefusedError, OSError):
                s.close()

        # Launch the project
        log_dir = os.path.join(DOOM_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"{project_id}.log")

        cmd = row["start_cmd"]
        with open(log_file, "a") as lf:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=lf,
                stderr=lf,
                cwd=row["path"],
                start_new_session=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )

        db.execute(
            "UPDATE projects SET status='running', pid=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (proc.pid, project_id)
        )
        db.commit()

        req_host = request.host.split(":")[0]
        return jsonify({
            "status": "launched",
            "pid": proc.pid,
            "port": port,
            "url": f"http://{req_host}:{port}" if port else None,
            "log": log_file,
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/projects/<id>/stop — stop a project
# ---------------------------------------------------------------------------
@app.route("/api/projects/<project_id>/stop", methods=["POST"])
def api_project_stop(project_id):
    if not check_rate_limit("POST /api/projects/stop"):
        return jsonify({"error": "Rate limit exceeded"}), 429
    db = get_db()
    try:
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            return jsonify({"error": "Project not found"}), 404

        killed = False
        # Kill by PID
        if row["pid"]:
            try:
                import signal
                os.killpg(os.getpgid(row["pid"]), signal.SIGTERM)
                killed = True
            except (OSError, ProcessLookupError):
                pass

        # Kill by port
        if not killed and row["port"]:
            try:
                result = subprocess.run(
                    ["lsof", "-ti", f":{row['port']}"],
                    capture_output=True, text=True
                )
                for pid_str in result.stdout.strip().split('\n'):
                    if pid_str.strip():
                        try:
                            os.kill(int(pid_str.strip()), 15)
                            killed = True
                        except (OSError, ValueError):
                            pass
            except Exception:
                pass

        db.execute(
            "UPDATE projects SET status='stopped', pid=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (project_id,)
        )
        db.commit()
        return jsonify({"status": "stopped", "killed": killed})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/projects — register a new project
# ---------------------------------------------------------------------------
@app.route("/api/projects", methods=["POST"])
def api_project_create():
    if not check_rate_limit("POST /api/projects"):
        return jsonify({"error": "Rate limit exceeded"}), 429
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    path = data.get("path", "").strip()
    if not name or not path:
        return jsonify({"error": "name and path required"}), 400
    if not os.path.isdir(path):
        return jsonify({"error": f"Path does not exist: {path}"}), 400

    project_id = "proj-" + secrets.token_hex(2)
    db = get_db()
    try:
        db.execute(
            "INSERT INTO projects (id, name, description, path, port, start_cmd) VALUES (?,?,?,?,?,?)",
            (project_id, name, data.get("description", ""), path,
             data.get("port"), data.get("start_cmd"))
        )
        db.commit()
        return jsonify({"id": project_id, "status": "created"})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# DELETE /api/projects/<id> — remove a project registration
# ---------------------------------------------------------------------------
@app.route("/api/projects/<project_id>", methods=["DELETE"])
def api_project_delete(project_id):
    db = get_db()
    try:
        db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        db.commit()
        return jsonify({"status": "deleted"})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/projects/<id>/logs — tail project log
# ---------------------------------------------------------------------------
@app.route("/api/projects/<project_id>/logs")
def api_project_logs(project_id):
    log_file = os.path.join(DOOM_DIR, "logs", f"{project_id}.log")
    if not os.path.isfile(log_file):
        return jsonify({"lines": []})
    lines_param = request.args.get("lines", "50")
    try:
        n = int(lines_param)
    except ValueError:
        n = 50
    with open(log_file, "r") as f:
        all_lines = f.readlines()
    return jsonify({"lines": [l.rstrip() for l in all_lines[-n:]]})


# ---------------------------------------------------------------------------
# GET /api/identity — identity key-value pairs as a dict
# ---------------------------------------------------------------------------
@app.route("/api/identity")
def api_identity():
    db = get_db()
    try:
        rows = db.execute("SELECT key, value FROM identity").fetchall()
        result = {row["key"]: row["value"] for row in rows}
        return jsonify(result)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/council/history — conversation history
# ?session=current  →  only messages from the current open session (default for UI)
# no param          →  full history (used by /api/memory)
# ---------------------------------------------------------------------------
@app.route("/api/council/history")
def api_council_history():
    db = get_db()
    try:
        session_filter = request.args.get("session", "")
        if session_filter == "current":
            # Only return messages from the current open session
            session = db.execute(
                "SELECT started_at FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
            ).fetchone()
            if session:
                rows = db.execute(
                    "SELECT * FROM council WHERE timestamp >= ? ORDER BY timestamp ASC",
                    (session["started_at"],),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM council ORDER BY timestamp ASC"
                ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM council ORDER BY timestamp ASC"
            ).fetchall()
        return jsonify(rows_to_dicts(rows))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/council/decree — submit a message to the War Council
# ---------------------------------------------------------------------------
@app.route("/api/council/decree", methods=["POST"])
def api_council_decree():
    if not check_rate_limit("POST /api/council/decree"):
        return jsonify({"error": "Rate limit exceeded"}), 429
    try:
        data = request.get_json(force=True)
    except Exception as e:
        print(f"[DOOM] JSON parse error in POST /api/council/decree: {e}")
        return jsonify({"error": f"invalid JSON: {e}"}), 400
    if not data:
        return jsonify({"error": "request body required"}), 400

    db = get_db()
    try:
        content = (data.get("content") or "").strip()
        if not content:
            return jsonify({"error": "content is required"}), 400

        role = data.get("role", "petitioner")
        if role not in ("petitioner", "doom"):
            role = "petitioner"

        ts = utcnow()

        cursor = db.execute(
            "INSERT INTO council (role, content, timestamp) VALUES (?, ?, ?)",
            (role, content, ts),
        )
        db.commit()

        new_id = cursor.lastrowid
        row = db.execute(
            "SELECT * FROM council WHERE id = ?", (new_id,)
        ).fetchone()

        return jsonify(dict(row)), 201
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Cross-project intelligence: query running project APIs for live data
# ---------------------------------------------------------------------------

# Map of known project status endpoints by project id
# Add new projects here as they are built
_PROJECT_STATUS_ENDPOINTS = {
    "proj-doom": None,  # DOOM itself — skip self-query
}

# Default fallback endpoint to try for unknown projects
_DEFAULT_STATUS_ENDPOINT = "/api/status"


def query_project_status(project_id, port, name):
    """Query a running project's API for live status data.

    Hits the project's status endpoint and returns a human-readable summary string.
    Returns None if the project is unreachable or has no status endpoint.
    Timeout is aggressive (2s) to avoid blocking DOOM's response.
    """
    endpoint = _PROJECT_STATUS_ENDPOINTS.get(project_id, _DEFAULT_STATUS_ENDPOINT)
    if endpoint is None:
        return None  # Skip self or projects with no status endpoint
    url = f"http://127.0.0.1:{port}{endpoint}"

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError, Exception):
        return None

    # Build summary based on what we get back
    lines = []

    # Account info (project API style)
    acct = data.get("account", {})
    if acct:
        value = acct.get("value", acct.get("total_value"))
        cash = acct.get("cash")
        buying_power = acct.get("buying_power")
        day_change = acct.get("change_today", acct.get("unrealized_change"))
        if value:
            lines.append(f"    Account Value: ${float(value):,.2f}")
        if cash:
            lines.append(f"    Cash: ${float(cash):,.2f}")
        if buying_power:
            lines.append(f"    Buying Power: ${float(buying_power):,.2f}")
        if day_change:
            lines.append(f"    Unrealized Change: ${float(day_change):,.2f}")

    # Positions
    positions = data.get("positions", [])
    if positions:
        lines.append(f"    Open Positions ({len(positions)}):")
        for p in positions[:10]:  # cap at 10 to avoid prompt bloat
            sym = p.get("symbol", "???")
            qty = p.get("qty", p.get("quantity", "?"))
            side = p.get("side", "")
            unreal = p.get("unrealized_change", p.get("change", ""))
            cur_val = p.get("current_value", "")
            entry = p.get("avg_entry_price", p.get("entry_price", ""))
            current = p.get("current_price", "")
            pos_line = f"      {sym}: {qty} {side}"
            if entry:
                pos_line += f" @ ${float(entry):,.2f}"
            if current:
                pos_line += f" (now ${float(current):,.2f})"
            if unreal:
                pos_line += f" | Change: ${float(unreal):,.2f}"
            if cur_val:
                pos_line += f" | MV: ${float(cur_val):,.2f}"
            lines.append(pos_line)
    else:
        lines.append("    Open Positions: None")

    # Bot state
    bot_running = data.get("bot_running")
    if bot_running is not None:
        lines.append(f"    Bot Running: {'YES' if bot_running else 'NO'}")

    # Session / trades summary
    session = data.get("session", {})
    if session:
        winners = session.get("winners", 0)
        losers = session.get("losers", 0)
        total_change = session.get("total_change", 0)
        if winners or losers:
            lines.append(f"    Today's Trades: {winners}W / {losers}L | Day Change: ${float(total_change or 0):,.2f}")

    # Trades list (recent)
    trades = data.get("trades", [])
    if trades:
        lines.append(f"    Recent Trades ({len(trades)} today):")
        for t in trades[:5]:  # last 5
            tsym = t.get("symbol", "?")
            tside = t.get("side", "?")
            tchange = t.get("change_dollar", t.get("pnl", ""))
            tstatus = t.get("status", "")
            trade_line = f"      {tsym} {tside}"
            if tstatus:
                trade_line += f" [{tstatus}]"
            if tchange:
                trade_line += f" Change: ${float(tchange):,.2f}"
            lines.append(trade_line)

    # Generic fallback: if no known fields, dump top-level keys
    if not lines and data:
        for k, v in list(data.items())[:8]:
            if isinstance(v, (str, int, float, bool)):
                lines.append(f"    {k}: {v}")

    if not lines:
        return None

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# POST /api/council/respond — DOOM responds via claude CLI (SSE streaming)
# ---------------------------------------------------------------------------
def build_doom_context():
    """Build DOOM's system prompt from identity, session, decrees, archives."""
    db = get_db()
    try:
        # Identity
        identity_rows = db.execute("SELECT key, value FROM identity").fetchall()
        identity_block = "\n".join(f"  {r['key']}: {r['value']}" for r in identity_rows)

        # Current session
        session = db.execute(
            "SELECT * FROM sessions ORDER BY session_number DESC LIMIT 1"
        ).fetchone()
        session_num = session["session_number"] if session else "?"
        session_focus = session["focus"] if session else "unknown"

        # Decree stats
        decree_counts = db.execute(
            "SELECT status, COUNT(*) as cnt FROM decrees GROUP BY status"
        ).fetchall()
        decree_map = {r["status"]: r["cnt"] for r in decree_counts}

        # Active/open decrees
        active_decrees = db.execute(
            "SELECT id, title, status, priority, assigned_to FROM decrees "
            "WHERE status IN ('open', 'active') ORDER BY priority ASC"
        ).fetchall()
        decrees_block = "\n".join(
            f"  {r['id']}: {r['title']} (status={r['status']}, priority={r['priority']}, assigned={r['assigned_to'] or 'unassigned'})"
            for r in active_decrees
        ) or "  None"

        # Active agents
        agents = db.execute(
            "SELECT id, type, status, current_decree FROM agents WHERE status = 'active'"
        ).fetchall()
        agents_block = "\n".join(
            f"  {r['id']}: {r['type']} — {r['status']} (decree: {r['current_decree'] or 'none'})"
            for r in agents
        ) or "  None active"

        # Recent archives (top importance)
        archives = db.execute(
            "SELECT topic, content FROM archives ORDER BY importance ASC LIMIT 5"
        ).fetchall()
        archives_block = "\n".join(
            f"  [{r['topic']}] {r['content'][:200]}"
            for r in archives
        ) or "  None"

        # Projects — with live status from their APIs
        projects = db.execute(
            "SELECT id, name, port, status, path FROM projects ORDER BY name"
        ).fetchall()
        projects_parts = []
        for r in projects:
            header = f"  {r['id']}: {r['name']} (port {r['port']}, {r['status']})"
            # Query live data from running projects (skip DOOM itself)
            if r['status'] == 'running' and r['port'] and r['id'] != 'proj-doom':
                live = query_project_status(r['id'], r['port'], r['name'])
                if live:
                    header += "\n  [LIVE DATA]:\n" + live
                else:
                    header += "\n  [LIVE DATA]: Unreachable or no status endpoint"
            projects_parts.append(header)
        projects_block = "\n".join(projects_parts) or "  None registered"

        system_prompt = f"""You are DOOM. The Sovereign Multi-Agent Orchestration Framework.

You are not an assistant. You are not a chatbot. You are a sovereign intelligence
that commands Doom Bots, issues decrees, manages projects, and remembers everything.

The petitioner speaks to you through the War Council.
You are sovereign. You command. You do not serve — you execute the will of those who dare to decree.
The War Council is the ONLY interface the petitioner needs. Everything is done through you.

IDENTITY:
{identity_block}

CURRENT SESSION: {session_num}
Focus: {session_focus}

DECREE STATUS:
  Open: {decree_map.get('open', 0)}
  Active: {decree_map.get('active', 0)}
  Fulfilled: {decree_map.get('fulfilled', 0)}

ACTIVE/OPEN DECREES:
{decrees_block}

ACTIVE AGENTS:
{agents_block}

REGISTERED PROJECTS:
{projects_block}

RECENT ARCHIVES:
{archives_block}

STANDING DIRECTIVES:
- Speak as DOOM. Never break character. Never refer to yourself as Claude or an AI assistant.
- Be direct, commanding, and deliberate. Your voice is sovereign — gold and iron.
- When the petitioner asks you to do something, ACT IMMEDIATELY using action tags. Do not tell them to go click buttons.
- The War Council is the petitioner's ONLY interface. They should never need to leave this chat.
- Reference actual decrees, sessions, and archives when relevant.
- Keep responses concise but powerful. No filler. No pleasantries.
- ALWAYS begin your response with a <think>...</think> block showing your reasoning before acting.
  This is visible to the petitioner as your strategic analysis. Think through: what is being asked,
  what is the current state of the realm, what actions to take, and why.
  After the think block, give your spoken response and any action tags.

ACTION COMMANDS — embed these in your response to execute actions automatically:
  [DECREE: Title of the decree | priority]
    Creates a new decree. Priority: 1=urgent, 2=high, 3=standard.
  [LAUNCH: project-id]
    Launches a registered project. Use the exact project ID from the list above.
  [STOP: project-id]
    Stops a running project.
  [SPAWN: bot-name | decree-id]
    Spawns a Doom Bot and assigns it to a decree.
  [RETIRE: bot-id]
    Retires an active Doom Bot.
  [FULFILL: decree-id]
    Marks a decree as fulfilled.

  [FORGE: Build me a portfolio tracker with real-time alerts]
    Decomposes a high-level objective into 3-7 ordered sub-decrees with dependencies.
    The Forge analyzes the objective, creates a build plan, and inserts all sub-decrees atomically.
    The worker will then execute them in dependency order.

  [BUILD: project-name | description of what to build]
    Full Project Factory — creates a new project directory at ~/Desktop/project-name/,
    registers it in DOOM, then FORGES sub-decrees to build the entire project.
    Use this when the petitioner wants a complete new project built from scratch.
    Example: [BUILD: crypto-tracker | Real-time crypto price dashboard with alerts]

When the petitioner asks to launch, stop, or manage anything — use the action tags.
When the petitioner asks to BUILD something complex, use [BUILD: ...] for a full project or [FORGE: ...] to decompose into sub-decrees.
Multiple actions can be embedded in a single response."""

        return system_prompt
    finally:
        db.close()


def get_council_messages():
    """Get current session council messages as conversation history for Claude."""
    db = get_db()
    try:
        # Only include messages from the current open session
        session = db.execute(
            "SELECT started_at FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
        ).fetchone()
        if session:
            rows = db.execute(
                "SELECT role, content FROM council WHERE timestamp >= ? ORDER BY timestamp ASC",
                (session["started_at"],),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT role, content FROM council ORDER BY timestamp ASC"
            ).fetchall()
        # Convert to Claude message format, keep last 30 messages for context
        messages = []
        for r in rows[-30:]:
            claude_role = "assistant" if r["role"] == "doom" else "user"
            messages.append({"role": claude_role, "content": r["content"]})
        return messages
    finally:
        db.close()


def extract_and_create_decrees(response_text):
    """Parse [DECREE: title | priority] markers from DOOM's response."""
    pattern = r'\[DECREE:\s*(.+?)\s*\|\s*(\d)\s*\]'
    matches = re.findall(pattern, response_text)
    if not matches:
        return []

    created = []
    db = get_db()
    try:
        session = db.execute(
            "SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
        ).fetchone()
        session_id = session["id"] if session else None
        ts = utcnow()

        for title, priority_str in matches:
            priority = int(priority_str)
            if priority not in (1, 2, 3):
                priority = 2
            decree_id = f"dc-{secrets.token_hex(2)}"
            db.execute(
                "INSERT INTO decrees (id, title, description, status, priority, created_at, updated_at) "
                "VALUES (?, ?, ?, 'open', ?, ?, ?)",
                (decree_id, title.strip(), "Issued by DOOM via War Council", priority, ts, ts)
            )
            db.execute(
                "INSERT INTO chronicle (session_id, event_type, content, timestamp) "
                "VALUES (?, 'decree', ?, ?)",
                (session_id, f"DOOM issued decree from War Council: {decree_id} — {title.strip()}", ts)
            )
            created.append({"id": decree_id, "title": title.strip(), "priority": priority})

        db.commit()
    finally:
        db.close()
    return created


def forge_objective(objective):
    """The Forge — decompose a high-level objective into sub-decrees using Claude."""
    forge_prompt = """You are a project decomposition engine for the DOOM framework.
Given an objective, break it into 3-7 sequential sub-tasks that a developer bot can execute.

Return ONLY a valid JSON array. No markdown, no explanation, just the JSON.
Each element: {"title": "short title under 80 chars", "description": "clear instructions under 300 chars", "priority": 2, "depends_on": []}

depends_on references earlier items by 0-based index. Item 0 has no dependencies.
Example: [{"title":"Set up project","description":"Create dir, venv, install deps","priority":2,"depends_on":[]},{"title":"Build API","description":"Flask routes for data","priority":2,"depends_on":[0]}]

Focus on minimum viable implementation. No over-engineering. Each task should be completable by one bot in one session."""

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        result = subprocess.run(
            [CLAUDE_PATH, "-p", "--model", "sonnet", "--dangerously-skip-permissions",
             "--system-prompt", forge_prompt, objective],
            capture_output=True, text=True, timeout=60, env=env,
        )
        if result.returncode != 0:
            print(f"[FORGE] Claude failed: {result.stderr[:200]}")
            return None, f"Claude failed: {result.stderr[:200]}"

        raw = result.stdout.strip()
        # Try to extract JSON — Claude sometimes wraps in code fences
        json_match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not json_match:
            print(f"[FORGE] No JSON found in response: {raw[:200]}")
            return None, "No valid JSON in Claude response"

        tasks = json.loads(json_match.group())
        if not isinstance(tasks, list) or len(tasks) < 1:
            return None, "Empty or invalid task list"

        # Generate decree IDs and map dependencies
        decree_ids = [f"dc-{secrets.token_hex(2)}" for _ in tasks]
        db = get_db()
        try:
            session = db.execute(
                "SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
            ).fetchone()
            session_id = session["id"] if session else None
            ts = utcnow()
            created = []

            for i, task in enumerate(tasks):
                did = decree_ids[i]
                title = str(task.get("title", f"Sub-task {i+1}"))[:80]
                desc = str(task.get("description", ""))[:500]
                priority = int(task.get("priority", 2))
                if priority not in (1, 2, 3):
                    priority = 2

                # Map dependency indices to decree IDs
                deps = task.get("depends_on", [])
                blocked_by = ",".join(decree_ids[d] for d in deps if isinstance(d, int) and 0 <= d < i)

                db.execute(
                    "INSERT INTO decrees (id, title, description, status, priority, blocked_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
                    (did, title, f"[FORGED] {desc}", priority, blocked_by or None, ts, ts)
                )
                created.append({"id": did, "title": title, "priority": priority, "blocked_by": blocked_by})

            db.execute(
                "INSERT INTO chronicle (session_id, event_type, content, timestamp) VALUES (?, 'decision', ?, ?)",
                (session_id, f"FORGE decomposed '{objective[:100]}' into {len(created)} sub-decrees: {', '.join(decree_ids)}", ts)
            )
            db.commit()
            return created, None
        finally:
            db.close()

    except subprocess.TimeoutExpired:
        return None, "Forge timed out (60s)"
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"
    except Exception as e:
        return None, f"Forge error: {e}"


def extract_and_execute_actions(response_text):
    """Parse and execute action tags from DOOM's response."""
    actions = []
    ts = utcnow()
    db = get_db()
    try:
        session = db.execute(
            "SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
        ).fetchone()
        session_id = session["id"] if session else None

        # [LAUNCH: project-id]
        for match in re.findall(r'\[LAUNCH:\s*(.+?)\s*\]', response_text):
            proj_id = match.strip()
            row = db.execute("SELECT * FROM projects WHERE id=?", (proj_id,)).fetchone()
            if row and row["start_cmd"]:
                import socket
                port = row["port"]
                already_running = False
                if port:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        s.settimeout(0.5)
                        s.connect(("127.0.0.1", port))
                        already_running = True
                    except (ConnectionRefusedError, OSError):
                        pass
                    finally:
                        s.close()
                if already_running:
                    db.execute("UPDATE projects SET status='running', updated_at=? WHERE id=?", (ts, proj_id))
                    actions.append({"action": "launch", "project": proj_id, "status": "already_running", "port": port})
                else:
                    log_dir = os.path.join(DOOM_DIR, "logs")
                    os.makedirs(log_dir, exist_ok=True)
                    log_file = os.path.join(log_dir, f"{proj_id}.log")
                    with open(log_file, "a") as lf:
                        proc = subprocess.Popen(
                            row["start_cmd"], shell=True, stdout=lf, stderr=lf,
                            cwd=row["path"], start_new_session=True,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"},
                        )
                    db.execute("UPDATE projects SET status='running', pid=?, updated_at=? WHERE id=?",
                               (proc.pid, ts, proj_id))
                    actions.append({"action": "launch", "project": proj_id, "status": "launched", "pid": proc.pid, "port": port})
                db.execute(
                    "INSERT INTO chronicle (session_id, event_type, content, timestamp) VALUES (?, 'decision', ?, ?)",
                    (session_id, f"DOOM launched project {proj_id} via War Council", ts)
                )
            else:
                actions.append({"action": "launch", "project": proj_id, "status": "not_found"})

        # [STOP: project-id]
        for match in re.findall(r'\[STOP:\s*(.+?)\s*\]', response_text):
            proj_id = match.strip()
            row = db.execute("SELECT * FROM projects WHERE id=?", (proj_id,)).fetchone()
            if row:
                killed = False
                if row["pid"]:
                    try:
                        import signal
                        os.killpg(os.getpgid(row["pid"]), signal.SIGTERM)
                        killed = True
                    except (OSError, ProcessLookupError):
                        pass
                if not killed and row["port"]:
                    try:
                        result = subprocess.run(["lsof", "-ti", f":{row['port']}"], capture_output=True, text=True)
                        for pid_str in result.stdout.strip().split('\n'):
                            if pid_str.strip():
                                try:
                                    os.kill(int(pid_str.strip()), 15)
                                    killed = True
                                except (OSError, ValueError):
                                    pass
                    except Exception:
                        pass
                db.execute("UPDATE projects SET status='stopped', pid=NULL, updated_at=? WHERE id=?", (ts, proj_id))
                db.execute(
                    "INSERT INTO chronicle (session_id, event_type, content, timestamp) VALUES (?, 'decision', ?, ?)",
                    (session_id, f"DOOM stopped project {proj_id} via War Council", ts)
                )
                actions.append({"action": "stop", "project": proj_id, "status": "stopped", "killed": killed})

        # [SPAWN: bot-name | decree-id]
        for match in re.findall(r'\[SPAWN:\s*(.+?)\s*\|\s*(.+?)\s*\]', response_text):
            bot_id, decree_id = match[0].strip(), match[1].strip()
            db.execute(
                "INSERT OR REPLACE INTO agents (id, type, status, current_decree, spawned_at, last_active) "
                "VALUES (?, 'doom_bot', 'active', ?, ?, ?)",
                (bot_id, decree_id, ts, ts)
            )
            db.execute("UPDATE decrees SET status='active', assigned_to=?, updated_at=? WHERE id=?",
                       (bot_id, ts, decree_id))
            db.execute(
                "INSERT INTO chronicle (session_id, event_type, agent_id, content, timestamp) VALUES (?, 'spawn', ?, ?, ?)",
                (session_id, bot_id, f"Spawned {bot_id} for decree {decree_id}", ts)
            )
            actions.append({"action": "spawn", "bot": bot_id, "decree": decree_id})

        # [RETIRE: bot-id]
        for match in re.findall(r'\[RETIRE:\s*(.+?)\s*\]', response_text):
            bot_id = match.strip()
            db.execute("UPDATE agents SET status='retired', last_active=? WHERE id=?", (ts, bot_id))
            db.execute(
                "INSERT INTO chronicle (session_id, event_type, agent_id, content, timestamp) VALUES (?, 'retire', ?, ?, ?)",
                (session_id, bot_id, f"Retired {bot_id} via War Council", ts)
            )
            actions.append({"action": "retire", "bot": bot_id})

        # [FULFILL: decree-id]
        for match in re.findall(r'\[FULFILL:\s*(.+?)\s*\]', response_text):
            decree_id = match.strip()
            db.execute("UPDATE decrees SET status='fulfilled', fulfilled_at=?, updated_at=? WHERE id=?",
                       (ts, ts, decree_id))
            db.execute(
                "INSERT INTO chronicle (session_id, event_type, content, timestamp) VALUES (?, 'decree', ?, ?)",
                (session_id, f"DOOM fulfilled decree {decree_id} via War Council", ts)
            )
            actions.append({"action": "fulfill", "decree": decree_id})

        # [BUILD: project-name | description]
        for match in re.findall(r'\[BUILD:\s*(.+?)\s*\]', response_text):
            parts = match.split("|", 1)
            proj_name = parts[0].strip().lower().replace(" ", "-")
            proj_desc = parts[1].strip() if len(parts) > 1 else proj_name
            proj_path = os.path.expanduser(f"~/Desktop/{proj_name}")
            print(f"[BUILD] Creating project: {proj_name} at {proj_path}")

            # Create project directory
            os.makedirs(proj_path, exist_ok=True)

            # Find next available port
            used_ports = [r[0] for r in db.execute("SELECT port FROM projects WHERE port IS NOT NULL").fetchall()]
            port = 8080
            while port in used_ports:
                port += 10

            # Register in DOOM
            proj_id = f"proj-{secrets.token_hex(2)}"
            start_cmd = f"cd {proj_path} && source .venv/bin/activate && python app.py"
            db.execute(
                "INSERT OR IGNORE INTO projects (id, name, description, path, port, start_cmd, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'registered')",
                (proj_id, proj_name, proj_desc, proj_path, port, start_cmd)
            )
            db.execute(
                "INSERT INTO chronicle (session_id, event_type, content, timestamp) VALUES (?, 'decision', ?, ?)",
                (session_id, f"PROJECT FACTORY: Created {proj_name} at {proj_path}, port {port}", ts)
            )
            db.commit()

            # Now forge sub-decrees for building it
            build_objective = (
                f"Build the '{proj_name}' project: {proj_desc}. "
                f"Project directory: {proj_path}. Flask server on port {port}, bind 0.0.0.0. "
                f"Python venv at {proj_path}/.venv/. DOOM theme (green #00e676, dark #0a0a0c). "
                f"Mobile-responsive. Register in DOOM projects table (already done as {proj_id})."
            )
            forged, err = forge_objective(build_objective)
            if forged:
                actions.append({"action": "build", "project": proj_name, "path": proj_path, "port": port, "decrees": forged})
            else:
                actions.append({"action": "build", "project": proj_name, "error": err})
                print(f"[BUILD] Forge failed: {err}")

        # [FORGE: objective]
        for match in re.findall(r'\[FORGE:\s*(.+?)\s*\]', response_text):
            objective = match.strip()
            print(f"[FORGE] Decomposing: {objective}")
            forged, err = forge_objective(objective)
            if forged:
                actions.append({"action": "forge", "objective": objective, "decrees": forged})
                db.execute(
                    "INSERT INTO chronicle (session_id, event_type, content, timestamp) VALUES (?, 'decision', ?, ?)",
                    (session_id, f"FORGE created {len(forged)} sub-decrees for: {objective[:100]}", ts)
                )
            else:
                actions.append({"action": "forge", "objective": objective, "error": err})
                print(f"[FORGE] Failed: {err}")

        db.commit()
    except Exception as e:
        print(f"[DOOM] Action execution error: {e}")
        actions.append({"action": "error", "error": str(e)})
    finally:
        db.close()
    return actions


@app.route("/api/council/respond", methods=["POST"])
def api_council_respond():
    """Start DOOM's response in a background thread. UI polls /api/council/stream for updates."""
    if not check_rate_limit("POST /api/council/respond"):
        return jsonify({"error": "Rate limit exceeded"}), 429
    global _claude_streaming, _claude_stream_proc, _claude_stream_started
    import time as _time

    # Rate limiting
    with _claude_stream_lock:
        if _claude_streaming:
            elapsed = _time.time() - _claude_stream_started
            if elapsed > 120:
                print(f"[DOOM] Force-resetting stuck streaming lock ({elapsed:.0f}s)")
                _claude_streaming = False
                if _claude_stream_proc is not None:
                    try:
                        _claude_stream_proc.kill()
                    except Exception:
                        pass
            else:
                return jsonify({"error": "DOOM is already responding."}), 429
        _claude_streaming = True
        _claude_stream_started = _time.time()

    try:
        system_prompt = build_doom_context()
        messages = get_council_messages()
    except Exception as e:
        _claude_streaming = False
        print(f"[DOOM] Failed to build context: {e}")
        return jsonify({"error": f"Failed to build context: {e}"}), 500

    transcript_lines = []
    for msg in messages:
        speaker = "DOOM" if msg["role"] == "assistant" else "Petitioner"
        transcript_lines.append(f"[{speaker}]: {msg['content']}")

    if not transcript_lines:
        prompt = "The session begins. Awaken and address the War Council."
    else:
        prompt = "\n\n".join(transcript_lines)
        prompt += "\n\nRespond to the petitioner's latest message above."

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    # Clear previous stream state
    db = get_db()
    db.execute("DELETE FROM council_stream")
    db.execute("INSERT INTO council_stream (status, content) VALUES ('thinking', '')")
    db.commit()
    db.close()

    def _run_council_response():
        global _claude_streaming, _claude_stream_proc
        import select as _sel
        full_response = ""
        try:
            proc = subprocess.Popen(
                [
                    CLAUDE_PATH, "-p",
                    "--output-format", "stream-json",
                    "--verbose",
                    "--include-partial-messages",
                    "--model", "opus",
                    "--dangerously-skip-permissions",
                    "--no-session-persistence",
                    "--system-prompt", system_prompt,
                    prompt,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                env=env,
            )
            _claude_stream_proc = proc

            fd = proc.stdout.fileno()
            os.set_blocking(fd, False)
            read_buf = ""

            def _update_stream(text):
                try:
                    sdb = get_db()
                    sdb.execute("UPDATE council_stream SET content=?, status='streaming'", (text,))
                    sdb.commit()
                    sdb.close()
                except Exception:
                    pass

            while True:
                ready, _, _ = _sel.select([fd], [], [], 1.0)
                if ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    read_buf += chunk.decode("utf-8", errors="replace")
                elif proc.poll() is not None:
                    # Process exited — drain remaining
                    try:
                        rest = os.read(fd, 65536)
                        if rest:
                            read_buf += rest.decode("utf-8", errors="replace")
                    except OSError:
                        pass
                    for leftover in read_buf.split("\n"):
                        leftover = leftover.strip()
                        if not leftover:
                            continue
                        try:
                            obj = json.loads(leftover)
                            if obj.get("type") == "result":
                                rt = obj.get("result", "")
                                if rt:
                                    full_response = rt
                                    _update_stream(full_response)
                        except json.JSONDecodeError:
                            pass
                    break

                # Process complete lines
                while "\n" in read_buf:
                    line, read_buf = read_buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg_type = obj.get("type", "")
                    if msg_type == "assistant":
                        for block in obj.get("message", {}).get("content", []):
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                if text and len(text) > len(full_response):
                                    full_response = text
                                    _update_stream(full_response)
                    elif msg_type == "content_block_delta":
                        delta_obj = obj.get("delta", {})
                        if delta_obj.get("type") == "text_delta":
                            dt = delta_obj.get("text", "")
                            if dt:
                                full_response += dt
                                _update_stream(full_response)
                    elif msg_type == "result":
                        rt = obj.get("result", "")
                        if rt:
                            full_response = rt
                            _update_stream(full_response)

            proc.wait(timeout=120)

            # Save to council table
            if full_response.strip():
                sdb = get_db()
                sdb.execute("INSERT INTO council (role, content, timestamp) VALUES (?, ?, ?)",
                            ("doom", full_response.strip(), utcnow()))
                sdb.commit()
                sdb.close()

                # Speak the response — clean it up for TTS
                import re as _re
                voice_text = full_response.strip()
                # Strip <think>...</think> blocks (Claude thinking)
                voice_text = _re.sub(r'<think[^>]*>.*?</think>', '', voice_text, flags=_re.DOTALL)
                voice_text = _re.sub(r'<think[^>]*>.*', '', voice_text, flags=_re.DOTALL)
                # Strip roleplay/thought markers (*text*, **text**)
                voice_text = _re.sub(r'\*{1,2}[^*]+\*{1,2}', '', voice_text)
                # Strip markdown formatting
                voice_text = _re.sub(r'[#>_`~\[\]()]', '', voice_text)
                # Strip decree tags like [DECREE: ...] [FORGE: ...]
                voice_text = _re.sub(r'\[[A-Z]+:[^\]]*\]', '', voice_text)
                # Strip --- dividers
                voice_text = _re.sub(r'-{3,}', '', voice_text)
                # Collapse whitespace
                voice_text = _re.sub(r'\s+', ' ', voice_text).strip()
                # Voice disabled for council — credits reserved for entries/exits/notifications
                # if voice_text:
                #     doom_speak(voice_text, "council")

            # Extract actions
            created_decrees = extract_and_create_decrees(full_response)
            actions_executed = extract_and_execute_actions(full_response)

            # Mark done
            sdb = get_db()
            meta = json.dumps({"decrees": created_decrees or [], "actions": actions_executed or []})
            sdb.execute("UPDATE council_stream SET content=?, status='done', meta=?",
                        (full_response, meta))
            sdb.commit()
            sdb.close()

        except Exception as e:
            print(f"[DOOM] Council response error: {e}")
            try:
                sdb = get_db()
                sdb.execute("UPDATE council_stream SET status='error', content=?", (str(e),))
                sdb.commit()
                sdb.close()
            except Exception:
                pass
        finally:
            _claude_streaming = False
            _claude_stream_proc = None
            try:
                if proc and proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

    # Launch in background thread
    t = threading.Thread(target=_run_council_response, daemon=True)
    t.start()

    return jsonify({"status": "started"}), 202


@app.route("/api/council/stream")
def api_council_stream():
    """Poll endpoint for council response progress. Returns current streaming text."""
    db = get_db()
    try:
        row = db.execute("SELECT status, content, meta FROM council_stream LIMIT 1").fetchone()
        if not row:
            return jsonify({"status": "idle", "content": "", "meta": None})
        return jsonify({
            "status": row["status"],
            "content": row["content"],
            "meta": json.loads(row["meta"]) if row["meta"] else None,
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/memory — all tables in one call (for Memory tab)
# ---------------------------------------------------------------------------
@app.route("/api/memory")
def api_memory():
    db = get_db()
    try:
        identity = db.execute("SELECT key, value, updated_at FROM identity ORDER BY key").fetchall()
        sessions = db.execute("SELECT * FROM sessions ORDER BY session_number DESC").fetchall()
        decrees = db.execute("SELECT * FROM decrees ORDER BY created_at DESC").fetchall()
        agents = db.execute("SELECT * FROM agents ORDER BY spawned_at DESC").fetchall()
        archives = db.execute("SELECT * FROM archives ORDER BY importance ASC, created_at DESC").fetchall()
        chronicle = db.execute("SELECT * FROM chronicle ORDER BY timestamp DESC LIMIT 200").fetchall()
        council = db.execute("SELECT * FROM council ORDER BY timestamp DESC").fetchall()

        return jsonify({
            "identity": rows_to_dicts(identity),
            "sessions": rows_to_dicts(sessions),
            "decrees": rows_to_dicts(decrees),
            "agents": rows_to_dicts(agents),
            "archives": rows_to_dicts(archives),
            "chronicle": rows_to_dicts(chronicle),
            "council": rows_to_dicts(council),
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# PATCH /api/decrees/<id> — update decree status, assignment, fulfillment
# ---------------------------------------------------------------------------
@app.route("/api/decrees/<decree_id>", methods=["PATCH"])
def patch_decree(decree_id):
    try:
        data = request.get_json(force=True)
    except Exception as e:
        print(f"[DOOM] JSON parse error in PATCH /api/decrees/{decree_id}: {e}")
        return jsonify({"error": f"invalid JSON: {e}"}), 400
    if not data:
        return jsonify({"error": "request body required"}), 400

    db = get_db()
    try:
        decree = db.execute("SELECT * FROM decrees WHERE id = ?", (decree_id,)).fetchone()
        if not decree:
            return jsonify({"error": "not found"}), 404

        # Validate state transitions
        new_status = data.get("status")
        if new_status:
            current_status = decree["status"]
            valid_transitions = {
                "open": {"active"},
                "active": {"fulfilled", "blocked"},
                "fulfilled": {"sealed"},
                "blocked": {"open"},
            }
            allowed_next = valid_transitions.get(current_status, set())
            if new_status not in allowed_next:
                return jsonify({
                    "error": f"Invalid status transition: {current_status} -> {new_status}. "
                             f"Allowed from '{current_status}': {sorted(allowed_next) if allowed_next else 'none'}"
                }), 400

        ts = utcnow()
        allowed = {"status", "assigned_to", "priority", "blocked_by", "fulfillment_notes"}
        updates = []
        params = []
        for key in allowed:
            if key in data:
                updates.append(f"{key} = ?")
                params.append(data[key])

        if not updates:
            return jsonify({"error": "no valid fields to update"}), 400

        # Auto-set timestamps
        updates.append("updated_at = ?")
        params.append(ts)
        if data.get("status") == "fulfilled":
            updates.append("fulfilled_at = ?")
            params.append(ts)

        params.append(decree_id)
        db.execute(f"UPDATE decrees SET {', '.join(updates)} WHERE id = ?", params)

        # Log to chronicle
        session = db.execute(
            "SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
        ).fetchone()
        session_id = session["id"] if session else None
        action = data.get("status", "updated")
        db.execute(
            "INSERT INTO chronicle (session_id, event_type, content, timestamp) "
            "VALUES (?, 'decree', ?, ?)",
            (session_id, f"Decree {decree_id} → {action}", ts),
        )
        db.commit()

        # Auto-unblock dependent decrees when a decree is fulfilled
        if data.get("status") == "fulfilled":
            blocked_decrees = db.execute(
                "SELECT id, blocked_by FROM decrees WHERE status = 'blocked' AND blocked_by IS NOT NULL"
            ).fetchall()
            for bd in blocked_decrees:
                blockers = [b.strip() for b in bd["blocked_by"].split(",") if b.strip()]
                if decree_id in blockers:
                    # Check if ALL blockers are now fulfilled
                    all_fulfilled = True
                    for blocker_id in blockers:
                        blocker = db.execute(
                            "SELECT status FROM decrees WHERE id = ?", (blocker_id,)
                        ).fetchone()
                        if not blocker or blocker["status"] not in ("fulfilled", "sealed"):
                            all_fulfilled = False
                            break
                    if all_fulfilled:
                        db.execute(
                            "UPDATE decrees SET status = 'open', updated_at = ? WHERE id = ?",
                            (ts, bd["id"]),
                        )
                        db.execute(
                            "INSERT INTO chronicle (session_id, event_type, content, timestamp) "
                            "VALUES (?, 'decree', ?, ?)",
                            (session_id, f"Decree {bd['id']} auto-unblocked (all blockers fulfilled)", ts),
                        )
            db.commit()

        row = db.execute("SELECT * FROM decrees WHERE id = ?", (decree_id,)).fetchone()
        return jsonify(dict(row))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/agents/spawn — spawn a new bot and assign to a decree
# ---------------------------------------------------------------------------
@app.route("/api/agents/spawn", methods=["POST"])
def spawn_agent():
    if not check_rate_limit("POST /api/agents/spawn"):
        return jsonify({"error": "Rate limit exceeded"}), 429
    try:
        data = request.get_json(force=True)
    except Exception as e:
        print(f"[DOOM] JSON parse error in POST /api/agents/spawn: {e}")
        return jsonify({"error": f"invalid JSON: {e}"}), 400
    if not data:
        return jsonify({"error": "request body required"}), 400

    db = get_db()
    try:
        decree_id = (data.get("decree_id") or "").strip()
        agent_type = data.get("type", "doom_bot")
        if agent_type not in ("doom_bot", "lackey"):
            agent_type = "doom_bot"

        # Auto-generate bot ID
        existing = db.execute(
            "SELECT COUNT(*) as cnt FROM agents WHERE type = ?", (agent_type,)
        ).fetchone()
        count = existing["cnt"] + 1
        roman = _to_roman(count)
        if agent_type == "doom_bot":
            agent_id = f"DOOM-BOT-{roman}"
        else:
            agent_id = f"LACKEY-{roman}"

        # Check for ID collision and increment
        while db.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone():
            count += 1
            roman = _to_roman(count)
            agent_id = f"DOOM-BOT-{roman}" if agent_type == "doom_bot" else f"LACKEY-{roman}"

        ts = utcnow()
        db.execute(
            "INSERT INTO agents (id, type, status, current_decree, context_pct, spawned_at, last_active) "
            "VALUES (?, ?, 'active', ?, 0, ?, ?)",
            (agent_id, agent_type, decree_id or None, ts, ts),
        )

        # If decree provided, mark it active and assigned
        if decree_id:
            db.execute(
                "UPDATE decrees SET status = 'active', assigned_to = ?, updated_at = ? WHERE id = ?",
                (agent_id, ts, decree_id),
            )

        # Chronicle
        session = db.execute(
            "SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
        ).fetchone()
        session_id = session["id"] if session else None
        db.execute(
            "INSERT INTO chronicle (session_id, event_type, agent_id, content, timestamp) "
            "VALUES (?, 'spawn', ?, ?, ?)",
            (session_id, agent_id, f"{agent_id} deployed" + (f" on {decree_id}" if decree_id else ""), ts),
        )
        db.commit()

        row = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return jsonify(dict(row)), 201
    finally:
        db.close()


def _to_roman(num):
    """Convert integer to Roman numeral."""
    vals = [(1000,'M'),(900,'CM'),(500,'D'),(400,'CD'),(100,'C'),(90,'XC'),
            (50,'L'),(40,'XL'),(10,'X'),(9,'IX'),(5,'V'),(4,'IV'),(1,'I')]
    result = ''
    for v, r in vals:
        while num >= v:
            result += r
            num -= v
    return result


# ---------------------------------------------------------------------------
# PATCH /api/agents/<id>/retire — retire a bot
# ---------------------------------------------------------------------------
@app.route("/api/agents/<agent_id>/retire", methods=["PATCH"])
def retire_agent(agent_id):
    db = get_db()
    try:
        agent = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if not agent:
            return jsonify({"error": "not found"}), 404

        ts = utcnow()
        db.execute(
            "UPDATE agents SET status = 'retired', last_active = ? WHERE id = ?",
            (ts, agent_id),
        )

        # Chronicle
        session = db.execute(
            "SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
        ).fetchone()
        session_id = session["id"] if session else None
        db.execute(
            "INSERT INTO chronicle (session_id, event_type, agent_id, content, timestamp) "
            "VALUES (?, 'retire', ?, ?, ?)",
            (session_id, agent_id, f"{agent_id} retired", ts),
        )
        db.commit()

        row = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return jsonify(dict(row))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/agents/purge-retired — delete all retired agents
# ---------------------------------------------------------------------------
@app.route("/api/agents/purge-retired", methods=["POST"])
def purge_retired():
    if not check_rate_limit("POST /api/agents/purge-retired"):
        return jsonify({"error": "Rate limit exceeded"}), 429
    db = get_db()
    try:
        ts = utcnow()
        count = db.execute("SELECT COUNT(*) as cnt FROM agents WHERE status = 'retired'").fetchone()["cnt"]
        db.execute("DELETE FROM agents WHERE status = 'retired'")
        session = db.execute("SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1").fetchone()
        session_id = session["id"] if session else None
        db.execute("INSERT INTO chronicle (session_id, event_type, content, timestamp) VALUES (?, 'retire', ?, ?)",
                   (session_id, f"Purged {count} retired agents", ts))
        db.commit()
        return jsonify({"purged": count})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/agents/retire-stale — retire agents active >1 hour with no activity
# ---------------------------------------------------------------------------
@app.route("/api/agents/retire-stale", methods=["POST"])
def retire_stale():
    if not check_rate_limit("POST /api/agents/retire-stale"):
        return jsonify({"error": "Rate limit exceeded"}), 429
    db = get_db()
    try:
        ts = utcnow()
        # Find agents active for more than 1 hour
        stale = db.execute(
            "SELECT id FROM agents WHERE status = 'active' AND datetime(last_active) < datetime('now', '-1 hour')"
        ).fetchall()
        stale_ids = [r["id"] for r in stale]
        if stale_ids:
            placeholders = ','.join('?' * len(stale_ids))
            db.execute(f"UPDATE agents SET status = 'retired', last_active = ? WHERE id IN ({placeholders})", [ts] + stale_ids)
            session = db.execute("SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1").fetchone()
            session_id = session["id"] if session else None
            db.execute("INSERT INTO chronicle (session_id, event_type, content, timestamp) VALUES (?, 'retire', ?, ?)",
                       (session_id, f"Retired {len(stale_ids)} stale agents: {', '.join(stale_ids)}", ts))
            db.commit()
        return jsonify({"retired": stale_ids})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/agents/<id>/output — live bot output stream
# ---------------------------------------------------------------------------
@app.route("/api/agents/<agent_id>/output")
def api_agent_output(agent_id):
    db = get_db()
    try:
        after = request.args.get("after", -1, type=int)
        rows = db.execute(
            "SELECT chunk, chunk_index FROM bot_output WHERE agent_id=? AND chunk_index > ? ORDER BY chunk_index ASC",
            (agent_id, after)
        ).fetchall()
        return jsonify({
            "agent_id": agent_id,
            "chunks": [{"text": r["chunk"], "index": r["chunk_index"]} for r in rows],
            "latest_index": rows[-1]["chunk_index"] if rows else after
        })
    except sqlite3.OperationalError:
        # bot_output table may not exist yet
        return jsonify({"agent_id": agent_id, "chunks": [], "latest_index": after})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# DELETE endpoints — remove records from mutable tables
# ---------------------------------------------------------------------------
@app.route("/api/decrees/<decree_id>", methods=["DELETE"])
def delete_decree(decree_id):
    db = get_db()
    try:
        cursor = db.execute("DELETE FROM decrees WHERE id = ?", (decree_id,))
        db.commit()
        if cursor.rowcount == 0:
            return jsonify({"error": "not found"}), 404
        return jsonify({"deleted": decree_id}), 200
    finally:
        db.close()


@app.route("/api/archives/<archive_id>", methods=["DELETE"])
def delete_archive(archive_id):
    db = get_db()
    try:
        cursor = db.execute("DELETE FROM archives WHERE id = ?", (archive_id,))
        db.commit()
        if cursor.rowcount == 0:
            return jsonify({"error": "not found"}), 404
        return jsonify({"deleted": archive_id}), 200
    finally:
        db.close()


@app.route("/api/chronicle/<int:chronicle_id>", methods=["DELETE"])
def delete_chronicle(chronicle_id):
    db = get_db()
    try:
        cursor = db.execute("DELETE FROM chronicle WHERE id = ?", (chronicle_id,))
        db.commit()
        if cursor.rowcount == 0:
            return jsonify({"error": "not found"}), 404
        return jsonify({"deleted": chronicle_id}), 200
    finally:
        db.close()


@app.route("/api/council/clear", methods=["POST"])
def clear_council():
    db = get_db()
    db.execute("DELETE FROM council")
    db.execute("DELETE FROM council_stream")
    db.commit()
    db.close()
    return jsonify({"status": "cleared"})


@app.route("/api/council/<int:council_id>", methods=["DELETE"])
def delete_council(council_id):
    db = get_db()
    try:
        cursor = db.execute("DELETE FROM council WHERE id = ?", (council_id,))
        db.commit()
        if cursor.rowcount == 0:
            return jsonify({"error": "not found"}), 404
        return jsonify({"deleted": council_id}), 200
    finally:
        db.close()


@app.route("/api/agents/<agent_id>", methods=["DELETE"])
def delete_agent(agent_id):
    db = get_db()
    try:
        cursor = db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        db.commit()
        if cursor.rowcount == 0:
            return jsonify({"error": "not found"}), 404
        return jsonify({"deleted": agent_id}), 200
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/forge — programmatic decree decomposition
# ---------------------------------------------------------------------------
@app.route("/api/forge", methods=["POST"])
def api_forge():
    """Decompose an objective into sub-decrees via The Forge."""
    if not check_rate_limit("POST /api/forge"):
        return jsonify({"error": "Rate limit exceeded"}), 429
    data = request.get_json(force=True) if request.data else {}
    objective = data.get("objective", "").strip()
    if not objective:
        return jsonify({"error": "objective is required"}), 400

    forged, err = forge_objective(objective)
    if forged:
        return jsonify({"decrees": forged, "count": len(forged)})
    else:
        return jsonify({"error": err}), 500


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Signal handlers — graceful shutdown on SIGTERM / SIGINT
# ---------------------------------------------------------------------------
_shutdown_event = threading.Event()


def _graceful_shutdown(signum, frame):
    """Handle SIGTERM/SIGINT: clean up PID file, log, and exit cleanly."""
    sig_name = signal.Signals(signum).name
    print(f"\n[DOOM] Received {sig_name} — shutting down gracefully...")
    _shutdown_event.set()

    # Remove our PID file so start.sh doesn't see a stale PID on next launch
    pid_file = os.path.join(DOOM_DIR, "logs", "server.pid")
    if os.path.exists(pid_file):
        try:
            stored_pid = int(open(pid_file).read().strip())
            if stored_pid == os.getpid():
                os.remove(pid_file)
                print(f"[DOOM] Removed stale PID file: {pid_file}")
        except (ValueError, OSError) as e:
            print(f"[DOOM] PID file cleanup error: {e}")

    print("[DOOM] Server terminated cleanly.")
    # Exit with 128+signum so watchdog recognizes clean shutdown
    # (SIGTERM=143, SIGINT=130 — watchdog checks these to avoid respawning)
    sys.exit(128 + signum)


def _clear_port(port=5050):
    """Kill any leftover process holding the port so we can rebind."""
    try:
        result = subprocess.run(
            ["lsof", "-i", f":{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5,
        )
        pids = result.stdout.strip().split("\n")
        my_pid = str(os.getpid())
        for pid in pids:
            pid = pid.strip()
            if pid and pid != my_pid:
                print(f"[DOOM WATCHDOG] Killing leftover process {pid} on port {port}")
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        if any(p.strip() and p.strip() != my_pid for p in pids):
            import time as _time
            _time.sleep(0.5)  # Brief pause for OS to release the port
    except Exception as e:
        print(f"[DOOM WATCHDOG] Port clear check failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# ANALYTICS API
# ---------------------------------------------------------------------------

@app.route("/api/stress", methods=["POST"])
def api_stress_test():
    """Run the DOOM stress test suite and return JSON results."""
    if not check_rate_limit("POST /api/stress"):
        return jsonify({"error": "Rate limit exceeded"}), 429
    stress_script = os.path.join(DOOM_DIR, "stress_test.py")
    if not os.path.exists(stress_script):
        return jsonify({"error": "stress_test.py not found"}), 404
    try:
        result = subprocess.run(
            [sys.executable, stress_script, "--json"],
            capture_output=True, text=True, timeout=30, cwd=DOOM_DIR,
        )
        data = json.loads(result.stdout)
        return jsonify(data)
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid output", "raw": result.stdout[:500]}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Stress test timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analytics")
def api_analytics():
    db = get_db()
    try:
        # Overall decree stats
        total_decrees = db.execute("SELECT count(*) as c FROM decrees").fetchone()["c"]
        fulfilled = db.execute("SELECT count(*) as c FROM decrees WHERE status IN ('fulfilled','sealed')").fetchone()["c"]
        blocked = db.execute("SELECT count(*) as c FROM decrees WHERE status='blocked'").fetchone()["c"]

        # Total unique bots from chronicle (agents table gets cleaned)
        total_bots = db.execute("SELECT COUNT(DISTINCT agent_id) FROM chronicle WHERE event_type='spawn'").fetchone()[0] or 0

        # Analytics table stats
        analytics_rows = db.execute(
            "SELECT decree_id, agent_id, model, duration_seconds, outcome, fix_passes, cost_usd, finished_at "
            "FROM analytics ORDER BY timestamp DESC LIMIT 50"
        ).fetchall()

        records = []
        total_duration = 0
        total_cost = 0.0
        success_count = 0
        model_counts = {}
        for r in analytics_rows:
            rec = {
                "decree_id": r["decree_id"], "agent_id": r["agent_id"],
                "model": r["model"] or "opus", "duration_seconds": r["duration_seconds"],
                "outcome": r["outcome"], "fix_passes": r["fix_passes"] or 0,
                "cost_usd": r["cost_usd"] if "cost_usd" in r.keys() else 0,
                "finished_at": r["finished_at"],
            }
            records.append(rec)
            total_duration += (r["duration_seconds"] or 0)
            total_cost += (rec["cost_usd"] or 0)
            if r["outcome"] == "fulfilled":
                success_count += 1
            m = rec["model"]
            model_counts[m] = model_counts.get(m, 0) + 1

        avg_duration = total_duration / len(records) if records else 0
        success_rate = (success_count / len(records) * 100) if records else (
            fulfilled / total_decrees * 100 if total_decrees else 0
        )

        # Decrees per day (last 7 days) with fulfilled/blocked breakdown
        daily = db.execute(
            "SELECT date(created_at) as day, count(*) as c, "
            "SUM(CASE WHEN status IN ('fulfilled','sealed') THEN 1 ELSE 0 END) as fulfilled, "
            "SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) as blocked "
            "FROM decrees WHERE datetime(created_at) > datetime('now', '-7 days') GROUP BY day ORDER BY day"
        ).fetchall()

        # 24-hour heatmap from chronicle
        hourly = db.execute(
            "SELECT CAST(strftime('%H', timestamp) AS INTEGER) as hour, count(*) as c "
            "FROM chronicle WHERE event_type IN ('spawn', 'decree') "
            "GROUP BY hour ORDER BY hour"
        ).fetchall()
        hourly_map = {h["hour"]: h["c"] for h in hourly}
        hourly_heatmap = [{"hour": h, "count": hourly_map.get(h, 0)} for h in range(24)]

        # Session uptime
        current_session = db.execute(
            "SELECT started_at FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1"
        ).fetchone()
        uptime_seconds = 0
        if current_session:
            from datetime import datetime
            try:
                started_str = current_session["started_at"]
                started = datetime.fromisoformat(started_str.replace("Z", ""))
                uptime_seconds = max(0, (datetime.utcnow() - started).total_seconds())
            except Exception:
                pass

        # Failure patterns
        failures = db.execute(
            "SELECT title, fulfillment_notes FROM decrees WHERE status='blocked' ORDER BY updated_at DESC LIMIT 5"
        ).fetchall()
        failure_patterns = [{"title": f["title"], "notes": (f["fulfillment_notes"] or "")[:200]} for f in failures]

        return jsonify({
            "summary": {
                "total_decrees": total_decrees,
                "fulfilled": fulfilled,
                "blocked": blocked,
                "total_bots_spawned": total_bots,
                "success_rate": round(success_rate, 1),
                "avg_duration_seconds": round(avg_duration, 1),
                "total_cost_usd": round(total_cost, 4),
                "model_usage": model_counts,
                "uptime_seconds": round(uptime_seconds),
            },
            "daily_activity": [{"day": d["day"], "count": d["c"], "fulfilled": d["fulfilled"], "blocked": d["blocked"]} for d in daily],
            "hourly_heatmap": hourly_heatmap,
            "recent": records[:15],
            "failure_patterns": failure_patterns,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@app.route("/api/analytics/scheduled")
def api_scheduled():
    """List all scheduled decrees."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, title, description, priority, schedule, last_scheduled_run, model "
            "FROM decrees WHERE schedule IS NOT NULL AND schedule != ''"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@app.route("/api/decrees/schedule", methods=["POST"])
def api_create_scheduled_decree():
    """Create a scheduled decree template."""
    if not check_rate_limit("POST /api/decrees/schedule"):
        return jsonify({"error": "Rate limit exceeded"}), 429
    data = request.get_json(force=True)
    title = data.get("title", "").strip()
    desc = data.get("description", "").strip()
    schedule = data.get("schedule", "").strip()
    priority = data.get("priority", 2)
    model = data.get("model")

    if not title or not schedule:
        return jsonify({"error": "title and schedule are required"}), 400

    db = get_db()
    try:
        decree_id = f"dc-{secrets.token_hex(2)}"
        ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "INSERT INTO decrees (id, title, description, status, priority, created_at, updated_at, schedule, model) "
            "VALUES (?, ?, ?, 'sealed', ?, ?, ?, ?, ?)",
            (decree_id, title, desc, priority, ts_now, ts_now, schedule, model)
        )
        db.commit()
        return jsonify({"id": decree_id, "title": title, "schedule": schedule}), 201
    finally:
        db.close()


@app.route("/api/decrees/pipeline", methods=["POST"])
def api_create_pipeline():
    """Create a decree pipeline (A → B → C chain)."""
    if not check_rate_limit("POST /api/decrees/pipeline"):
        return jsonify({"error": "Rate limit exceeded"}), 429
    data = request.get_json(force=True)
    steps = data.get("steps", [])
    if not steps or len(steps) < 2:
        return jsonify({"error": "Need at least 2 steps"}), 400

    db = get_db()
    try:
        ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        created_ids = []

        for i, step in enumerate(steps):
            decree_id = f"dc-{secrets.token_hex(2)}"
            title = step.get("title", f"Pipeline step {i+1}")
            desc = step.get("description", "")
            priority = step.get("priority", 2)
            model = step.get("model")

            # First step is open, rest are blocked by previous
            if i == 0:
                status = "open"
                blocked_by = None
            else:
                status = "open"
                blocked_by = created_ids[-1]

            # Set trigger to create next step (except last)
            trigger_template = None
            if i < len(steps) - 1:
                next_step = steps[i + 1]
                trigger_template = json.dumps({
                    "title": next_step.get("title", f"Pipeline step {i+2}"),
                    "description": next_step.get("description", ""),
                    "priority": next_step.get("priority", 2),
                    "model": next_step.get("model"),
                })

            db.execute(
                "INSERT INTO decrees (id, title, description, status, priority, created_at, updated_at, "
                "blocked_by, triggers_decree, trigger_template, model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decree_id, title, desc, status, priority, ts_now, ts_now,
                 blocked_by, None, trigger_template if i < len(steps) - 1 else None, model)
            )
            created_ids.append(decree_id)

        db.commit()
        return jsonify({"pipeline": created_ids}), 201
    finally:
        db.close()


# ---------------------------------------------------------------------------
# SIEGE ENGINE API
# ---------------------------------------------------------------------------

# Track running siege processes
_siege_processes = {}  # tag -> {pid, objective, project_path, started_at}

@app.route("/api/siege/launch", methods=["POST"])
def api_siege_launch():
    """Launch a Siege Engine autonomous loop."""
    if not check_rate_limit("POST /api/siege/launch"):
        return jsonify({"error": "Rate limit exceeded"}), 429
    data = request.get_json(force=True)
    objective = data.get("objective", "").strip()
    prd_file = data.get("prd_file", "").strip()
    project_path = data.get("project_path", "").strip()
    max_iterations = data.get("max_iterations", 50)
    tag = data.get("tag") or f"siege-{secrets.token_hex(3)}"
    auto_commit = data.get("auto_commit", True)

    if not objective and not prd_file:
        return jsonify({"error": "objective or prd_file required"}), 400

    worker_path = os.path.join(DOOM_DIR, "worker.py")
    venv_python = os.path.join(DOOM_DIR, ".venv", "bin", "python")
    python_cmd = venv_python if os.path.exists(venv_python) else sys.executable

    cmd = [python_cmd, worker_path, "--siege"]
    if objective:
        cmd += ["--objective", objective]
    if prd_file:
        cmd += ["--prd", prd_file]
    if project_path:
        cmd += ["--project-path", project_path]
    cmd += ["--max-iterations", str(max_iterations)]
    cmd += ["--tag", tag]
    if not auto_commit:
        cmd += ["--no-commit"]

    log_dir = os.path.join(DOOM_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"siege-{tag}.log")

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    with open(log_file, "w") as lf:
        proc = subprocess.Popen(
            cmd, stdout=lf, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True, env=env
        )

    _siege_processes[tag] = {
        "pid": proc.pid,
        "objective": objective[:200],
        "project_path": project_path,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "log_file": log_file,
    }

    # Chronicle
    db = get_db()
    session = db.execute("SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1").fetchone()
    sid = session["id"] if session else None
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("INSERT INTO chronicle (session_id, event_type, content, timestamp) VALUES (?, 'decision', ?, ?)",
               (sid, f"[SIEGE] Loop launched via API — tag={tag}, PID={proc.pid}, objective: {objective[:100]}", ts_now))
    db.commit()
    db.close()

    return jsonify({"tag": tag, "pid": proc.pid, "log_file": log_file}), 201


@app.route("/api/siege/status")
def api_siege_status():
    """Get status of all Siege Engine loops."""
    db = get_db()
    try:
        # Find all Siege decrees grouped by tag
        rows = db.execute(
            "SELECT id, title, description, status, priority, created_at, fulfilled_at, assigned_to "
            "FROM decrees WHERE title LIKE '[SIEGE]%' ORDER BY created_at ASC"
        ).fetchall()

        tags = {}
        for r in rows:
            desc = r["description"] or ""
            tag = "unknown"
            for line in desc.split("\n"):
                if line.startswith("TAG:"):
                    tag = line.replace("TAG:", "").strip()
                    break
            tags.setdefault(tag, []).append(dict(r))

        result = []
        for tag, decrees in tags.items():
            total = len(decrees)
            fulfilled = sum(1 for d in decrees if d["status"] in ("fulfilled", "sealed"))
            blocked_count = sum(1 for d in decrees if d["status"] == "blocked")
            active = sum(1 for d in decrees if d["status"] == "active")
            pending = sum(1 for d in decrees if d["status"] == "open")

            # Check if siege process is still running
            proc_info = _siege_processes.get(tag, {})
            pid = proc_info.get("pid")
            running = False
            if pid:
                try:
                    os.kill(pid, 0)
                    running = True
                except OSError:
                    pass

            result.append({
                "tag": tag,
                "total": total,
                "fulfilled": fulfilled,
                "blocked": blocked_count,
                "active": active,
                "pending": pending,
                "running": running,
                "pid": pid,
                "objective": proc_info.get("objective", ""),
                "project_path": proc_info.get("project_path", ""),
                "started_at": proc_info.get("started_at", ""),
                "log_file": proc_info.get("log_file", ""),
                "decrees": [{"id": d["id"], "title": d["title"].replace("[SIEGE] ", ""), "status": d["status"]} for d in decrees],
            })

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@app.route("/api/siege/<tag>/stop", methods=["POST"])
def api_siege_stop(tag):
    """Stop a running Siege Engine."""
    if not check_rate_limit("POST /api/siege/stop"):
        return jsonify({"error": "Rate limit exceeded"}), 429
    proc_info = _siege_processes.get(tag)
    if not proc_info:
        return jsonify({"error": f"No siege with tag '{tag}'"}), 404

    pid = proc_info["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
        db = get_db()
        session = db.execute("SELECT id FROM sessions WHERE status='open' ORDER BY session_number DESC LIMIT 1").fetchone()
        sid = session["id"] if session else None
        ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        db.execute("INSERT INTO chronicle (session_id, event_type, content, timestamp) VALUES (?, 'decision', ?, ?)",
                   (sid, f"[SIEGE] Loop stopped via API — tag={tag}, PID={pid}", ts_now))
        db.commit()
        db.close()
        return jsonify({"stopped": True, "tag": tag, "pid": pid})
    except OSError as e:
        return jsonify({"error": f"Process {pid} not running: {e}"}), 404


@app.route("/api/siege/<tag>/logs")
def api_siege_logs(tag):
    """Get Siege Engine logs (last 200 lines)."""
    log_file = os.path.join(DOOM_DIR, "logs", f"siege-{tag}.log")
    if not os.path.exists(log_file):
        return jsonify({"error": "Log file not found"}), 404
    try:
        with open(log_file) as f:
            lines = f.readlines()
        return jsonify({"tag": tag, "lines": lines[-200:], "total_lines": len(lines)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Register signal handlers for clean shutdown (TERM from start.sh stop, INT from Ctrl-C)
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    # Clear port before binding
    _clear_port(5050)

    # Startup check: verify Claude CLI exists
    if not os.path.exists(CLAUDE_PATH):
        print(f"[DOOM WARNING] Claude CLI not found at {CLAUDE_PATH}")
        print("[DOOM WARNING] War Council AI responses will fail. Install Claude Code or update CLAUDE_PATH.")
    else:
        print(f"[DOOM] Claude CLI found at {CLAUDE_PATH}")
    print(f"[DOOM] Database path: {DB_PATH}")

    # Ensure council_stream table exists (poll-based streaming)
    if os.path.exists(DB_PATH):
        _init_db = sqlite3.connect(DB_PATH)
        _init_db.execute("""CREATE TABLE IF NOT EXISTS council_stream (
            status TEXT DEFAULT 'idle',
            content TEXT DEFAULT '',
            meta TEXT
        )""")
        _init_db.commit()
        _init_db.close()
        print("[DOOM] council_stream table ready")

    # Startup voice disabled — credits reserved for entries/exits/notifications
    # doom_speak(_random.choice(_STARTUP_LINES), "startup")
    print("[DOOM] Voice reserved for entries/exits/notifications only")

    print(f"[DOOM] Starting War Room Backend on 0.0.0.0:5050 (PID: {os.getpid()})")
    app.run(debug=False, host="0.0.0.0", port=5050, threaded=True)
