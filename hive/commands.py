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
from . import bus
from .takeover import decide_tier, human_prompt, RouterUnavailable

ROLES = Path(__file__).parent / "roles"
PENDING = state.STATE_DIR / "pending"


# --- routing step 3b helpers -----------------------------------------------------------

def _tier_choices(tier_names: list[str], default: str | None) -> list[str]:
    """Picker choices: the router pick (default) first."""
    if default in tier_names:
        return [default] + [t for t in tier_names if t != default]
    return tier_names


def repo_file_count(repo: str | Path) -> int:
    """Files tracked by git in the repo. 0 when git fails."""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=str(repo), capture_output=True,
                             text=True, timeout=10).stdout
        return sum(1 for line in out.splitlines() if line.strip())
    except (OSError, subprocess.TimeoutExpired):
        return 0


def route_for_spawn(hive, cfg, task_text: str, title: str, flag_tier: str | None,
                    file_count: int | None = None):
    """Decide the tier for one spawn (takeover.decide_tier). Returns
    (route, request, human_reason) where `request` is the exact /v1/decide
    request the router saw. The request is rebuilt deterministically when the
    router never ran (--tier or a failed router), so log_case always has one.
    `human_reason` is the text the picker popup got (takeover reason), None
    when the human was never asked.

    Human cancel is not a tier: the caller must stop the spawn.
    The router client asks the socket server (router.ask_server);
    RouterUnavailable covers missing deps, a missing model, a dead server and
    the configured timeout.
    """
    from . import router
    captured: dict = {}
    criteria = router.tier_criteria(cfg)

    def ask_router(text: str, title2: str | None) -> dict[str, float]:
        request = router.task_question(text, title=title2,
                                       file_count=file_count if file_count is not None
                                       else repo_file_count(hive["repo"]),
                                       criteria=criteria)
        try:
            resp = router.ask_server(request, timeout=float(cfg.routing.timeout_s))
        except router.RouterError as e:
            raise RouterUnavailable(str(e)) from e
        probs = resp["answers"][0]["probs"]
        captured["request"] = request
        return {t: float(probs.get(t, 0.0)) for t in cfg.tiers}

    def ask_human(reason: str, probs: dict[str, float], default: str | None):
        captured["human_reason"] = reason
        choices = _tier_choices(list(cfg.tiers), default)
        return ask("Hive: worker tier", human_prompt(reason, probs, default), choices,
                   workspace=hive["workspace_id"])

    route = decide_tier(task_text, title, list(cfg.tiers), cfg.routing.mode,
                        cfg.routing.min_confidence, ask_router, ask_human, flag_tier=flag_tier)
    request = captured.get("request") or router.task_question(
        task_text, title=title,
        file_count=file_count if file_count is not None else repo_file_count(hive["repo"]),
        criteria=criteria)
    return route, request, captured.get("human_reason")


def _route_reason(route, human_reason: str | None, cfg) -> str:
    """Why the case lands in the retrain log. The takeover reason text when
    the human was asked; otherwise the crafted form."""
    if human_reason is not None:
        return human_reason
    if route.decided_by == "human":
        return "router failed; human picked" if not route.probs \
            else "human picked with router probabilities"
    return f"low confidence {route.confidence:.3f} < {cfg.routing.min_confidence:.3f}"


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
    """Ask the human. Popup when in herdr; terminal prompt otherwise. None means cancelled.

    `HIVE_ASK_MODE=plain` forces the numbered terminal prompt, so scripts and
    live tests can answer without a TUI."""
    req = {"title": title, "prompt": prompt, "choices": choices, "allow_text": allow_text, "default": default}
    mode = os.environ.get("HIVE_ASK_MODE", "")
    if mode == "plain":
        from .picker import ask_plain
        ans = ask_plain(req)
    elif mode == "inline" or not os.environ.get("HERDR_ENV"):
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
    models = h.model_choices()
    model = ask(f"Hive: {who} model", f"{h.label} model for the {who}", models, allow_text=True,
                default=models[0] if models else None, workspace=workspace)
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


# --- worker permissions (task L) ---------------------------------------------------

