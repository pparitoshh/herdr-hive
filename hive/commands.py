"""Hive subcommands: new, spawn, fix, merge, discard, finish, close, ask, picker, bench, hook, reconcile."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config, state
from . import herdr_ops as H
from . import radio_ops as R

ROLES = Path(__file__).parent / "roles"
PENDING = state.STATE_DIR / "pending"


# --- helpers -----------------------------------------------------------------

def G():
    from . import git_ops  # imported lazily so `hive status` works without git
    return git_ops


def slug(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", branch).strip("-") or "hive"


def worker_branch(hive_branch: str, n: int) -> str:
    # `<hive-branch>/w<N>` cannot coexist with the ref `<hive-branch>` in git,
    # so worker refs live under `hive/<hive-branch>/w<N>`.
    return f"hive/{hive_branch}/w{n}"


def ask(title: str, prompt: str, choices: list[str], allow_text=False, default=None,
        workspace=None, timeout=None) -> str | None:
    """Ask the human. Popup when in herdr; terminal prompt otherwise. None means cancelled."""
    req = {"title": title, "prompt": prompt, "choices": choices, "allow_text": allow_text, "default": default}
    if os.environ.get("HIVE_ASK_MODE") == "inline" or not os.environ.get("HERDR_ENV"):
        from .picker import ask_inline
        ans = ask_inline(req)
    else:
        if timeout is None:
            timeout = config.parse_duration(config.load().ask_timeout)
        ans = H.popup_ask(req, workspace=workspace, timeout=timeout)
    if ans.get("cancelled") or not ans.get("answer"):
        return None
    return ans["answer"]


def pick_harness_model(cfg, who: str, workspace=None, harnesses=None) -> tuple[str, str] | None:
    names = harnesses or list(cfg.harnesses)
    labels = [f"{n} — {cfg.harnesses[n].label}" for n in names]
    pick = ask(f"Hive: {who} harness", f"Harness for the {who}", labels, workspace=workspace)
    if not pick:
        return None
    h = cfg.harnesses[names[labels.index(pick)]]
    model = ask(f"Hive: {who} model", f"{h.label} model for the {who}", h.models, allow_text=True,
                default=h.models[0] if h.models else None, workspace=workspace)
    if not model:
        return None
    return h.name, model


def role_text(name: str, **kw) -> str:
    return (ROLES / f"{name}.md").read_text().format(**kw)


def find_key(obj, key):
    """Depth-first search for the first value of key in nested JSON."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def launch_agent(pane: str, handle: str, harness, model: str, worker: bool, role: str, workspace: str):
    """radio join (handle only) -> start harness in the pane -> inject the role prompt."""
    R.join(handle, pane, workspace=workspace)
    H.pane_rename(pane, handle)
    H.wait_shell(pane)
    H.pane_run(pane, harness.argv(model, worker=worker))
    if not H.wait_agent_ready(pane):
        raise SystemExit(f"hive: {harness.name} did not become ready in pane {pane}")
    H.agent_prompt(pane, role)
    H.wait_agent_ready(pane, timeout_ms=120000)


def msgs_dir(hive) -> Path:
    return Path(hive["worktree"]) / ".hive" / "msgs"


def sys_send(hive, to, type_, text, **kw):
    try:
        R.send(to, type_, hive["name"], text, sender=R.system_handle(hive["name"]),
               msgs_dir=msgs_dir(hive), workspace=hive["workspace_id"], **kw)
    except RuntimeError as e:
        print(f"hive: radio: {e}", file=sys.stderr)


def parse_wn(s: str) -> int:
    m = re.fullmatch(r"w?(\d+)", s.strip())
    if not m:
        raise SystemExit(f"hive: bad worker id {s!r} (use wN)")
    return int(m.group(1))


def _current_hive(conn):
    from .cli import current_hive
    return current_hive(conn)


# --- new / open ----------------------------------------------------------------

def open_hive(repo: Path, workspace_for_popup: str | None, close_ws: str | None = None) -> int:
    git = G()
    conn = state.connect()
    root = git.primary_checkout(repo)
    cfg = config.load(root)
    branches = git.local_branches(root) if hasattr(git, "local_branches") else []
    branch = ask("Hive: branch", f"Branch for the new hive in {root.name} (new branches start from {cfg.base_branch})",
                 branches, allow_text=True, workspace=workspace_for_popup)
    if not branch:
        print("hive: cancelled")
        return 1
    name = slug(branch)
    if state.hive_by_name(conn, name):
        raise SystemExit(f"hive: hive {name} is already open")
    PENDING.mkdir(parents=True, exist_ok=True)
    marker = PENDING / name
    marker.write_text(str(root))
    try:
        return _open_hive_body(conn, git, root, cfg, branch, name, workspace_for_popup, close_ws)
    finally:
        marker.unlink(missing_ok=True)


