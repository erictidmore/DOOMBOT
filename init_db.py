#!/usr/bin/env python3
"""Initialize a fresh DOOM memory.db with all required tables."""

import os
import sqlite3
import sys

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.db")


def init_db(db_path=None):
    path = db_path or DB_PATH
    if os.path.exists(path):
        print(f"  memory.db already exists at {path}")
        print(f"  Delete it first if you want a fresh database.")
        return False

    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")

    db.executescript("""
        CREATE TABLE IF NOT EXISTS identity (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS decrees (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'open',
            priority INTEGER DEFAULT 2,
            assigned_to TEXT,
            blocked_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            fulfilled_at TIMESTAMP,
            fulfillment_notes TEXT
        );

        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            type TEXT,
            status TEXT DEFAULT 'idle',
            current_decree TEXT,
            context_pct INTEGER DEFAULT 0,
            spawned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            session_number INTEGER,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            focus TEXT,
            summary TEXT,
            status TEXT DEFAULT 'open'
        );

        CREATE TABLE IF NOT EXISTS archives (
            id TEXT PRIMARY KEY,
            topic TEXT,
            content TEXT,
            source_session TEXT,
            importance INTEGER DEFAULT 3,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS chronicle (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            event_type TEXT,
            agent_id TEXT,
            content TEXT,
            transient INTEGER DEFAULT 0,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS council (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS council_stream (
            status TEXT DEFAULT 'idle',
            content TEXT DEFAULT '',
            meta TEXT
        );

        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT,
            description TEXT,
            path TEXT,
            port INTEGER,
            start_cmd TEXT,
            stop_cmd TEXT,
            status TEXT DEFAULT 'stopped',
            pid INTEGER,
            decree_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS solutions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            problem TEXT,
            solution TEXT,
            decree_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            data TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS bot_output (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT,
            line_number INTEGER,
            content TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS decree_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decree_id TEXT,
            step_number INTEGER,
            description TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Seed identity
    identity = {
        "name": "DOOM",
        "role": "Sovereign Multi-Agent Orchestration Framework",
        "version": "1.0.0",
        "created": "2026-03",
        "runtime": "Claude Code",
        "language": "Python",
        "storage": "SQLite",
        "principle": "Decrees, not tasks. DOOM commands, never executes.",
    }
    for key, value in identity.items():
        db.execute(
            "INSERT OR IGNORE INTO identity (key, value) VALUES (?, ?)",
            (key, value),
        )

    # Seed initial council stream row
    db.execute("INSERT INTO council_stream (status, content, meta) VALUES ('idle', '', NULL)")

    # Register DOOM itself as a project
    db.execute("""
        INSERT OR IGNORE INTO projects (id, name, description, path, port, start_cmd)
        VALUES ('proj-doom', 'DOOM War Room', 'Sovereign command center', ?, 5050,
                'cd ' || ? || ' && source .venv/bin/activate && python server.py')
    """, (os.path.dirname(path), os.path.dirname(path)))

    db.commit()
    db.close()

    print(f"  memory.db initialized at {path}")
    print(f"  Tables: identity, decrees, agents, sessions, archives, chronicle,")
    print(f"          council, projects, solutions, analytics, bot_output, decree_steps")
    print(f"  Identity seeded. Ready for Session 1.")
    return True


if __name__ == "__main__":
    init_db()
