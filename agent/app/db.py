"""SQLite state. Lives on the Sprite disk, so a checkpoint restore rewinds it too."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

from .config import DB_PATH

_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")

_conn.executescript(
    """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT, message_id TEXT, thread_id TEXT, sender TEXT, subject TEXT,
  mode TEXT, checkpoint TEXT, status TEXT DEFAULT 'running', reply TEXT, model TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, created_at TEXT, kind TEXT, summary TEXT, data TEXT, undone INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, created_at TEXT, due_at TEXT, text TEXT, status TEXT DEFAULT 'pending',
  thread_id TEXT, message_id TEXT, recipient TEXT
);
CREATE TABLE IF NOT EXISTS processed (key TEXT PRIMARY KEY, created_at TEXT);
CREATE TABLE IF NOT EXISTS lives (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, created_at TEXT, question TEXT, status TEXT, thread_id TEXT, message_id TEXT,
  recipient TEXT, summary TEXT, chosen TEXT
);
CREATE TABLE IF NOT EXISTS restores (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, version TEXT, run_id INTEGER, tenant TEXT
);
CREATE TABLE IF NOT EXISTS prefs (
  tenant TEXT PRIMARY KEY, tz TEXT
);
CREATE TABLE IF NOT EXISTS timelines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  life_id INTEGER, label TEXT, option TEXT, sprite_name TEXT, status TEXT, report TEXT, error TEXT
);
"""
)
for _table, _col in (("runs", "tenant"), ("lives", "tenant"), ("reminders", "draft_id"), ("runs", "flags"),
                     ("reminders", "context"), ("reminders", "link")):
    try:
        _conn.execute(f"ALTER TABLE {_table} ADD COLUMN {_col} TEXT")
    except sqlite3.OperationalError:
        pass  # already migrated

# Reminders are AgentMail scheduled drafts ('scheduled'); 'pending' is the fallback that /tick sends.
UPCOMING = "m.status IN ('pending','scheduled') AND m.due_at > strftime('%Y-%m-%dT%H:%M:%S+00:00','now')"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def q(sql: str, *args) -> list[sqlite3.Row]:
    with _lock:
        return _conn.execute(sql, args).fetchall()


def one(sql: str, *args):
    rows = q(sql, *args)
    return rows[0] if rows else None


def ex(sql: str, *args) -> int:
    with _lock:
        cur = _conn.execute(sql, args)
        return cur.lastrowid


def claim(key: str) -> bool:
    """Idempotency: returns True the first time a key is seen."""
    with _lock:
        try:
            _conn.execute("INSERT INTO processed(key, created_at) VALUES (?, ?)", (key, now()))
            return True
        except sqlite3.IntegrityError:
            return False


def add_action(run_id: int, kind: str, summary: str, data: dict) -> int:
    return ex(
        "INSERT INTO actions(run_id, created_at, kind, summary, data) VALUES (?,?,?,?,?)",
        run_id, now(), kind, summary, json.dumps(data),
    )