def _open_hive_body(conn, git, root, cfg, branch, name, workspace_for_popup, close_ws):
    exists = git.branch_exists(root, branch)
    path = str(cfg.worktree_root / root.name / name) if cfg.worktree_root else None
    res = H.worktree_create(str(root), branch, None if exists else cfg.base_branch, path, branch)
    ws = find_key(res, "workspace_id")
    wt = find_key(res, "path") or find_key(res, "worktree_path") or path
    brain_pane = (res.get("root_pane") or {}).get("pane_id") if isinstance(res, dict) else None
    if not ws:
        raise SystemExit(f"hive: unexpected worktree create result: {json.dumps(res)[:400]}")
    if not wt:
        wt = H.workspace_cwd(ws)
    if not brain_pane:
        brain_pane = H.pane_list(ws)[0]["pane_id"]
    wt = str(Path(wt).resolve())
    git.copy_worktreeinclude(root, Path(wt))
    git.add_info_exclude(Path(wt), ".hive/")
    if close_ws:
        H.workspace_close(close_ws)
    hm = pick_harness_model(cfg, "brain", workspace=ws)
    if not hm:
        print("hive: brain harness not chosen; the worktree stays open without a brain")
        return 1
    hname, model = hm
    tabs = H.tab_list(ws)
    if tabs:
        H.tab_rename(tabs[0]["tab_id"], "hive")
    hid = state.add_hive(conn, name=name, repo=str(root), branch=branch, base=cfg.base_branch, worktree=wt,
                         workspace_id=ws, brain_pane=brain_pane, brain_harness=hname, brain_model=model)
    state.event(conn, hid, "open", {"branch": branch})
    brain = R.brain_handle(name)
    role = role_text("brain", hive=name, branch=branch, base=cfg.base_branch, worktree=wt, brain=brain,
                     max_workers=cfg.max_workers)
    launch_agent(brain_pane, brain, cfg.harness(hname), model, worker=False, role=role, workspace=ws)
    open_bench(name, ws, wt)
    H.toast("Hive ready", f"{name}: brain is live ({hname}/{model})", sound="done")
    print(f"hive {name} open in {ws} (brain pane {brain_pane})")
    return 0


def open_bench(name, ws, wt):
    try:
        H.call("plugin", "pane", "open", "--plugin", H.PLUGIN_ID, "--entrypoint", "bench",
               "--placement", "tab", "--workspace", ws, "--cwd", wt, "--env", f"HIVE_NAME={name}")
    except H.HerdrError as e:
        print(f"hive: bench tab did not open: {e}", file=sys.stderr)


def cmd_new(args):
    ws = os.environ.get("HERDR_WORKSPACE_ID")
    cwd = args.cwd or (H.workspace_cwd(ws) if ws else None) or os.getcwd()
    return open_hive(Path(cwd), ws)


# --- spawn / fix ---------------------------------------------------------------

def cmd_spawn(args):
    git = G()
    conn = state.connect()
    hive = _current_hive(conn)
    cfg = config.load(Path(hive["repo"]))
    brain = R.brain_handle(hive["name"])
    live = state.workers(conn, hive["id"], live_only=True)
    if len(live) >= cfg.max_workers:
        msg = f"cap reached: {len(live)}/{cfg.max_workers} workers live. Merge or discard one first."
        state.event(conn, hive["id"], "spawn_refused", {"reason": "cap"})
        print(f"hive: {msg}", file=sys.stderr)
        return 3
    task = Path(args.task_file).resolve()
    if not task.exists():
        raise SystemExit(f"hive: task file not found: {task}")
    title = args.title or task.stem
    hm = pick_harness_model(cfg, f"worker ({title})", workspace=hive["workspace_id"])
    if not hm:
        state.event(conn, hive["id"], "spawn_declined", {"title": title})
        print("hive: spawn declined by the human", file=sys.stderr)
        return 4
    hname, model = hm
    n = state.next_worker_n(conn, hive["id"])
    wb = worker_branch(hive["branch"], n)
    wt = Path(hive["worktree"]).parent / f"{hive['name']}-w{n}"
    wid = state.add_worker(conn, hive_id=hive["id"], n=n, branch=wb, worktree=str(wt), harness=hname,
                           model=model, title=title, task_ref=str(task), status="spawning")
    try:
        git.worktree_add(Path(hive["repo"]), wt, wb, hive["branch"])
        git.copy_worktreeinclude(Path(hive["repo"]), wt)
        target = live[-1]["pane_id"] if live else hive["brain_pane"]
        direction = "down" if live else "right"
        pane = H.split(target, direction, str(wt), env={"HIVE_NAME": hive["name"]})
        state.update(conn, "workers", wid, pane_id=pane)
        handle = R.worker_handle(hive["name"], n)
        role = role_text("worker", n=n, hive=hive["name"], worktree=wt, worker_branch=wb,
                         branch=hive["branch"], handle=handle, brain=brain)
        launch_agent(pane, handle, cfg.harness(hname), model, worker=True, role=role,
                     workspace=hive["workspace_id"])
    except Exception as e:
        state.update(conn, "workers", wid, status="failed")
        state.event(conn, hive["id"], "spawn_failed", {"error": str(e)}, wid)
        raise
    R.send(handle, "task", hive["name"], f"New task: {title}. Read the task file in the ref, do it, commit, report.",
           sender=brain, worker=n, task=str(wid), ref=str(task), kind="new", workspace=hive["workspace_id"])
    state.update(conn, "workers", wid, status="running")
    state.event(conn, hive["id"], "spawn", {"n": n, "harness": hname, "model": model, "title": title}, wid)
    print(f"spawned w{n} ({hname}/{model}) pane {pane} worktree {wt} branch {wb}")
    return 0


