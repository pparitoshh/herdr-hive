"""Retrain log for routing cases (routing step 3b, task items 4 and 5).

The log is ON by default (user change): `routing.save_cases` is true, and
`$HIVE_SAVE_CASES=0` turns it off. When it is off, `log_case`
and `log_outcome` write nothing and create no `route_log/` directory.
`hive router export` still reads old files and writes 0 rows when there are
none.

One case line per spawn that the router did not decide:

    {"ts", "worker_id", "repo", "title", "task_text" (first 2000 chars),
     "state_features", "state", "instructions",
     "options": {tier: description}, "probs", "router_pick", "confidence",
     "reason", "human_pick", "final_tier", "outcome": null}

A line lands when `decided_by` is "human", or the confidence is below
`min_confidence`, or the router failed (every human decision passes through
here; the router-decided auto case has no human input and is not logged).

Outcome: of the two schemas in the task, the simpler one wins: append one
outcome line after merge, discard or fix instead of rewriting the case line.

    {"type": "outcome", "worker_id": N, "ts": ..., "outcome": "merge"}

Rewriting one line of a shared JSONL across processes needs byte range locks,
so the code appends. `export` and `cases` join the last outcome per worker id.

`hive router export [--since YYYY-MM-DD] [--out F]` writes training rows in
the decision-model `choice` format (one row per case with a label):

    {"state", "instructions", "options", "label"}

Label = `human_pick`, or `final_tier` when no human picked and the last
outcome for the worker is "merge".
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

from . import state


def save_enabled(cfg=None) -> bool:
    """Is the retrain log on? $HIVE_SAVE_CASES wins over the config file."""
    env = os.environ.get("HIVE_SAVE_CASES")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    if cfg is None:
        from . import config
        cfg = config.load()
    return bool(getattr(cfg.routing, "save_cases", False))


def log_dir() -> Path:
    p = state.STATE_DIR / "route_log"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _date(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _append(row: dict, ts: float) -> Path:
    path = log_dir() / f"{_date(ts)}.jsonl"
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(line)
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return path


def should_log(route, min_confidence: float) -> bool:
    """A human decision (also after a router failure or in ask mode) or a
    low-confidence router decision gets a case line (task item 4)."""
    if route.decided_by == "human":
        return True
    return bool(route.probs) and route.confidence < min_confidence


def log_case(hive, worker_id: int, title: str, task_text: str, request: dict,
             route, cfg, reason: str, state_features: dict, human_pick: str | None,
             now: float | None = None) -> Path | None:
    """Append one case line when logging is on and the route needs it.
    Returns the path, or None when the log is off or not needed."""
    if not save_enabled(cfg) or not should_log(route, cfg.routing.min_confidence):
        return None
    now = now if now is not None else time.time()
    q = request["questions"][0]
    probs = {t: round(float(p), 6) for t, p in route.probs.items()}
    router_pick = max(probs, key=probs.get) if probs else None
    row = {
        "ts": now,
        "worker_id": worker_id,
        "repo": hive["repo"],
        "title": title,
        "task_text": (task_text or "")[:2000],
        "state_features": {"title": state_features.get("title"),
                           "file_count": state_features.get("file_count"),
                           "task_chars": len(task_text or "")},
        "state": request["state"],
        "instructions": q["instructions"],
        "options": dict(q["criteria"]),
        "probs": probs,
        "router_pick": router_pick,
        "confidence": round(float(route.confidence), 6),
        "reason": reason,
        "human_pick": human_pick,
        "final_tier": route.tier,
        "outcome": None,
    }
    return _append(row, now)


def log_outcome(worker_id: int, outcome: str, cfg=None, now: float | None = None) -> Path | None:
    """Append an outcome line after merge, discard or fix. Returns None when
    the retrain log is off."""
    if not save_enabled(cfg):
        return None
    now = now if now is not None else time.time()
    return _append({"type": "outcome", "worker_id": worker_id, "ts": now,
                    "outcome": outcome}, now)


def cases(since: str | None = None) -> list[dict]:
    """All case lines. When one worker id has several case lines, the last
    wins. Outcome lines join separately (outcome lookups use them)."""
    d = log_dir()
    rows: dict[int, dict] = {}
    paths = sorted(d.glob("*.jsonl"), key=lambda p: p.name)
    if since:
        paths = [p for p in paths if p.name[:10] >= since]
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") == "outcome" or "worker_id" not in row:
                    continue
                rows[row["worker_id"]] = row
    return list(rows.values())


def outcomes() -> dict[int, str]:
    """worker_id -> the last outcome line value."""
    d = log_dir()
    out: dict[int, str] = {}
    for path in sorted(d.glob("*.jsonl")):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") == "outcome":
                    out[row["worker_id"]] = row["outcome"]
    return out


def export(since: str | None = None, out: Path | None = None) -> tuple[Path, int]:
    """Write training rows (one per case with a label) as JSONL. Returns
    (path, rows written)."""
    out = out or log_dir() / "export.jsonl"
    outs = outcomes()
    rows = []
    for case in cases(since):
        label = case.get("human_pick")
        if not label and case.get("outcome") is None:
            label = case.get("final_tier") if outs.get(case["worker_id"]) == "merge" else None
        if not label:
            continue
        rows.append({"state": case["state"], "instructions": case["instructions"],
                     "options": case["options"], "label": label})
    with open(out, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return out, len(rows)