TRUST_DIALOG = re.compile(r"do you trust the files in this folder|trust this folder", re.I)


def worker_perm_argv(harness, worktree: str, hive_worktree: str) -> tuple[list[str], dict[str, str]]:
    """Per-launch permission grant for a worker: extra argv flags and env vars.

    Claude reads a settings file under `<worktree>/.tmp` (`--settings`) plus
    `--add-dir <hive worktree>`. OpenCode reads the OPENCODE_PERMISSION env
    var. Only the hive bus commands and the hive worktree path are allowed;
    nothing is written to a user-level config file.
    """
    if harness.herdr_kind == "claude":
        settings = Path(worktree) / ".tmp" / "hive-perms.json"
        settings.write_text(json.dumps(config.claude_worker_settings()))
        os.chmod(settings, 0o600)
        return config.claude_worker_perm_argv(settings, hive_worktree), {}
    if harness.herdr_kind == "opencode":
        return [], config.opencode_worker_perm_env(hive_worktree)
    return [], {}


def _tell_brain_trust(handle: str, body: str) -> None:
    """Bus message to the brain: the worker waits on the trust dialog.
    Never blocks the launch."""
    hive = bus.hive_of(handle) or ""
    if not hive or handle == bus.brain_handle(hive):
        return  # the brain cannot tell itself
    try:
        bus.send(bus.brain_handle(hive), "system", hive, body,
                 sender=bus.system_handle(hive))
    except bus.BusError as e:
        print(f"hive: bus: {e}", file=sys.stderr)


def blocked_notice(handle: str, pane_id: str) -> callable:
    """on_dialog hook for wait_agent_ready: when the blocked dialog is the
    folder-trust prompt, toast and tell the brain; otherwise a generic toast."""
    def notice(p: dict, text: str) -> None:
        label = p.get("label") or pane_id
        if TRUST_DIALOG.search(text or ""):
            body = f"worker {handle} waits on the trust dialog in pane {label}"
            H.toast("Hive: worker blocked", body)
            _tell_brain_trust(handle, body)
        else:
            H.toast("Hive: agent needs you", f"Answer the dialog in pane {label}")
    return notice


def launch_agent(pane: str, handle: str, harness, model: str, worker: bool, role: str, workspace: str,
                 worktree: str | None = None, hive_worktree: str | None = None):
    """Bus join (handle only) -> start harness in the pane -> inject the role prompt.

    A worker needs TMPDIR=<worktree>/.tmp: OpenCode rejects writes outside the
    worktree and a /tmp write stopped a run (plan §Worker temp dir). With
    `hive_worktree`, a worker also gets a per-launch permission grant: only the
    hive bus commands and the hive worktree path (task L).
    """
    bus.join(handle, pane, workspace=workspace)
    H.pane_rename(pane, handle)
    H.wait_shell(pane)
    argv = harness.argv(model, worker=worker)
    envs: list[str] = []
    notice = None
    if worker and worktree:
        tmp = Path(worktree) / ".tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        envs.append(f"TMPDIR={tmp}")
        if hive_worktree:
            flags, henv = worker_perm_argv(harness, worktree, hive_worktree)
            argv += flags
            envs += [f"{k}={v}" for k, v in henv.items()]
        notice = blocked_notice(handle, pane)
    if envs:
        argv = ["env", *envs, *argv]
    H.pane_run(pane, argv)
    if not H.wait_agent_ready(pane, on_dialog=notice):
        raise SystemExit(f"hive: {harness.name} did not become ready in pane {pane}")
    H.agent_prompt(pane, role)
    H.wait_agent_ready(pane, timeout_ms=120000, on_dialog=notice)


def msgs_dir(hive) -> Path:
    return Path(hive["worktree"]) / ".hive" / "msgs"