def cmd_fix(args):
    conn = state.connect()
    hive = _current_hive(conn)
    w = _live_worker(conn, hive, args.worker)
    text = Path(args.task_file).read_text() if args.task_file else " ".join(args.text or [])
    if not text:
        raise SystemExit("hive: give --task-file or text")
    R.send(R.worker_handle(hive["name"], w["n"]), "task", hive["name"], text, sender=R.brain_handle(hive["name"]),
           msgs_dir=msgs_dir(hive), worker=w["n"], task=str(w["id"]), kind="fix",
           ref=str(Path(args.task_file).resolve()) if args.task_file else None, workspace=hive["workspace_id"])
    state.update(conn, "workers", w["id"], status="fixing")
    state.event(conn, hive["id"], "fix", {"n": w["n"]}, w["id"])
    print(f"fix sent to w{w['n']}")
    return 0


def _live_worker(conn, hive, wn):
    w = state.worker(conn, hive["id"], parse_wn(wn))
    if not w or w["status"] not in state.LIVE:
        raise SystemExit(f"hive: no live worker {wn}")
    return w


# --- merge / discard -------------------------------------------------------------

def remove_worker(conn, hive, w, status, force_branch=False):
    git = G()
    if w["pane_id"]:
        H.pane_close(w["pane_id"])
    R.part(R.worker_handle(hive["name"], w["n"]), workspace=hive["workspace_id"])
    repo = Path(hive["repo"])
    try:
        git.worktree_remove(repo, Path(w["worktree"]), force=force_branch)
    except Exception as e:
        print(f"hive: worktree remove: {e}", file=sys.stderr)
    try:
        git.branch_delete(repo, w["branch"], force=force_branch)
    except Exception as e:
        print(f"hive: branch delete: {e}", file=sys.stderr)
    state.update(conn, "workers", w["id"], status=status, merged_at=time.time() if status == "merged" else None)


def cmd_merge(args):
    git = G()
    conn = state.connect()
    hive = _current_hive(conn)
    w = _live_worker(conn, hive, args.worker)
    if git.has_uncommitted(Path(w["worktree"])):
        sys_send(hive, R.worker_handle(hive["name"], w["n"]), "system",
                 "The brain tried to merge, but your worktree has uncommitted changes. Commit them and report again.",
                 worker=w["n"])
        raise SystemExit(f"hive: w{w['n']} has uncommitted changes; told the worker to commit")
    ok, out = git.merge_no_ff(Path(hive["worktree"]), w["branch"], f"hive: merge w{w['n']} — {w['title']}")
    if not ok:
        state.event(conn, hive["id"], "conflict", {"files": out}, w["id"])
        state.update(conn, "workers", w["id"], status="fixing")
        sys_send(hive, R.worker_handle(hive["name"], w["n"]), "conflict",
                 f"Merge into {hive['branch']} conflicted (merge aborted). Run `git rebase {hive['branch']}`, "
                 f"resolve, run the tests, and report again.\nConflicted files:\n{out}",
                 worker=w["n"], task=str(w["id"]))
        print(f"conflict: w{w['n']} told to rebase on {hive['branch']}\n{out}")
        return 5
    remove_worker(conn, hive, w, "merged", force_branch=False)
    state.event(conn, hive["id"], "merge", {"n": w["n"]}, w["id"])
    print(f"merged w{w['n']} into {hive['branch']}; pane, worktree and branch removed")
    return 0


