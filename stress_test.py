#!/usr/bin/env python3
"""
DOOM Stress Test — Real Load + Diagnostic Suite

Phase 1: Health check (are things alive?)
Phase 2: Latency benchmarks (are endpoints fast?)
Phase 3: Concurrent load (can it handle 20+ parallel requests?)
Phase 4: DB stress (write contention under load)
Phase 5: Integrity (files, security, config)

Usage:
    python stress_test.py                  # Full test (~15-20s)
    python stress_test.py --category load  # Only load tests
    python stress_test.py --json           # Pure JSON output
    python stress_test.py --fix            # Include fix suggestions

Categories: health, latency, load, db_stress, council, integrity
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

DOOM_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DOOM_DIR, "memory.db")
BASE_URL = "http://localhost:5050"

# ── Test results accumulator ──
results = []
_quiet = False
_print_lock = Lock()


def record(category, name, passed, detail=""):
    results.append({
        "category": category,
        "name": name,
        "passed": passed,
        "detail": detail,
    })
    if _quiet:
        return
    with _print_lock:
        status = "\033[32mPASS\033[0m" if passed else "\033[31mFAIL\033[0m"
        print(f"  [{status}] {category}/{name}" + (f" — {detail}" if detail else ""))


def http_get(path, timeout=10):
    """GET request, return (status_code, body_text, elapsed_ms, error_string)."""
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(BASE_URL + path)
        resp = urllib.request.urlopen(req, timeout=timeout)
        body = resp.read().decode("utf-8", errors="replace")
        elapsed = (time.monotonic() - t0) * 1000
        return resp.status, body, elapsed, None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        elapsed = (time.monotonic() - t0) * 1000
        return e.code, body, elapsed, str(e)
    except Exception as e:
        elapsed = (time.monotonic() - t0) * 1000
        return 0, "", elapsed, str(e)


def http_post(path, data=None, timeout=10):
    """POST JSON, return (status_code, body_text, elapsed_ms, error_string)."""
    t0 = time.monotonic()
    try:
        payload = json.dumps(data or {}).encode("utf-8")
        req = urllib.request.Request(
            BASE_URL + path,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        body = resp.read().decode("utf-8", errors="replace")
        elapsed = (time.monotonic() - t0) * 1000
        return resp.status, body, elapsed, None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        elapsed = (time.monotonic() - t0) * 1000
        return e.code, body, elapsed, str(e)
    except Exception as e:
        elapsed = (time.monotonic() - t0) * 1000
        return 0, "", elapsed, str(e)


# ═══════════════════════════════════════════════════════════════════
# Phase 1: HEALTH — are all components alive?
# ═══════════════════════════════════════════════════════════════════
def test_health():
    if not _quiet:
        print("\n── PHASE 1: HEALTH CHECK ──")

    # Server alive
    status, _, ms, err = http_get("/health")
    record("health", "server-alive", status == 200, f"{ms:.0f}ms" if status == 200 else err)

    # All daemon PID files
    daemons = ["server", "worker", "watchtower", "introspect", "scheduler", "healthmon"]
    for d in daemons:
        pidpath = os.path.join(DOOM_DIR, "logs", f"{d}.pid")
        alive = False
        if os.path.exists(pidpath):
            try:
                pid = int(open(pidpath).read().strip())
                os.kill(pid, 0)
                alive = True
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        record("health", f"{d}-alive", alive, "" if alive else "process dead or no PID")

    # Database accessible
    try:
        conn = sqlite3.connect(DB_PATH, timeout=2)
        conn.execute("SELECT 1")
        conn.close()
        record("health", "db-accessible", True)
    except Exception as e:
        record("health", "db-accessible", False, str(e))

    # Required tables
    try:
        conn = sqlite3.connect(DB_PATH)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        required = ["identity", "decrees", "agents", "sessions", "archives",
                     "chronicle", "council", "projects", "council_stream"]
        missing = [t for t in required if t not in tables]
        record("health", "all-tables-present", len(missing) == 0,
               f"missing: {', '.join(missing)}" if missing else f"{len(required)} tables OK")
    except Exception as e:
        record("health", "all-tables-present", False, str(e))


# ═══════════════════════════════════════════════════════════════════
# Phase 2: LATENCY — response time benchmarks per endpoint
# ═══════════════════════════════════════════════════════════════════
LATENCY_THRESHOLD_MS = 500  # endpoints must respond under this

ENDPOINTS = [
    "/health",
    "/api/session",
    "/api/agents",
    "/api/decrees",
    "/api/chronicle",
    "/api/archives",
    "/api/projects",
    "/api/identity",
    "/api/council/history",
    "/api/council/stream",
    "/api/memory",
    "/api/analytics",
    "/api/siege/status",
]


def test_latency():
    if not _quiet:
        print("\n── PHASE 2: LATENCY BENCHMARKS ──")

    for ep in ENDPOINTS:
        # Hit each endpoint 3 times, take the median
        times = []
        ok = True
        for _ in range(3):
            status, body, ms, err = http_get(ep)
            if status != 200:
                ok = False
                record("latency", f"GET {ep}", False, err or f"status={status}")
                break
            times.append(ms)

        if ok and times:
            median = sorted(times)[len(times) // 2]
            p99 = max(times)
            passed = median < LATENCY_THRESHOLD_MS
            record("latency", f"GET {ep}", passed,
                   f"median={median:.0f}ms p99={p99:.0f}ms" + ("" if passed else f" SLOW (>{LATENCY_THRESHOLD_MS}ms)"))

    # UI routes
    for path in ["/mobile", "/desktop", "/logo.png"]:
        status, _, ms, err = http_get(path)
        record("latency", f"GET {path}", status == 200 and ms < 1000,
               f"{ms:.0f}ms" if status == 200 else err)


# ═══════════════════════════════════════════════════════════════════
# Phase 3: LOAD — concurrent request hammering
# ═══════════════════════════════════════════════════════════════════
CONCURRENT_USERS = 20
REQUESTS_PER_USER = 5


def _load_worker(endpoint):
    """Single worker: hit endpoint N times, return (successes, failures, times)."""
    successes = 0
    failures = 0
    times = []
    for _ in range(REQUESTS_PER_USER):
        status, _, ms, _ = http_get(endpoint, timeout=15)
        times.append(ms)
        if status == 200:
            successes += 1
        else:
            failures += 1
    return successes, failures, times


def test_load():
    if not _quiet:
        print(f"\n── PHASE 3: CONCURRENT LOAD ({CONCURRENT_USERS} users x {REQUESTS_PER_USER} req) ──")

    # Test a few key endpoints under load
    load_endpoints = ["/api/session", "/api/decrees", "/api/agents", "/api/chronicle"]

    for ep in load_endpoints:
        all_times = []
        total_ok = 0
        total_fail = 0
        t0 = time.monotonic()

        with ThreadPoolExecutor(max_workers=CONCURRENT_USERS) as pool:
            futures = [pool.submit(_load_worker, ep) for _ in range(CONCURRENT_USERS)]
            for f in as_completed(futures):
                ok, fail, times = f.result()
                total_ok += ok
                total_fail += fail
                all_times.extend(times)

        elapsed = time.monotonic() - t0
        total_reqs = total_ok + total_fail
        rps = total_reqs / elapsed if elapsed > 0 else 0
        error_rate = (total_fail / total_reqs * 100) if total_reqs > 0 else 0
        avg_ms = sum(all_times) / len(all_times) if all_times else 0
        p95 = sorted(all_times)[int(len(all_times) * 0.95)] if all_times else 0
        p99 = sorted(all_times)[int(len(all_times) * 0.99)] if all_times else 0

        passed = error_rate < 5 and p95 < 2000
        record("load", f"HAMMER {ep}", passed,
               f"{total_reqs} reqs in {elapsed:.1f}s | {rps:.0f} rps | avg={avg_ms:.0f}ms p95={p95:.0f}ms p99={p99:.0f}ms | err={error_rate:.1f}%")

    # Mixed endpoint load — all endpoints at once
    if not _quiet:
        print("  ... mixed endpoint barrage ...")
    all_times = []
    total_ok = 0
    total_fail = 0
    t0 = time.monotonic()

    def _mixed_worker():
        ok, fail, times = 0, 0, []
        for ep in ENDPOINTS:
            status, _, ms, _ = http_get(ep, timeout=15)
            times.append(ms)
            if status == 200:
                ok += 1
            else:
                fail += 1
        return ok, fail, times

    with ThreadPoolExecutor(max_workers=CONCURRENT_USERS) as pool:
        futures = [pool.submit(_mixed_worker) for _ in range(CONCURRENT_USERS)]
        for f in as_completed(futures):
            ok, fail, times = f.result()
            total_ok += ok
            total_fail += fail
            all_times.extend(times)

    elapsed = time.monotonic() - t0
    total_reqs = total_ok + total_fail
    rps = total_reqs / elapsed if elapsed > 0 else 0
    error_rate = (total_fail / total_reqs * 100) if total_reqs > 0 else 0
    avg_ms = sum(all_times) / len(all_times) if all_times else 0
    p95 = sorted(all_times)[int(len(all_times) * 0.95)] if all_times else 0

    passed = error_rate < 5
    record("load", "MIXED BARRAGE", passed,
           f"{total_reqs} reqs in {elapsed:.1f}s | {rps:.0f} rps | avg={avg_ms:.0f}ms p95={p95:.0f}ms | err={error_rate:.1f}%")


# ═══════════════════════════════════════════════════════════════════
# Phase 4: DB STRESS — concurrent writes and read/write contention
# ═══════════════════════════════════════════════════════════════════
DB_WRITE_WORKERS = 10
DB_WRITES_PER_WORKER = 20


def test_db_stress():
    if not _quiet:
        print(f"\n── PHASE 4: DB STRESS ({DB_WRITE_WORKERS} writers x {DB_WRITES_PER_WORKER} writes) ──")

    if not os.path.exists(DB_PATH):
        record("db_stress", "db-exists", False, "No database")
        return

    # Concurrent chronicle writes
    write_errors = []
    write_times = []
    write_lock = Lock()

    def _db_write_worker(worker_id):
        errors = 0
        times = []
        for i in range(DB_WRITES_PER_WORKER):
            t0 = time.monotonic()
            try:
                conn = sqlite3.connect(DB_PATH, timeout=10)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute(
                    "INSERT INTO chronicle (session_id, event_type, agent_id, content) VALUES (?, ?, ?, ?)",
                    ("stress-test", "stress", f"stress-{worker_id}",
                     f"Stress write {worker_id}-{i} at {time.time()}")
                )
                conn.commit()
                conn.close()
                times.append((time.monotonic() - t0) * 1000)
            except Exception as e:
                errors += 1
                times.append((time.monotonic() - t0) * 1000)
        return errors, times

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=DB_WRITE_WORKERS) as pool:
        futures = [pool.submit(_db_write_worker, i) for i in range(DB_WRITE_WORKERS)]
        for f in as_completed(futures):
            errs, times = f.result()
            write_errors.append(errs)
            write_times.extend(times)

    elapsed = time.monotonic() - t0
    total_writes = DB_WRITE_WORKERS * DB_WRITES_PER_WORKER
    total_errors = sum(write_errors)
    wps = total_writes / elapsed if elapsed > 0 else 0
    avg_ms = sum(write_times) / len(write_times) if write_times else 0
    p95 = sorted(write_times)[int(len(write_times) * 0.95)] if write_times else 0
    error_rate = (total_errors / total_writes * 100) if total_writes > 0 else 0

    record("db_stress", "concurrent-writes", error_rate < 2,
           f"{total_writes} writes in {elapsed:.1f}s | {wps:.0f} w/s | avg={avg_ms:.0f}ms p95={p95:.0f}ms | err={error_rate:.1f}%")

    # Read/write contention: writers + API readers at the same time
    if not _quiet:
        print("  ... read/write contention test ...")

    contention_errors = {"read": 0, "write": 0}
    contention_times = {"read": [], "write": []}

    def _contention_reader():
        ok, fail, times = 0, 0, []
        for _ in range(10):
            status, _, ms, _ = http_get("/api/chronicle", timeout=15)
            times.append(ms)
            if status == 200:
                ok += 1
            else:
                fail += 1
        return "read", fail, times

    def _contention_writer(wid):
        errors, times = 0, []
        for i in range(10):
            t0 = time.monotonic()
            try:
                conn = sqlite3.connect(DB_PATH, timeout=10)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute(
                    "INSERT INTO chronicle (session_id, event_type, agent_id, content) VALUES (?, ?, ?, ?)",
                    ("stress-test", "stress", f"contention-{wid}", f"Contention {wid}-{i}")
                )
                conn.commit()
                conn.close()
                times.append((time.monotonic() - t0) * 1000)
            except Exception:
                errors += 1
                times.append((time.monotonic() - t0) * 1000)
        return "write", errors, times

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = []
        for i in range(10):
            futures.append(pool.submit(_contention_reader))
            futures.append(pool.submit(_contention_writer, i))
        for f in as_completed(futures):
            kind, errs, times = f.result()
            contention_errors[kind] += errs
            contention_times[kind].extend(times)

    read_err = contention_errors["read"]
    write_err = contention_errors["write"]
    read_avg = sum(contention_times["read"]) / len(contention_times["read"]) if contention_times["read"] else 0
    write_avg = sum(contention_times["write"]) / len(contention_times["write"]) if contention_times["write"] else 0

    record("db_stress", "read-write-contention", read_err + write_err < 5,
           f"reads: avg={read_avg:.0f}ms err={read_err} | writes: avg={write_avg:.0f}ms err={write_err}")

    # Clean up stress test entries
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM chronicle WHERE session_id='stress-test'")
        conn.commit()
        deleted = conn.execute("SELECT changes()").fetchone()[0]
        conn.close()
        record("db_stress", "cleanup", True, f"removed {deleted} stress entries")
    except Exception as e:
        record("db_stress", "cleanup", False, str(e))

    # Integrity check after stress
    try:
        conn = sqlite3.connect(DB_PATH)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()
        record("db_stress", "post-stress-integrity", integrity == "ok", integrity)
    except Exception as e:
        record("db_stress", "post-stress-integrity", False, str(e))


# ═══════════════════════════════════════════════════════════════════
# Phase 5: COUNCIL — streaming pipeline test
# ═══════════════════════════════════════════════════════════════════
def test_council():
    if not _quiet:
        print("\n── PHASE 5: COUNCIL PIPELINE ──")

    # Stream endpoint responds
    status, body, ms, err = http_get("/api/council/stream")
    if status == 200:
        try:
            data = json.loads(body)
            record("council", "stream-endpoint", "status" in data,
                   f"{ms:.0f}ms status={data.get('status')}")
        except json.JSONDecodeError:
            record("council", "stream-endpoint", False, "Invalid JSON")
    else:
        record("council", "stream-endpoint", False, err)

    # History returns array
    status, body, ms, err = http_get("/api/council/history?session=current")
    if status == 200:
        try:
            data = json.loads(body)
            record("council", "history-array", isinstance(data, list),
                   f"{ms:.0f}ms {len(data)} messages")
        except json.JSONDecodeError:
            record("council", "history-array", False, "Invalid JSON")
    else:
        record("council", "history-array", False, err)

    # POST decree + DELETE round-trip
    status, body, ms, err = http_post("/api/council/decree", {
        "content": "__stress_test_ping__", "role": "petitioner"
    })
    record("council", "decree-roundtrip", status == 201, f"{ms:.0f}ms")
    if status == 201:
        try:
            msg_id = json.loads(body).get("id")
            if msg_id:
                req = urllib.request.Request(
                    BASE_URL + f"/api/council/{msg_id}", method="DELETE")
                urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    # Hammer stream endpoint concurrently (simulates multiple UI tabs)
    stream_times = []
    stream_errors = 0
    def _stream_poll():
        s, _, ms, _ = http_get("/api/council/stream")
        return s, ms

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(_stream_poll) for _ in range(30)]
        for f in as_completed(futures):
            s, ms = f.result()
            stream_times.append(ms)
            if s != 200:
                stream_errors += 1

    avg_ms = sum(stream_times) / len(stream_times) if stream_times else 0
    p95 = sorted(stream_times)[int(len(stream_times) * 0.95)] if stream_times else 0
    record("council", "stream-under-load", stream_errors == 0,
           f"30 polls: avg={avg_ms:.0f}ms p95={p95:.0f}ms err={stream_errors}")


# ═══════════════════════════════════════════════════════════════════
# Phase 6: INTEGRITY — files, security, config
# ═══════════════════════════════════════════════════════════════════
def test_integrity():
    if not _quiet:
        print("\n── PHASE 6: INTEGRITY ──")

    critical_files = [
        "server.py", "worker.py", "dm.py", "start.sh",
        "doom-ui.html", "doom-mobile.html", "logo.png",
    ]
    for f in critical_files:
        record("integrity", f"file-{f}", os.path.exists(os.path.join(DOOM_DIR, f)))

    # start.sh executable
    record("integrity", "start.sh-executable",
           os.access(os.path.join(DOOM_DIR, "start.sh"), os.X_OK))

    # SECURITY: no .env in DOOMBOT
    env_exists = os.path.exists(os.path.join(DOOM_DIR, ".env"))
    record("integrity", "no-env-file", not env_exists,
           "SECURITY VIOLATION: .env in DOOMBOT!" if env_exists else "")

    # .gitignore coverage
    gi_path = os.path.join(DOOM_DIR, ".gitignore")
    if os.path.exists(gi_path):
        gi = open(gi_path).read()
        for entry in [".env", "memory.db", ".venv"]:
            record("integrity", f"gitignore-{entry}", entry in gi)

    # Venv + Flask
    venv = os.path.join(DOOM_DIR, ".venv", "bin", "python3")
    if os.path.exists(venv):
        try:
            r = subprocess.run([venv, "-c", "import flask; print(flask.__version__)"],
                               capture_output=True, text=True, timeout=10)
            record("integrity", "flask-importable", r.returncode == 0, r.stdout.strip())
        except Exception as e:
            record("integrity", "flask-importable", False, str(e))

    # No-cache headers on HTML
    try:
        req = urllib.request.Request(BASE_URL + "/mobile")
        resp = urllib.request.urlopen(req, timeout=5)
        cc = resp.headers.get("Cache-Control", "")
        record("integrity", "no-cache-headers", "no-store" in cc or "no-cache" in cc, cc)
    except Exception as e:
        record("integrity", "no-cache-headers", False, str(e))

    # CORS
    try:
        req = urllib.request.Request(BASE_URL + "/api/session", method="OPTIONS")
        resp = urllib.request.urlopen(req, timeout=5)
        cors = resp.headers.get("Access-Control-Allow-Origin", "")
        record("integrity", "cors-headers", cors == "*", f"ACAO={cors}")
    except Exception as e:
        record("integrity", "cors-headers", False, str(e))

    # Port bound
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", 5050))
        s.close()
        record("integrity", "port-5050-bound", True)
    except Exception as e:
        record("integrity", "port-5050-bound", False, str(e))


# ═══════════════════════════════════════════════════════════════════
# Report
# ═══════════════════════════════════════════════════════════════════
def generate_report(as_json=False, suggest_fixes=False):
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed
    failures = [r for r in results if not r["passed"]]

    if as_json:
        print(json.dumps({
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": f"{(passed/total*100):.1f}%" if total else "N/A",
            "results": results,
            "failures": failures,
        }, indent=2))
        return failed

    print(f"\n{'='*60}")
    print(f"  DOOM STRESS TEST REPORT")
    print(f"{'='*60}")
    print(f"  Total: {total}  |  Passed: {passed}  |  Failed: {failed}  |  Rate: {(passed/total*100):.1f}%" if total else "  No tests run")

    if failures:
        print(f"\n  FAILURES:")
        for f in failures:
            print(f"    [{f['category']}] {f['name']}: {f['detail']}")

    if suggest_fixes and failures:
        print(f"\n  SUGGESTED FIXES:")
        for f in failures:
            fix = suggest_fix(f)
            if fix:
                print(f"    [{f['category']}/{f['name']}] {fix}")

    print(f"{'='*60}")
    return failed


def suggest_fix(failure):
    cat = failure["category"]
    name = failure["name"]
    if cat == "health" and "alive" in name:
        return "Restart DOOM: ./start.sh stop && ./start.sh start"
    if "table" in name:
        return "Run: python dm.py wake"
    if cat == "latency" and "SLOW" in failure.get("detail", ""):
        return f"Endpoint too slow — check server.py query for {name}"
    if cat == "load":
        return "Server buckling under load — check threading, DB connection pooling"
    if cat == "db_stress":
        return "DB write contention — check WAL mode, busy_timeout"
    if "no-env" in name:
        return "SECURITY: Remove .env from DOOMBOT immediately!"
    return None


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════
CATEGORIES = {
    "health": test_health,
    "latency": test_latency,
    "load": test_load,
    "db_stress": test_db_stress,
    "council": test_council,
    "integrity": test_integrity,
}


def main():
    parser = argparse.ArgumentParser(description="DOOM Stress Test Suite")
    parser.add_argument("--category", "-c", choices=list(CATEGORIES.keys()),
                        help="Run only this category")
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    parser.add_argument("--fix", action="store_true", help="Include fix suggestions")
    args = parser.parse_args()

    global _quiet
    if args.json:
        _quiet = True
        import io
        _real_stdout = sys.stdout
        sys.stdout = io.StringIO()

    if not _quiet:
        print("╔══════════════════════════════════════╗")
        print("║     DOOM STRESS TEST — FULL LOAD     ║")
        print("╚══════════════════════════════════════╝")

    t0 = time.monotonic()

    if args.category:
        CATEGORIES[args.category]()
    else:
        for cat_fn in CATEGORIES.values():
            cat_fn()

    elapsed = time.monotonic() - t0

    if args.json:
        sys.stdout = _real_stdout

    if not _quiet:
        print(f"\n  Completed in {elapsed:.1f}s")

    failed = generate_report(as_json=args.json, suggest_fixes=args.fix)
    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
