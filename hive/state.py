"""SQLite state: who is where (spec §9)."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("HIVE_STATE_DIR", "~/.local/state/herdr-hive")).expanduser()

SCHEMA = """
CREATE TABLE IF NOT EXISTS hives(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, repo TEXT, branch TEXT, base TEXT, worktree TEXT,
  workspace_id TEXT, brain_pane TEXT, brain_harness TEXT, brain_model TEXT,
  status TEXT DEFAULT 'open', created_at REAL);
CREATE TABLE IF NOT EXISTS workers(
  id INTEGER PRIMARY KEY, hive_id INTEGER REFERENCES hives(id), n INTEGER, branch TEXT, worktree TEXT,
  pane_id TEXT, harness TEXT, model TEXT, title TEXT, task_ref TEXT,
  status TEXT DEFAULT 'spawning', created_at REAL, merged_at REAL, UNIQUE(hive_id, n));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, hive_id INTEGER, worker_id INTEGER, kind TEXT, payload_json TEXT, ts REAL);
"""

LIVE = ("spawning", "running", "reported", "fixing")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or STATE_DIR / "hive.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def event(conn, hive_id, kind, payload=None, worker_id=None):
    conn.execute(
        "INSERT INTO events(hive_id, worker_id, kind, payload_json, ts) VALUES(?,?,?,?,?)",
        (hive_id, worker_id, kind, json.dumps(payload or {}), time.time()),
    )


def add_hive(conn, **kw) -> int:
    kw.setdefault("created_at", time.time())
    cols = ",".join(kw)
    cur = conn.execute(f"INSERT INTO hives({cols}) VALUES({','.join('?' * len(kw))})", tuple(kw.values()))
    return cur.lastrowid


def update(conn, table: str, row_id: int, **kw):
    sets = ",".join(f"{k}=?" for k in kw)
    conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*kw.values(), row_id))


def hive_by_name(conn, name):
    return conn.execute("SELECT * FROM hives WHERE name=? AND status='open'", (name,)).fetchone()


def hive_by_worktree(conn, path) -> sqlite3.Row | None:
    """The open hive whose hive worktree or one of whose worker worktrees contains path."""
    p = str(Path(path).resolve())
    for h in conn.execute("SELECT * FROM hives WHERE status='open'"):
        if p == h["worktree"] or p.startswith(h["worktree"] + os.sep):
            return h
    for w in conn.execute("SELECT * FROM workers"):
        if w["worktree"] and (p == w["worktree"] or p.startswith(w["worktree"] + os.sep)):
            h = conn.execute("SELECT * FROM hives WHERE id=? AND status='open'", (w["hive_id"],)).fetchone()
            if h:
                return h
    return None


def hive_by_workspace(conn, ws):
    return conn.execute("SELECT * FROM hives WHERE workspace_id=? AND status='open'", (ws,)).fetchone()


def workers(conn, hive_id, live_only=False):
    q = "SELECT * FROM workers WHERE hive_id=?"
    if live_only:
        q += f" AND status IN ({','.join('?' * len(LIVE))})"
        return conn.execute(q + " ORDER BY n", (hive_id, *LIVE)).fetchall()
    return conn.execute(q + " ORDER BY n", (hive_id,)).fetchall()


def worker(conn, hive_id, n):
    return conn.execute("SELECT * FROM workers WHERE hive_id=? AND n=?", (hive_id, n)).fetchone()


def next_worker_n(conn, hive_id) -> int:
    row = conn.execute("SELECT MAX(n) FROM workers WHERE hive_id=?", (hive_id,)).fetchone()
    return (row[0] or 0) + 1


def add_worker(conn, **kw) -> int:
    kw.setdefault("created_at", time.time())
    cols = ",".join(kw)
    cur = conn.execute(f"INSERT INTO workers({cols}) VALUES({','.join('?' * len(kw))})", tuple(kw.values()))
    return cur.lastrowid
