"""Config: global harnesses.toml merged with an optional per-repo .hive.toml (spec §4)."""
from __future__ import annotations

import json
import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

GLOBAL_PATH = Path(os.environ.get("HIVE_CONFIG", "~/.config/herdr-hive/harnesses.toml")).expanduser()

DEFAULT_TOML = """\
[defaults]
max_workers = 3
base_branch = "main"
# worktree_root = "~/.herdr/worktrees"   # herdr [worktrees].directory is used if unset
auto_open = true
ask_timeout = "30m"

[workers]
# Hook for a future trust-dialog bypass write (task L). Not used yet.
pretrust = false

[harness.claude]
label   = "Claude Code"
command = ["claude"]
model_arg = ["--model", "{model}"]
auto_approve_args = ["--permission-mode", "acceptEdits"]
models  = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
herdr_kind = "claude"
headless_args = ["-p"]

[harness.opencode]
label   = "OpenCode"
command = ["opencode"]
model_arg = ["--model", "{model}"]
auto_approve_args = []
models  = ["anthropic/claude-sonnet-5-5"]
# The picker runs this command and lists each output line as a model. `models` stays first.
models_command = ["opencode", "models"]
herdr_kind = "opencode"
headless_args = ["run"]

[tiers.fast]
harness = "opencode"
model   = "opencode-go/qwen3.8-flash"
description = "Short and simple edits: one or two files, a clear change, no design"

[tiers.regular]
harness = "claude"
model   = "claude-haiku-5-5"
description = "Normal coding work: a clear feature or bug fix, several files, tests"

[tiers.think]
harness = "opencode"
model   = "opencode-go/glm-5.3-flash"
description = "Long or hard work: design, debugging, many files, unclear goal"

[routing]
mode = "auto"            # auto | ask | fixed
min_confidence = 0.4     # below this, the human picks the tier
idle_exit = "30m"        # the router server exits after this idle time
timeout_s = 2            # max wait for one router request
save_cases = true        # write the retrain log (route_log/); $HIVE_SAVE_CASES=0 turns it off

[review]
harnesses = ["claude", "opencode"]
"""


@dataclass
class Harness:
    name: str
    label: str
    command: list[str]
    model_arg: list[str] = field(default_factory=list)
    auto_approve_args: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    herdr_kind: str = ""
    headless_args: list[str] = field(default_factory=list)
    models_command: list[str] = field(default_factory=list)

    def model_choices(self, timeout: float = 15) -> list[str]:
        """The static `models`, then each line of `models_command` output, without duplicates.

        If the command fails, only the static list is returned."""
        out = list(self.models)
        if self.models_command:
            try:
                r = subprocess.run(self.models_command, capture_output=True, text=True, timeout=timeout)
                if r.returncode == 0:
                    out += [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
            except (OSError, subprocess.TimeoutExpired):
                pass
        return list(dict.fromkeys(out))

    def argv(self, model: str | None, worker: bool, extra: list[str] | None = None) -> list[str]:
        out = list(self.command)
        if model:
            out += [a.replace("{model}", model) for a in self.model_arg]
        if worker:
            out += list(self.auto_approve_args)
        return out + list(extra or [])


# The bus commands a worker may run. Anything else stays unapproved.
HIVE_BUS_CMDS = ("hive msg", "hive inbox", "hive show")


def claude_worker_settings() -> dict:
    """The `--settings` file content: allow only the hive bus commands."""
    return {"permissions": {"allow": [f"Bash({c}:*)" for c in HIVE_BUS_CMDS]}}


def claude_worker_perm_argv(hive_worktree: str | Path) -> list[str]:
    """Extra claude flags for a worker: the hive worktree.

    Permissions go through the worktree project settings instead of
    `--settings`: the harness command may already pass its own `--settings`
    (e.g. claude-router inline auth) and a second flag would override it."""
    return ["--add-dir", str(hive_worktree)]


def opencode_worker_perm_env(hive_worktree: str | Path) -> dict[str, str]:
    """Per-launch OpenCode permissions for a worker (OPENCODE_PERMISSION env var).

    external_directory allows only the hive worktree; bash allows only the hive
    bus commands and falls back to ask for everything else."""
    perm = {
        # both depths: opencode glob `*` may not cross `/`, `**` may not exist
        "external_directory": {f"{hive_worktree}/*": "allow", f"{hive_worktree}/**": "allow"},
        "bash": {**{f"{c}*": "allow" for c in HIVE_BUS_CMDS}, "*": "ask"},
    }
    return {"OPENCODE_PERMISSION": json.dumps(perm)}


@dataclass
class WorkerPolicy:
    pretrust: bool = False  # hook for a future trust-dialog bypass write; not used yet


@dataclass
class Tier:
    name: str
    harness: str
    model: str
    description: str


# Built-in tiers. An old global harnesses.toml has no [tiers], so `load` fills them in.
TIERS = {
    "fast": Tier("fast", "opencode", "opencode-go/qwen3.8-flash",
                 "Short and simple edits: one or two files, a clear change, no design"),
    "regular": Tier("regular", "claude", "claude-haiku-5-5",
                    "Normal coding work: a clear feature or bug fix, several files, tests"),
    "think": Tier("think", "opencode", "opencode-go/glm-5.3-flash",
                  "Long or hard work: design, debugging, many files, unclear goal"),
}


@dataclass
class Routing:
    mode: str = "auto"
    min_confidence: float = 0.4
    idle_exit: int = 1800
    timeout_s: int = 2
    save_cases: bool = True  # retrain log on by default ($HIVE_SAVE_CASES=0 turns it off)


@dataclass
class Config:
    max_workers: int = 3
    base_branch: str = "main"
    worktree_root: Path | None = None
    auto_open: bool = True
    ask_timeout: str = "30m"
    workers: WorkerPolicy = field(default_factory=WorkerPolicy)
    harnesses: dict[str, Harness] = field(default_factory=dict)
    review_harnesses: list[str] = field(default_factory=list)
    tiers: dict[str, Tier] = field(default_factory=dict)
    routing: Routing = field(default_factory=Routing)

    def harness(self, name: str) -> Harness:
        try:
            return self.harnesses[name]
        except KeyError:
            raise SystemExit(f"hive: unknown harness {name!r} (known: {', '.join(self.harnesses)})")


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_global(path: Path = GLOBAL_PATH) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_TOML)
    return path


