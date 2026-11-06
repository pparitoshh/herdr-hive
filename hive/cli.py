"""`hive` CLI entry (spec §12)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import config, state


def _cwd() -> Path:
    return Path(os.environ.get("HIVE_CWD") or os.getcwd()).resolve()


def current_hive(conn, required=True):
    """Find the hive for the caller: $HIVE_NAME, the herdr workspace, then the cwd."""
    h = None
    if os.environ.get("HIVE_NAME"):
        h = state.hive_by_name(conn, os.environ["HIVE_NAME"])
    if h is None and os.environ.get("HERDR_WORKSPACE_ID"):
        h = state.hive_by_workspace(conn, os.environ["HERDR_WORKSPACE_ID"])
    if h is None:
        h = state.hive_by_worktree(conn, _cwd())
    if h is None and required:
        raise SystemExit("hive: no open hive here (run `hive new` from a git workspace)")
    return h


def cmd_status(args) -> int:
    conn = state.connect()
    hives = [current_hive(conn, required=False)] if not args.all else None
    if not args.all and hives == [None]:
        hives = None
    if hives is None:
        hives = conn.execute("SELECT * FROM hives WHERE status='open' ORDER BY id").fetchall()
    out = []
    for h in hives:
        ws = [dict(w) for w in state.workers(conn, h["id"])]
        out.append({**dict(h), "workers": ws})
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    if not out:
        print("no open hives")
        return 0
    for h in out:
        print(f"hive {h['name']}  branch={h['branch']}  base={h['base']}  brain={h['brain_harness']}/{h['brain_model']}  pane={h['brain_pane']}")
        print(f"  worktree {h['worktree']}")
        for w in h["workers"]:
            print(f"  w{w['n']:<3} {w['status']:<10} {w['harness']}/{w['model']}  pane={w['pane_id']}  {w['title'] or ''}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hive", description="brain + workers per herdr workspace")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="show hives and workers")
    s.add_argument("--json", action="store_true")
    s.add_argument("--all", action="store_true", help="every open hive, not only this one")
    s.set_defaults(func=cmd_status)

    from . import commands  # registers the remaining subcommands
    commands.register(sub)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
