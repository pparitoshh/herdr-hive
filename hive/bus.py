"""Minimal message bus over SQLite: no network, no daemon, stdlib only.

Handles have the form `name.<hive>`; the hive name is the part after the
first dot. Messages live in the `messages` table. Delivery is an inline
`herdr agent prompt` to the target pane when its agent is idle (spec plan:
minimal message bus, envelope credit to AgentRadio).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from hive import herdr_ops, state
from hive.radio_ops import header, parse_header  # noqa: F401  (re-exports)

MAX_BODY = 4096
MAX_ATTEMPTS = 5
BUSY = ("working", "blocked")


def brain_handle(hive: str) -> str:
    return f"brain.{hive}"


def worker_handle(hive: str, n: int) -> str:
    return f"w{n}.{hive}"


def bench_handle(hive: str) -> str:
    return f"bench.{hive}"


def system_handle(hive: str) -> str:
    return f"hive.{hive}"


def hive_of(handle: str) -> str | None:
    """The hive name of a handle: the part after the first dot."""
    return handle.split(".", 1)[1] if "." in handle else None


def _conn(conn=None):
    if conn is not None:
        return conn
    return state.connect()


def _hive(conn, handle):
    name = hive_of(handle)
    h = state.hive_by_name(conn, name) if name else None
    if h is None:
        raise ValueError(f"unknown hive in handle: {handle}")
    return h


# --- presence ---------------------------------------------------------------

def join(handle: str, pane: str, role: str | None = None, workspace: str | None = None,
         conn=None) -> None:
    """Bind a pane to a handle so others can send to it."""
    c = _conn(conn)
    h = _hive(c, handle)
    c.execute(
        "INSERT OR REPLACE INTO handles(hive_id, name, pane_id, created_at) VALUES(?,?,?,?)",
        (h["id"], handle, pane, time.time()),
    )


def part(handle: str, workspace: str | None = None, conn=None) -> None:
    """Remove a handle; messages to it are no longer deliverable."""
    c = _conn(conn)
    h = _hive(c, handle)
    c.execute("DELETE FROM handles WHERE hive_id=? AND name=?", (h["id"], handle))


def handles(workspace: str | None = None, conn=None) -> str:
    """String, one line `<handle> <pane_id>` per live handle, sorted."""
    c = _conn(conn)
    rows = c.execute(
        "SELECT handles.name, handles.pane_id FROM handles"
        " JOIN hives ON hives.id = handles.hive_id"
        " WHERE hives.status='open' ORDER BY handles.name"
    ).fetchall()
    return "".join(f"{r['name']} {r['pane_id']}\n" for r in rows)


# --- rules ------------------------------------------------------------------

def _check_sender(conn, sender: str) -> None:
    if not sender:
        raise ValueError("sender required")
    if sender.startswith(("hive.", "bench.")):
        return  # internal senders only come from code, trust the process
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return  # headless caller (tests, scripts)
    row = conn.execute("SELECT name FROM handles WHERE pane_id=?", (pane,)).fetchone()
    if row and row["name"] != sender:
        raise ValueError(f"sender {sender} is not the handle of this pane ({row['name']})")


def _ref_ok(conn, hive_id: int, ref: str) -> bool:
    p = str(Path(ref).resolve())
    ws = [r["worktree"] for r in
          conn.execute("SELECT worktree FROM hives WHERE id=?", (hive_id,))]
    ws += [r["worktree"] for r in
           conn.execute("SELECT worktree FROM workers WHERE hive_id=?", (hive_id,))]
    for root in ws:
        if root is None:
            continue
        root = str(Path(root).resolve())
        if p == root or p.startswith(root + os.sep):
            return True
    return False


# --- envelope ---------------------------------------------------------------

def envelope(row) -> str:
    reply = " reply=required" if row["reply_required"] else ""
    out = [f"[HIVE_MESSAGE id={row['id']} from={row['sender']}{reply}]", row["body"]]
    if row["ref"]:
        out.append(f"Ref: {row['ref']}")
    out.append(f"[END_HIVE_MESSAGE id={row['id']}]")
    return "\n".join(out)


# --- send -------------------------------------------------------------------

def send(to: str, type_: str, hive: str, text: str, *, sender: str,
         msgs_dir: Path | None = None, worker: int | None = None, task: str | None = None,
         ref: str | None = None, reply_required: bool = False, workspace: str | None = None,
         conn=None, **extra) -> str:
    """Store one message and make one delivery try. Returns the message id as text."""
    c = _conn(conn)
    h = _hive(c, to)
    pane_row = c.execute(
        "SELECT pane_id FROM handles WHERE hive_id=? AND name=?", (h["id"], to)).fetchone()
    if pane_row is None:
        raise ValueError(f"unknown target: {to}")
    _check_sender(c, sender)
    if ref is not None and not _ref_ok(c, h["id"], ref):
        raise ValueError(f"ref outside the hive or worker worktrees: {ref}")

    head = header(type_, hive, worker=worker, task=task, **extra)
    spilled = len(text) > MAX_BODY
    body = head + "\n" + (text[:300] if spilled else text)
    cur = c.execute(
        "INSERT INTO messages(hive_id, sender, target, body, ref, reply_required, status, created_at)"
        " VALUES(?,?,?,?,?,?,'pending',?)",
        (h["id"], sender, to, body, ref, 1 if reply_required else 0, time.time()),
    )
    mid = cur.lastrowid
    if spilled:
        d = Path(msgs_dir) if msgs_dir else Path(h["worktree"]) / ".hive" / "msgs"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{mid}.md"
        path.write_text(text)
        os.chmod(path, 0o600)
        c.execute("UPDATE messages SET ref=? WHERE id=?", (str(path), mid))
    _deliver(c, show(mid, conn=c))
    return str(mid)


# --- delivery ---------------------------------------------------------------

def _deliver(conn, row) -> str:
    """One delivery try for one row. Returns the new status."""
    pane_row = conn.execute(
        "SELECT pane_id FROM handles WHERE hive_id=? AND name=?",
        (row["hive_id"], row["target"]),
    ).fetchone()
    st = herdr_ops.pane_state(pane_row["pane_id"]) if pane_row else None
    if st is None:  # pane unusable or gone
        conn.execute("UPDATE messages SET status='failed' WHERE id=?", (row["id"],))
        return "failed"
    if st.get("agent_status") in BUSY or st.get("focused"):
        return "pending"  # keep the place in the queue, do not count an attempt
    try:
        herdr_ops.agent_prompt(pane_row["pane_id"], envelope(row))
    except Exception:
        attempts = (row["attempts"] or 0) + 1
        status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
        conn.execute("UPDATE messages SET attempts=?, status=? WHERE id=?",
                     (attempts, status, row["id"]))
        return status
    conn.execute("UPDATE messages SET status='delivered', delivered_at=? WHERE id=?",
                 (time.time(), row["id"]))
    return "delivered"


def flush(conn=None) -> int:
    """Deliver pending rows, oldest first. Returns the count of deliveries."""
    c = _conn(conn)
    n = 0
    skip: set[str] = set()
    for row in c.execute(
        "SELECT * FROM messages WHERE status='pending' ORDER BY id"
    ).fetchall():
        if row["target"] in skip:
            continue  # keep order: an earlier row for this target could not go
        if _deliver(c, row) == "delivered":
            n += 1
        else:
            skip.add(row["target"])
    return n


# --- read -------------------------------------------------------------------

def inbox(handle: str, conn=None) -> list:
    c = _conn(conn)
    h = _hive(c, handle)
    return c.execute(
        "SELECT * FROM messages WHERE hive_id=? AND target=? ORDER BY id",
        (h["id"], handle),
    ).fetchall()


def show(id, conn=None):
    c = _conn(conn)
    return c.execute("SELECT * FROM messages WHERE id=?", (int(id),)).fetchone()
