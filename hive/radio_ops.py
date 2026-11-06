"""Wrap the AgentRadio `radio` CLI and the HIVE/1 header (spec §11).

Radio handles allow only [A-Za-z0-9_.-], so the spec's `brain@<hive>` becomes
`brain.<hive>` and `w<N>@<hive>` becomes `w<N>.<hive>`. The `@` in a radio pane
label means a named frequency, so the dot form also keeps labels unambiguous.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

LONG = 1200


def radio_bin() -> str:
    return os.environ.get("HIVE_RADIO_BIN") or shutil.which("radio") or "radio"


def brain_handle(hive: str) -> str:
    return f"brain.{hive}"


def worker_handle(hive: str, n: int) -> str:
    return f"w{n}.{hive}"


def bench_handle(hive: str) -> str:
    return f"bench.{hive}"


def system_handle(hive: str) -> str:
    return f"hive.{hive}"


def header(type_: str, hive: str, worker: int | None = None, task: str | None = None,
           ref: str | None = None, **extra) -> str:
    parts = [f"HIVE/1 type={type_}", f"hive={hive}"]
    if worker is not None:
        parts.append(f"worker=w{worker}")
    if task is not None:
        parts.append(f"task={task}")
    for k, v in extra.items():
        if v is not None:
            parts.append(f"{k}={v}")
    if ref:
        parts.append(f"ref={ref}")
    return " ".join(parts)


def parse_header(body: str) -> dict:
    line = body.splitlines()[0] if body else ""
    if not line.startswith("HIVE/1 "):
        return {}
    out = {}
    for tok in line.split()[1:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def _run(args: list[str], workspace: str | None = None, check=True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if workspace:
        env["HERDR_WORKSPACE_ID"] = workspace
    r = subprocess.run([radio_bin(), *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"radio {' '.join(args[:2])} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r


def send(to: str, type_: str, hive: str, text: str, *, sender: str, msgs_dir: Path | None = None,
         worker: int | None = None, task: str | None = None, ref: str | None = None,
         reply_required: bool = False, workspace: str | None = None, **extra) -> str:
    """Send one HIVE/1 message. A long body goes to <msgs_dir>/<id>.md and rides as --ref."""
    if len(text) > LONG and msgs_dir is not None and not ref:
        msgs_dir.mkdir(parents=True, exist_ok=True)
        mid = f"{int(time.time() * 1000)}-{type_}"
        path = msgs_dir / f"{mid}.md"
        path.write_text(text)
        ref = str(path)
        text = text[:300].rstrip() + f"\n… (full text in ref)"
    body = header(type_, hive, worker, task, ref, **extra) + "\n" + text
    args = ["pm", to, body, "--from", sender]
    if ref:
        args += ["--ref", ref]
    if reply_required:
        args.append("--reply-required")
    r = _run(args, workspace)
    return r.stdout.strip()


def join(handle: str, pane: str, role: str | None = None, workspace: str | None = None) -> None:
    """Register a pane under a handle without launching an agent (hive launches it)."""
    args = ["join", handle, "--pane", pane, "--no-launch"]
    if role:
        args += ["--role", role]
    _run(args, workspace)


def part(handle: str, workspace: str | None = None) -> None:
    _run(["part", handle], workspace, check=False)


def handles(workspace: str | None = None) -> str:
    return _run(["handles"], workspace, check=False).stdout