def cmd_discard(args):
    conn = state.connect()
    hive = _current_hive(conn)
    w = _live_worker(conn, hive, args.worker)
    if not args.yes:
        ans = ask("Hive: discard worker", f"Discard w{w['n']} ({w['title']})? Its branch and worktree are deleted.",
                  ["Keep", "Discard"], workspace=hive["workspace_id"])
        if ans != "Discard":
            print("hive: discard cancelled")
            return 1
    remove_worker(conn, hive, w, "discarded", force_branch=True)
    state.event(conn, hive["id"], "discard", {"n": w["n"]}, w["id"])
    print(f"discarded w{w['n']}")
    return 0


# --- finish / close --------------------------------------------------------------

def cmd_finish(args):
    git = G()
    conn = state.connect()
    hive = _current_hive(conn)
    live = state.workers(conn, hive["id"], live_only=True)
    if live:
        names = ", ".join(f"w{w['n']} [{w['status']}]" for w in live)
        print(f"hive: finish refused; live workers: {names}. Merge or discard each one first.", file=sys.stderr)
        return 6
    choice = args.choice or ask("Hive: finish", f"Hive {hive['name']} is done. Push {hive['branch']}?",
                                ["Push", "Push + PR (gh)", "Nothing"], workspace=hive["workspace_id"])
    if not choice or choice == "Nothing":
        print("finish: nothing pushed")
        state.event(conn, hive["id"], "finish", {"choice": "nothing"})
        return 0
    print(git.push(Path(hive["worktree"]), hive["branch"]))
    if "PR" in choice:
        r = subprocess.run(["gh", "pr", "create", "--fill", "--base", hive["base"], "--head", hive["branch"]],
                           cwd=hive["worktree"], capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())
    state.event(conn, hive["id"], "finish", {"choice": choice})
    return 0


def cmd_close(args):
    conn = state.connect()
    hive = None
    if args.hive:
        hive = state.hive_by_name(conn, args.hive)
    if hive is None:
        hive = _current_hive(conn)
    live = state.workers(conn, hive["id"], live_only=True)
    if live and not args.yes:
        ans = ask("Hive: close", f"{len(live)} worker(s) are live in {hive['name']}. Close anyway? "
                  "Their worktrees and branches are deleted.", ["Cancel", "Close"], workspace=hive["workspace_id"])
        if ans != "Close":
            print("hive: close cancelled")
            return 1
    for w in live:
        remove_worker(conn, hive, w, "discarded", force_branch=True)
    R.part(R.brain_handle(hive["name"]), workspace=hive["workspace_id"])
    state.update(conn, "hives", hive["id"], status="closed")
    state.event(conn, hive["id"], "close", {})
    print(f"hive {hive['name']} closed; the hive worktree {hive['worktree']} is kept")
    return 0


# --- ask / picker / bench ----------------------------------------------------------

def cmd_ask(args):
    conn = state.connect()
    from .cli import current_hive
    hive = current_hive(conn, required=False)
    choices = [c.strip() for c in args.choices.split(",") if c.strip()] if args.choices else []
    ans = ask("Hive: question", args.prompt, choices, allow_text=args.text or not choices,
              workspace=hive["workspace_id"] if hive else None, timeout=config.parse_duration(args.timeout))
    if hive:
        state.event(conn, hive["id"], "ask", {"prompt": args.prompt, "answer": ans})
    if ans is None:
        print("no answer")
        return 2
    print(ans)
    return 0


def cmd_picker(args):
    from . import picker
    return picker.main(args.rest)


def cmd_bench(args):
    if args.open:
        conn = state.connect()
        hive = _current_hive(conn)
        open_bench(hive["name"], hive["workspace_id"], hive["worktree"])
        return 0
    from .bench import app
    return app.main(args.rest)


# --- hooks / reconcile ---------------------------------------------------------------

