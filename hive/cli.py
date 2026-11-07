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


def _sender() -> str:
    """The message bus handle bound to the pane that runs this command."""
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        raise SystemExit("hive: $HERDR_PANE_ID is not set; msg, inbox and show need a herdr pane")
    row = state.connect().execute("SELECT name FROM handles WHERE pane_id=?", (pane,)).fetchone()
    if row is None:
        raise SystemExit(f"hive: no handle is bound to pane {pane}")
    return row["name"]


def cmd_msg(args) -> int:
    from . import bus
    conn = state.connect()
    hive = current_hive(conn)
    mid = bus.send(args.handle, "chat", hive["name"], args.text, sender=_sender(),
                   ref=args.ref, reply_required=args.reply_required, workspace=hive["workspace_id"])
    print(f"sent {mid}")
    return 0


def cmd_inbox(args) -> int:
    from . import bus
    rows = bus.inbox(_sender(), conn=state.connect())
    if not rows:
        print("(empty)")
        return 0
    for r in rows:
        first = (r["body"] or "").splitlines()[0]
        mark = "R" if r["reply_required"] else " "
        print(f"{r['id']:<4} {r['status']:<9} from={r['sender']} {mark} {first}")
    return 0


def cmd_show(args) -> int:
    from . import bus
    row = bus.show(args.id)
    if row is None:
        raise SystemExit(f"hive: no message {args.id}")
    print(bus.envelope(row))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hive", description="brain + workers per herdr workspace")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="show hives and workers")
    s.add_argument("--json", action="store_true")
    s.add_argument("--all", action="store_true", help="every open hive, not only this one")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("msg", help="send a message to a handle (agents: put the HIVE/1 line first)")
    s.add_argument("handle")
    s.add_argument("text")
    s.add_argument("--ref")
    s.add_argument("--reply-required", action="store_true")
    s.set_defaults(func=cmd_msg)

    s = sub.add_parser("inbox", help="messages addressed to this pane's handle")
    s.set_defaults(func=cmd_inbox)

    s = sub.add_parser("show", help="show one message by id")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)

    from . import commands  # registers the remaining subcommands
    commands.register(sub)
    return p


def main(argv=None) -> int:
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    try:
        code = int(args.func(args) or 0)
    except KeyboardInterrupt:
        code = 130
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        if e.code and not isinstance(e.code, int):  # argparse errors print themselves
            print(e.code, file=sys.stderr)
    except ValueError as e:  # bus.BusError and other bad-input errors
        print(f"hive: {e}", file=sys.stderr)
        code = 1
    finally:
        if args.cmd not in ("picker", "hook"):  # these run inside herdr, not a shell command
            try:
                from . import bus  # push every pending message
                bus.flush()
            except Exception:
                pass
    return code


if __name__ == "__main__":
    sys.exit(main())