def parse_duration(s: str) -> int:
    """'30m' -> 1800 seconds. Units: s, m, h. A plain number is seconds."""
    s = str(s).strip()
    mult = {"s": 1, "m": 60, "h": 3600}
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(float(s))


def load(repo: Path | None = None, path: Path | None = None) -> Config:
    data = tomllib.loads(ensure_global(path or GLOBAL_PATH).read_text())
    if repo is not None:
        local = Path(repo) / ".hive.toml"
        if local.exists():
            data = _deep_merge(data, tomllib.loads(local.read_text()))
    d = data.get("defaults", {})
    cfg = Config(
        max_workers=int(d.get("max_workers", 3)),
        base_branch=str(d.get("base_branch", "main")),
        worktree_root=Path(d["worktree_root"]).expanduser() if d.get("worktree_root") else None,
        auto_open=bool(d.get("auto_open", True)),
        ask_timeout=str(d.get("ask_timeout", "30m")),
        workers=WorkerPolicy(pretrust=bool(data.get("workers", {}).get("pretrust", False))),
    )
    allowed = d.get("allowed_harnesses")
    for name, h in data.get("harness", {}).items():
        if allowed and name not in allowed:
            continue
        cfg.harnesses[name] = Harness(
            name=name,
            label=h.get("label", name),
            command=list(h.get("command", [name])),
            model_arg=list(h.get("model_arg", [])),
            auto_approve_args=list(h.get("auto_approve_args", [])),
            models=list(h.get("models", [])),
            herdr_kind=h.get("herdr_kind", name),
            headless_args=list(h.get("headless_args", [])),
            models_command=list(h.get("models_command", [])),
        )
    cfg.review_harnesses = [n for n in data.get("review", {}).get("harnesses", []) if n in cfg.harnesses]
    for name, t in data.get("tiers", {}).items():
        d = TIERS.get(name)
        cfg.tiers[name] = Tier(name=name,
                               harness=str(t.get("harness", d.harness if d else "opencode")),
                               model=str(t.get("model", d.model if d else "")),
                               description=str(t.get("description", d.description if d else "")))
    for name, t in TIERS.items():  # old toml without [tiers] still gets the defaults
        if name not in cfg.tiers:
            cfg.tiers[name] = t
    r = data.get("routing", {})
    idle = r.get("idle_exit", 1800)
    routing = Routing(mode=str(r.get("mode", "auto")),
                      min_confidence=float(r.get("min_confidence", 0.4)),
                      idle_exit=parse_duration(idle) if isinstance(idle, str) else int(idle),
                      timeout_s=int(r.get("timeout_s", 2)),
                      save_cases=bool(r.get("save_cases", True)))
    # $HIVE_SAVE_CASES=0 turns the retrain log off even when the config is on.
    env = os.environ.get("HIVE_SAVE_CASES")
    if env is not None:
        routing.save_cases = env.strip().lower() in ("1", "true", "yes", "on")
    cfg.routing = routing
    return cfg