def sys_send(hive, to, type_, text, **kw):
    try:
        bus.send(to, type_, hive["name"], text, sender=bus.system_handle(hive["name"]),
                 msgs_dir=msgs_dir(hive), workspace=hive["workspace_id"], **kw)
    except bus.BusError as e:
        print(f"hive: bus: {e}", file=sys.stderr)


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
    git.add_info_exclude(Path(wt), ".tmp/")
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
    brain = bus.brain_handle(name)
    role = role_text("brain", hive=name, branch=branch, base=cfg.base_branch, worktree=wt, brain=brain,
                     max_workers=cfg.max_workers)
    launch_agent(brain_pane, brain, cfg.harness(hname), model, worker=False, role=role, workspace=ws)
    hive_row = state.hive_by_workspace(conn, ws)
    pre_spawn_errors = _pre_spawn_workers(conn, git, cfg, hive_row)
    open_bench(name, ws, wt)
    H.toast("Hive ready", f"{name}: brain is live ({hname}/{model})", sound="done")
    print(f"hive {name} open in {ws} (brain pane {brain_pane})")
    for tier_name, err in pre_spawn_errors:
        print(f"hive: pre-spawn {tier_name} worker failed: {err}", file=sys.stderr)
    return 0


def _pre_spawn_workers(conn, git, cfg, hive_row):
    """Start one idle worker per tier at hive open so tasks assign instantly."""
    errors = []
    for tier_name in cfg.tiers:
        try:
            _launch_worker(conn, git, cfg, hive_row, tier_name)
        except Exception as e:
            errors.append((tier_name, str(e)))
    return errors


def open_bench(name, ws, wt):
    try:
        H.call("plugin", "pane", "open", "--plugin", H.PLUGIN_ID, "--entrypoint", "bench",
               "--placement", "tab", "--workspace", ws, "--cwd", wt, "--env", f"HIVE_NAME={name}")
        for _ in range(20):
            bench = [p for p in H.pane_list(ws) if (p.get("label") or "").startswith("Review bench")]
            if bench:
                H.tab_rename(bench[0]["tab_id"], "review")
                break
            time.sleep(0.3)
    except H.HerdrError as e:
        print(f"hive: bench tab did not open: {e}", file=sys.stderr)


def cmd_new(args):
    ws = os.environ.get("HERDR_WORKSPACE_ID")
    cwd = args.cwd or (H.workspace_cwd(ws) if ws else None) or os.getcwd()
    return open_hive(Path(cwd), ws)


# --- spawn / fix ---------------------------------------------------------------

def _launch_worker(conn, git, cfg, hive, tier_name, task=None, title=None, route_json=None):
    """Create a worker: row + worktree + branch + pane + agent. With a task,
    send it and mark running; without one, leave the worker idle."""
    brain = bus.brain_handle(hive["name"])
    tier_cfg = cfg.tiers[tier_name]
    hname, model = tier_cfg.harness, tier_cfg.model
    n = state.next_worker_n(conn, hive["id"])
    wb = worker_branch(hive["branch"], n)
    wt = Path(hive["worktree"]).parent / f"{hive['name']}-w{n}"
    live = state.workers(conn, hive["id"], live_only=True)
    wid = state.add_worker(conn, hive_id=hive["id"], n=n, branch=wb, worktree=str(wt), harness=hname,
                           model=model, title=title, task_ref=str(task) if task else None,
                           status="spawning", tier=tier_name, route_json=route_json)
    try:
        git.worktree_add(Path(hive["repo"]), wt, wb, hive["branch"])
        git.copy_worktreeinclude(Path(hive["repo"]), wt)
        git.add_info_exclude(wt, ".tmp/")
        target = live[-1]["pane_id"] if live else hive["brain_pane"]
        direction = "down" if live else "right"
        pane = H.split(target, direction, str(wt), env={"HIVE_NAME": hive["name"]})
        state.update(conn, "workers", wid, pane_id=pane)
        handle = bus.worker_handle(hive["name"], n)
        role = role_text("worker", n=n, hive=hive["name"], worktree=wt, worker_branch=wb,
                         branch=hive["branch"], handle=handle, brain=brain)
        launch_agent(pane, handle, cfg.harness(hname), model, worker=True, role=role,
                     workspace=hive["workspace_id"], worktree=str(wt),
                     hive_worktree=str(hive["worktree"]))
    except Exception as e:
        state.update(conn, "workers", wid, status="failed")
        state.event(conn, hive["id"], "spawn_failed", {"error": str(e)}, wid)
        raise
    if task:
        bus.send(handle, "task", hive["name"], f"New task: {title}. Read the task file in the ref, do it, commit, report.",
                 sender=brain, worker=n, task=str(wid), ref=str(task), kind="new", workspace=hive["workspace_id"])
        state.update(conn, "workers", wid, status="running")
    else:
        state.update(conn, "workers", wid, status="idle")
    state.event(conn, hive["id"], "spawn", {"n": n, "harness": hname, "model": model,
                                            "title": title, "tier": tier_name}, wid)
    return wid, n, str(wt), wb, pane