def cmd_hook(args):
    if args.event != "workspace-created":
        return 0
    cfg = config.load()
    if not cfg.auto_open:
        return 0
    ev = {}
    try:
        ev = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
    except json.JSONDecodeError:
        pass
    ws = find_key(ev, "workspace_id") or os.environ.get("HERDR_WORKSPACE_ID")
    if not ws:
        return 0
    time.sleep(1.0)  # let the workspace settle and hive new write its marker
    conn = state.connect()
    if state.hive_by_workspace(conn, ws):
        return 0
    cwd = H.workspace_cwd(ws)
    if not cwd:
        return 0
    git = G()
    try:
        top = git.repo_root(Path(cwd))
    except Exception:
        return 0  # not a git repo
    if git.is_worker_worktree(top) or state.hive_by_worktree(conn, top):
        return 0
    if PENDING.exists() and any(time.time() - m.stat().st_mtime < 900 for m in PENDING.iterdir()):
        return 0  # a hive is being opened right now; this is its own workspace
    if find_key(ev, "worktree") or "hive-w" in str(top):
        pass
    ans = ask("Hive", f"Open a hive for {top.name}? The plain workspace closes after the hive opens.",
              ["Not now", "Open hive"], workspace=ws)
    if ans != "Open hive":
        return 0
    return open_hive(top, ws, close_ws=ws)


def cmd_reconcile(args):
    """Mark orphans failed after a herdr restart (spec Q6)."""
    conn = state.connect()
    try:
        panes = {p["pane_id"] for p in H.pane_list()}
    except H.HerdrError:
        return 0
    changed = 0
    for h in conn.execute("SELECT * FROM hives WHERE status='open'").fetchall():
        if not Path(h["worktree"]).exists():
            state.update(conn, "hives", h["id"], status="failed")
            changed += 1
            continue
        if h["brain_pane"] not in panes:
            state.event(conn, h["id"], "orphan_brain", {"pane": h["brain_pane"]})
        for w in state.workers(conn, h["id"], live_only=True):
            if w["pane_id"] not in panes or not Path(w["worktree"]).exists():
                state.update(conn, "workers", w["id"], status="failed")
                state.event(conn, h["id"], "orphan", {"n": w["n"]}, w["id"])
                changed += 1
    if not args.quiet:
        print(f"reconciled: {changed} orphan(s) marked failed")
    return 0


# --- registration ----------------------------------------------------------------------

def register(sub) -> None:
    def cmd_config(args):
        path = config.ensure_global()
        cfg = config.load()
        print(f"config: {path}")
        print(f"state:  {state.STATE_DIR / 'hive.db'}")
        for h in cfg.harnesses.values():
            print(f"  {h.name:<10} {h.label:<14} models: {', '.join(h.models)}")
        return 0

    s = sub.add_parser("config", help="show the merged config"); s.set_defaults(func=cmd_config)
    s = sub.add_parser("new", help="open a hive (branch -> worktree -> brain)")
    s.add_argument("--cwd"); s.set_defaults(func=cmd_new)
    s = sub.add_parser("spawn", help="spawn a worker for a task file")
    s.add_argument("--task-file", required=True); s.add_argument("--title"); s.set_defaults(func=cmd_spawn)
    s = sub.add_parser("fix", help="send a fix task to a worker")
    s.add_argument("worker"); s.add_argument("text", nargs="*"); s.add_argument("--task-file")
    s.set_defaults(func=cmd_fix)
    s = sub.add_parser("merge", help="merge a worker into the hive branch"); s.add_argument("worker")
    s.set_defaults(func=cmd_merge)
    s = sub.add_parser("discard", help="discard a worker"); s.add_argument("worker")
    s.add_argument("--yes", action="store_true"); s.set_defaults(func=cmd_discard)
    s = sub.add_parser("finish", help="finish the hive (push / PR)")
    s.add_argument("--choice", choices=["Push", "Push + PR (gh)", "Nothing"]); s.set_defaults(func=cmd_finish)
    s = sub.add_parser("close", help="close the hive"); s.add_argument("--hive")
    s.add_argument("--yes", action="store_true"); s.set_defaults(func=cmd_close)
    s = sub.add_parser("ask", help="ask the human (blocks)")
    s.add_argument("--prompt", required=True); s.add_argument("--choices", default="")
    s.add_argument("--text", action="store_true", help="also allow free text")
    s.add_argument("--timeout", default="30m"); s.set_defaults(func=cmd_ask)
    s = sub.add_parser("picker", help="popup entrypoint (internal)")
    s.add_argument("rest", nargs="*"); s.set_defaults(func=cmd_picker)
    s = sub.add_parser("bench", help="review bench"); s.add_argument("--open", action="store_true")
    s.add_argument("rest", nargs=argparse_remainder()); s.set_defaults(func=cmd_bench)
    s = sub.add_parser("hook", help="internal"); s.add_argument("event"); s.set_defaults(func=cmd_hook)
    s = sub.add_parser("reconcile", help="mark orphans failed"); s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_reconcile)


def argparse_remainder():
    import argparse
    return argparse.REMAINDER
