#!/usr/bin/env python3
"""
DOOM Notify — Push notifications to phone via ntfy.sh

Usage:
    python notify.py --setup          # Generate topic, show subscribe URL
    python notify.py --test           # Send a test notification
    python notify.py "Title" "Message"  # Send a notification

From code:
    from notify import send_notification
    send_notification("Decree Fulfilled", "dc-1234 complete", priority="high")
"""

import argparse
import json
import os
import secrets
import sqlite3
import sys
import urllib.request
import urllib.error

DOOMBOT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DOOMBOT_DIR, "memory.db")
NTFY_BASE = "https://ntfy.sh"


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_topic():
    """Get the ntfy topic from identity table."""
    conn = get_db()
    row = conn.execute("SELECT value FROM identity WHERE key='ntfy_topic'").fetchone()
    conn.close()
    if row:
        return row["value"]
    return None


def set_topic(topic):
    """Store the ntfy topic in identity table."""
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO identity (key, value, updated_at) VALUES ('ntfy_topic', ?, datetime('now'))",
        (topic,)
    )
    conn.commit()
    conn.close()


def send_notification(title, message, priority="default", tags=None):
    """Send a push notification via ntfy.sh.

    Priority: urgent (alarm sound), high, default, low, min
    Tags: list of emoji shortcodes, e.g. ["skull", "fire"]
    """
    topic = get_topic()
    if not topic:
        print("[NOTIFY] No topic configured. Run: python notify.py --setup")
        return False

    url = f"{NTFY_BASE}/{topic}"
    headers = {
        "Title": title[:256].encode("ascii", errors="replace").decode("ascii"),
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = ",".join(tags)

    data = message.encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.URLError as e:
        print(f"[NOTIFY] Failed to send: {e}")
        return False
    except Exception as e:
        print(f"[NOTIFY] Error: {e}")
        return False


def setup():
    """Interactive setup — generate topic and show subscribe URL."""
    existing = get_topic()
    if existing:
        print(f"  Existing topic: {existing}")
        print(f"  Subscribe URL: {NTFY_BASE}/{existing}")
        resp = input("  Generate new topic? (y/N): ").strip().lower()
        if resp != "y":
            return

    topic = f"doom-{secrets.token_hex(4)}"
    set_topic(topic)

    print()
    print("=" * 50)
    print("  DOOM NOTIFICATIONS — SETUP COMPLETE")
    print("=" * 50)
    print()
    print(f"  Topic: {topic}")
    print(f"  Subscribe URL: {NTFY_BASE}/{topic}")
    print()
    print("  To receive notifications on your phone:")
    print("  1. Install ntfy app (iOS App Store / Google Play)")
    print(f"  2. Subscribe to topic: {topic}")
    print(f"  3. Or open: {NTFY_BASE}/{topic}")
    print()
    print("  Sending test notification...")
    ok = send_notification(
        "DOOM ONLINE",
        "Notifications are working. DOOM sees all.",
        priority="high",
        tags=["skull"]
    )
    if ok:
        print("  Test notification sent!")
    else:
        print("  Failed to send test notification.")


def test():
    """Send a test notification."""
    topic = get_topic()
    if not topic:
        print("[NOTIFY] No topic configured. Run: python notify.py --setup")
        return
    ok = send_notification(
        "DOOM TEST",
        "If you see this, notifications are working.",
        priority="default",
        tags=["white_check_mark"]
    )
    print("Sent!" if ok else "Failed!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DOOM Notify — Push notifications")
    parser.add_argument("--setup", action="store_true", help="Setup ntfy topic")
    parser.add_argument("--test", action="store_true", help="Send test notification")
    parser.add_argument("title", nargs="?", help="Notification title")
    parser.add_argument("message", nargs="?", help="Notification message")
    parser.add_argument("--priority", default="default", help="Priority: urgent, high, default, low, min")
    args = parser.parse_args()

    if args.setup:
        setup()
    elif args.test:
        test()
    elif args.title and args.message:
        ok = send_notification(args.title, args.message, priority=args.priority)
        print("Sent!" if ok else "Failed!")
    else:
        parser.print_help()