def _idle_worker_of_tier(conn, hive, tier):
    for w in state.workers(conn, hive["id"], live_only=True):
        if w["status"] == "idle" and w["tier"] == tier:
            return w
    return None


def cmd_spawn(args):
    git = G()
    conn = state.connect()
    hive = _current_hive(conn)
    cfg = config.load(Path(hive["repo"]))
    task = Path(args.task_file).resolve()
    if not task.exists():
        raise SystemExit(f"hive: task file not found: {task}")
    title = args.title or task.stem
    # The task file must sit inside the hive or worker worktrees (bus.send
    # refuses it). Check before any git or herdr change: a late failure left a
    # stray pane and a worker stuck in "spawning".
    if not bus.ref_ok(conn, hive["id"], str(task)):
        raise SystemExit(f"hive: task file must live in the hive or a worker worktree: {task}")
    # Tier by router + takeover (plan §Spawn flow). --tier skips the router.
    task_text = task.read_text(errors="replace")
    if args.tier and args.tier not in cfg.tiers:
        raise SystemExit(f"hive: unknown tier {args.tier!r} (known: {', '.join(cfg.tiers)})")
    file_count = repo_file_count(hive["repo"])
    route, router_request, human_reason = route_for_spawn(
        hive, cfg, task_text, title, flag_tier=args.tier, file_count=file_count)
    if route.tier is None:
        state.event(conn, hive["id"], "spawn_declined", {"title": title, "reason": "tier cancelled"})
        print("hive: spawn declined by the human", file=sys.stderr)
        return 4
    route_json = json.dumps({"probs": {t: round(float(p), 6) for t, p in route.probs.items()},
                             "confidence": round(route.confidence, 6),
                             "decided_by": route.decided_by})
    idle = _idle_worker_of_tier(conn, hive, route.tier)
    if idle is not None:
        bus.send(bus.worker_handle(hive["name"], idle["n"]), "task", hive["name"],
                 f"New task: {title}. Read the task file in the ref, do it, commit, report.",
                 sender=bus.brain_handle(hive["name"]), worker=idle["n"], task=str(idle["id"]),
                 ref=str(task), kind="new", workspace=hive["workspace_id"])
        state.update(conn, "workers", idle["id"], status="running", title=title, task_ref=str(task))
        state.event(conn, hive["id"], "spawn", {"n": idle["n"], "harness": idle["harness"],
                                                "model": idle["model"], "title": title,
                                                "tier": route.tier, "reused": True}, idle["id"])
        print(f"assigned task to idle w{idle['n']} tier={route.tier} ({idle['harness']}/{idle['model']})")
        return 0
    live = state.workers(conn, hive["id"], live_only=True)
    if len(live) >= cfg.max_workers:
        msg = f"cap reached: {len(live)}/{cfg.max_workers} workers live. Merge or discard one first."
        state.event(conn, hive["id"], "spawn_refused", {"reason": "cap"})
        print(f"hive: {msg}", file=sys.stderr)
        return 3
    from . import routelog
    wid, n, wt, wb, pane = _launch_worker(conn, git, cfg, hive, route.tier, task=task, title=title,
                                          route_json=route_json)
    w = state.worker(conn, hive["id"], n)
    routelog.log_case(hive, wid, title, task_text, router_request, route, cfg,
                      reason=_route_reason(route, human_reason, cfg),
                      state_features={"title": title, "file_count": file_count},
                      human_pick=route.tier if route.decided_by == "human" else None)
    state.event(conn, hive["id"], "route", {"tier": route.tier, "decided_by": route.decided_by,
                                            "confidence": round(route.confidence, 6),
                                            "probs": {t: round(float(p), 6) for t, p in route.probs.items()}}, wid)
    print(f"spawned w{n} tier={route.tier} ({w['harness']}/{w['model']}) pane {pane} worktree {wt} branch {wb}")
    return 0


