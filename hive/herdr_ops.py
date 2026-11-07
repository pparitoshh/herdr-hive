"""Wrap $HERDR_BIN_PATH: panes, tabs, worktrees, toasts, plugin popups."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

PLUGIN_ID = "pparitoshh.hive"


class HerdrError(RuntimeError):
    pass


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or "herdr"


def call(*args: str, check=True) -> dict:
    r = subprocess.run([herdr_bin(), *args], capture_output=True, text=True)
    out = r.stdout.strip() or r.stderr.strip()
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        data = {"raw": out}
    if check and (r.returncode != 0 or "error" in data):
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise HerdrError(f"herdr {' '.join(args[:2])}: {err.get('message') or out}")
    return data.get("result", data) if isinstance(data, dict) else data


def context() -> dict:
    try:
        return json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except json.JSONDecodeError:
        return {}


# --- topology ---------------------------------------------------------------

def pane_list(workspace: str | None = None) -> list[dict]:
    args = ["pane", "list"] + (["--workspace", workspace] if workspace else [])
    return call(*args).get("panes", [])


def pane_get(pane: str) -> dict | None:
    try:
        return call("pane", "get", pane).get("pane")
    except HerdrError:
        return None


def pane_state(pane: str) -> dict | None:
    """Agent status and focus of a pane; None if the pane is gone."""
    p = pane_get(pane)
    if p is None:
        return None
    return {"agent_status": p.get("agent_status"), "focused": bool(p.get("focused"))}


def workspace_get(ws: str) -> dict | None:
    try:
        return call("workspace", "get", ws).get("workspace")
    except HerdrError:
        return None


def workspace_cwd(ws: str) -> str | None:
    """The cwd of the focused (else first) pane in a workspace."""
    panes = pane_list(ws)
    panes.sort(key=lambda p: not p.get("focused"))
    for p in panes:
        if p.get("cwd"):
            return p["cwd"]
    return None


def worktree_create(repo: str, branch: str, base: str | None, path: str | None, label: str | None) -> dict:
    args = ["worktree", "create", "--cwd", repo, "--branch", branch, "--no-focus"]
    if base:
        args += ["--base", base]
    if path:
        args += ["--path", path]
    if label:
        args += ["--label", label]
    return call(*args)


def worktree_open(repo: str, path: str) -> dict:
    return call("worktree", "open", "--cwd", repo, "--path", path, "--no-focus")


def workspace_close(ws: str) -> None:
    call("workspace", "close", ws, check=False)


def tab_list(ws: str) -> list[dict]:
    return call("tab", "list", "--workspace", ws).get("tabs", [])


def tab_rename(tab: str, label: str) -> None:
    call("tab", "rename", tab, label)


def tab_create(ws: str, cwd: str, label: str) -> dict:
    return call("tab", "create", "--workspace", ws, "--cwd", cwd, "--label", label, "--no-focus")


def split(pane: str, direction: str, cwd: str, env: dict | None = None) -> str:
    args = ["pane", "split", pane, "--direction", direction, "--cwd", cwd, "--no-focus"]
    for k, v in (env or {}).items():
        args += ["--env", f"{k}={v}"]
    return call(*args)["pane"]["pane_id"]


def pane_close(pane: str) -> None:
    call("pane", "close", pane, check=False)


def pane_rename(pane: str, label: str) -> None:
    call("pane", "rename", pane, label, check=False)


def pane_run(pane: str, argv: list[str]) -> None:
    call("pane", "run", pane, shlex.join(argv))


def wait_shell(pane: str, timeout: float = 15) -> None:
    """Wait until a fresh pane shows a prompt (any output)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            txt = call("pane", "read", pane, "--source", "visible").get("text", "")
        except HerdrError:
            txt = ""
        if txt.strip():
            return
        time.sleep(0.3)


def pane_text(pane: str) -> str:
    """Visible pane text; empty when the pane is gone or unreadable."""
    try:
        return call("pane", "read", pane, "--source", "visible").get("text", "")
    except HerdrError:
        return ""


def wait_agent_ready(pane: str, timeout_ms: int = 90000, blocked_timeout_ms: int = 1800000,
                     on_dialog=None) -> bool:
    """Wait until herdr detects an agent in the pane and it settles idle.

    A startup dialog (folder trust, login, approval) makes the agent `blocked`.
    Only the human can answer it, so hive waits up to blocked_timeout_ms and
    notifies once: `on_dialog(p, text)` when given (text = visible pane text),
    else a generic toast.
    """
    end = time.time() + timeout_ms / 1000
    told = False
    while time.time() < end:
        p = pane_get(pane) or {}
        st = p.get("agent_status")
        if p.get("agent") and st in ("idle", "done"):
            return True
        if st == "blocked" and not told:
            told = True
            if on_dialog:
                on_dialog(p, pane_text(pane))
            else:
                toast("Hive: agent needs you", f"Answer the dialog in pane {p.get('label') or pane}")
            end = max(end, time.time() + blocked_timeout_ms / 1000)
        time.sleep(1)
    return False


def agent_prompt(pane: str, text: str) -> None:
    call("agent", "prompt", pane, text)


def toast(title: str, body: str = "", sound: str = "request") -> None:
    args = ["notification", "show", title, "--sound", sound]
    if body:
        args += ["--body", body]
    call(*args, check=False)


def _first_pane(workspace: str | None) -> str | None:
    try:
        panes = pane_list(workspace)
    except HerdrError:
        return None
    panes.sort(key=lambda p: not p.get("focused"))
    return panes[0]["pane_id"] if panes else None


# --- popups -----------------------------------------------------------------

def popup_ask(req: dict, workspace: str | None = None, timeout: float = 1800) -> dict:
    """Open the picker popup and block until it writes an answer (spec §10).

    Returns {"answer": str|None, "cancelled": bool, "timeout": bool}.
    Falls back to a split pane when the popup cannot open (spec Q2).
    """
    tmp = Path(tempfile.mkdtemp(prefix="hive-ask-"))
    req_path, out_path = tmp / "req.json", tmp / "out.json"
    req_path.write_text(json.dumps(req))
    env = ["--env", f"HIVE_PICKER_REQ={req_path}", "--env", f"HIVE_PICKER_OUT={out_path}"]
    base = ["plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "picker"]
    opened = False
    last = ""
    for attempt in range(20):
        try:
            call(*base, "--placement", "popup", *env)
            opened = True
            break
        except HerdrError as e:
            last = str(e)
            if "ui_busy" in str(e) or "busy" in str(e).lower():
                time.sleep(3)  # another popup is open; queue behind it
                continue
            break
    if not opened:
        target = os.environ.get("HERDR_PANE_ID") or _first_pane(workspace)
        if not target:
            raise HerdrError(f"popup did not open ({last}) and no pane to split")
        call(*base, "--placement", "split", "--target-pane", target, "--focus", *env)
    toast(req.get("title", "Hive"), req.get("prompt", ""))
    end = time.time() + timeout
    while time.time() < end:
        if out_path.exists():
            try:
                ans = json.loads(out_path.read_text())
                ans.setdefault("timeout", False)
                return ans
            except json.JSONDecodeError:
                pass
        time.sleep(0.5)
    return {"answer": None, "cancelled": True, "timeout": True}