def cmd_fix(args):
    conn = state.connect()
    hive = _current_hive(conn)
    w = _live_worker(conn, hive, args.worker)
    text = Path(args.task_file).read_text() if args.task_file else " ".join(args.text or [])
    if not text:
        raise SystemExit("hive: give --task-file or text")
    bus.send(bus.worker_handle(hive["name"], w["n"]), "task", hive["name"], text,
             sender=bus.brain_handle(hive["name"]),
           msgs_dir=msgs_dir(hive), worker=w["n"], task=str(w["id"]), kind="fix",
           ref=str(Path(args.task_file).resolve()) if args.task_file else None, workspace=hive["workspace_id"])
    state.update(conn, "workers", w["id"], status="fixing")
    state.event(conn, hive["id"], "fix", {"n": w["n"]}, w["id"])
    _route_outcome(w["id"], "fix", hive=hive)
    print(f"fix sent to w{w['n']}")
    return 0


def _route_outcome(worker_id: int, outcome: str, hive=None) -> None:
    """Append the retrain outcome line (only when routing.save_cases is on).
    Never blocks the command."""
    try:
        from . import routelog
        if hive is not None:
            cfg = config.load(Path(hive["repo"]))
            routelog.log_outcome(worker_id, outcome, cfg)
        else:
            routelog.log_outcome(worker_id, outcome)  # the caller decides with env/config
    except OSError as e:
        print(f"hive: route log error: {e}", file=sys.stderr)


def _live_worker(conn, hive, wn):
    w = state.worker(conn, hive["id"], parse_wn(wn))
    if not w or w["status"] not in state.LIVE:
        raise SystemExit(f"hive: no live worker {wn}")
    return w


# --- merge / discard -------------------------------------------------------------

def remove_worker(conn, hive, w, status, force_branch=False, merged_into=None):
    git = G()
    if w["pane_id"]:
        H.pane_close(w["pane_id"])
    bus.part(bus.worker_handle(hive["name"], w["n"]), workspace=hive["workspace_id"])
    repo = Path(hive["repo"])
    try:
        git.worktree_remove(repo, Path(w["worktree"]), force=force_branch)
    except Exception as e:
        print(f"hive: worktree remove: {e}", file=sys.stderr)
    # `git branch -d` checks against HEAD of the primary checkout, not the hive branch.
    # A branch that is merged into the hive branch is safe to delete with -D.
    merged = bool(merged_into) and git.is_ancestor(repo, w["branch"], merged_into)
    try:
        git.branch_delete(repo, w["branch"], force=force_branch or merged)
    except Exception as e:
        print(f"hive: branch delete: {e}", file=sys.stderr)
    state.update(conn, "workers", w["id"], status=status, merged_at=time.time() if status == "merged" else None)


def cmd_merge(args):
    git = G()
    conn = state.connect()
    hive = _current_hive(conn)
    w = _live_worker(conn, hive, args.worker)
    if git.has_uncommitted(Path(w["worktree"])):
        sys_send(hive, bus.worker_handle(hive["name"], w["n"]), "system",
                 "The brain tried to merge, but your worktree has uncommitted changes. Commit them and report again.",
                 worker=w["n"])
        raise SystemExit(f"hive: w{w['n']} has uncommitted changes; told the worker to commit")
    ok, out = git.merge_no_ff(Path(hive["worktree"]), w["branch"], f"hive: merge w{w['n']} — {w['title']}")
    if not ok:
        state.event(conn, hive["id"], "conflict", {"files": out}, w["id"])
        state.update(conn, "workers", w["id"], status="fixing")
        sys_send(hive, bus.worker_handle(hive["name"], w["n"]), "conflict",
                 f"Merge into {hive['branch']} conflicted (merge aborted). Run `git rebase {hive['branch']}`, "
                 f"resolve, run the tests, and report again.\nConflicted files:\n{out}",
                 worker=w["n"], task=str(w["id"]))
        print(f"conflict: w{w['n']} told to rebase on {hive['branch']}\n{out}")
        return 5
    remove_worker(conn, hive, w, "merged", force_branch=False, merged_into=hive["branch"])
    state.event(conn, hive["id"], "merge", {"n": w["n"]}, w["id"])
    _route_outcome(w["id"], "merge", hive=hive)
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
    _route_outcome(w["id"], "discard", hive=hive)
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
    bus.part(bus.brain_handle(hive["name"]), workspace=hive["workspace_id"])
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

def _hook_bus(event: str) -> None:
    """Flush on agent idle / focus change. Runs on every pane event, so it
    stays fast: no DB open without a reason, and it never raises."""
    try:
        from . import bus
        if not (state.STATE_DIR / "hive.db").exists():
            return
        ev = {}
        try:
            ev = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
        except json.JSONDecodeError:
            pass
        status = find_key(ev, "agent_status") if event == "agent-status" else None
        if event == "agent-status":  # only an idle agent can take a prompt
            if status not in ("idle", "done"):
                return
            pane = find_key(ev, "pane_id")
            if not pane:
                return
        else:
            pane = None
        conn = state.connect()
        try:
            if event == "agent-status":
                row = conn.execute(
                    "SELECT 1 FROM messages WHERE status='pending' AND target IN"
                    " (SELECT name FROM handles WHERE pane_id=?) LIMIT 1", (pane,)).fetchone()
            else:
                row = conn.execute("SELECT 1 FROM messages WHERE status='pending' LIMIT 1").fetchone()
            if not row:
                return
            bus.flush(conn)
        finally:
            conn.close()
    except Exception as e:
        print(f"hive: hook: {e}", file=sys.stderr)


def _auto_open_new_hive():
    """The workspace-created hook body: offer a hive for a plain git workspace."""
    if not config.load().auto_open:
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


def cmd_hook(args):
    if args.event == "workspace-created":
        return _auto_open_new_hive()
    if args.event in ("agent-status", "pane-focused"):
        _hook_bus(args.event)
        return 0
    return 0


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
    for row in conn.execute(
        "SELECT handles.name AS name, handles.pane_id AS pane_id FROM handles"
        " JOIN hives ON hives.id=handles.hive_id WHERE hives.status='open'"
    ).fetchall():
        if row["pane_id"] in panes:
            continue
        conn.execute("UPDATE messages SET status='failed' WHERE target=? AND status='pending'",
                     (row["name"],))
        bus.part(row["name"], conn=conn)
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
    s.add_argument("--task-file", required=True); s.add_argument("--title")
    s.add_argument("--tier", choices=["fast", "regular", "think"], default=None,
                   help="skip the router and force a tier")
    s.set_defaults(func=cmd_spawn)
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
    register_router(sub)


def register_router(sub, name="router") -> None:
    """`hive router` subcommands (routing step 1)."""
    from . import router
    s = sub.add_parser(name, help="local decision-model router (setup | serve | ask | ping)")
    rs = s.add_subparsers(dest="router_cmd", required=True)
    c = rs.add_parser("setup", help="install router deps and download the model files (one time)")
    c.add_argument("--dest", default=None, dest="dest")
    c.set_defaults(func=router.cmd_router_setup)
    c = rs.add_parser("serve", help="serve the router on a local Unix socket")
    c.set_defaults(func=router.cmd_router_serve)
    c = rs.add_parser("ask", help="ask the router to pick a tier for a task file")
    c.add_argument("--task-file", required=True)
    c.add_argument("--title", default=None)
    c.add_argument("--timeout", default="2")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=router.cmd_router_ask)
    c = rs.add_parser("ping", help="check that the router server answers")
    c.set_defaults(func=router.cmd_router_ping)
    c = rs.add_parser("export", help="write retraining rows (choice format) from the route log")
    c.add_argument("--since", default=None, help="YYYY-MM-DD, cases from this date on")
    c.add_argument("--out", default=None, help="output file (default $HIVE_STATE_DIR/route_log/export.jsonl)")
    c.set_defaults(func=router.cmd_router_export)


def argparse_remainder():
    import argparse
    return argparse.REMAINDER
