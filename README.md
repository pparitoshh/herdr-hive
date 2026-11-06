# herdr-hive

A [herdr](https://herdr.dev) plugin that runs one **brain** and N **workers** in each workspace.

- One hive is one branch and one git worktree.
- Each worker has its own worktree, on a branch made from the hive branch.
- All agents talk over [AgentRadio](https://github.com/detailles/AgentRadio).
- A **review bench** tab shows each worker's diff and the whole-workspace diff to the human.

The design is in [SPEC.md](SPEC.md).

## Requirements

| Tool | Version |
|---|---|
| herdr | 0.9.0 or later |
| AgentRadio (`radio`) | 0.7 or later |
| python | 3.11 or later (`tomllib`) |
| git | 2.38 or later |
| gh | optional, for "Push + PR" |

Install the herdr integration for each harness that you use, for example `herdr integration install claude`.

## Install

```bash
herdr plugin install detailles/AgentRadio --yes
herdr plugin install pparitoshh/herdr-hive --yes
```

For local development, do these steps:

1. Clone the repo.
2. Run `sh scripts/setup.sh`. This makes a venv with Textual in `~/.local/state/herdr-hive/venv` and links `hive` into `~/.local/bin`.
3. Run `herdr plugin link "$PWD"`.

Bind a key to the "new hive" action in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+shift+h"
type = "plugin_action"
command = "pparitoshh.hive.new"
description = "new hive"
```

## Use

1. In a workspace of a git repo, run the action `pparitoshh.hive.new` (key or command palette).
2. Pick or type a branch. herdr opens a new worktree workspace for it.
3. Pick the brain harness and model. The brain starts in tab `hive`. The bench opens in tab `review`.
4. Give the brain a goal. The brain writes task files and runs `hive spawn`. For each worker, you pick the harness and model in a popup.
5. Workers commit and send a `report`. The brain reviews and runs `hive merge wN` or `hive fix wN`.
6. The brain runs `hive finish`. You pick **Push**, **Push + PR (gh)** or **Nothing**.

## CLI

```
hive new [--cwd DIR]            open a hive
hive status [--json] [--all]    who is where
hive spawn --task-file F [--title T]
hive fix wN --task-file F | TEXT
hive merge wN
hive discard wN [--yes]
hive finish [--choice ...]
hive close [--hive NAME] [--yes]
hive ask --prompt P --choices a,b,c [--text] [--timeout 30m]
hive bench [--open]
hive reconcile                  mark orphans failed after a herdr restart
hive config                     show the merged config
```

`hive ask` prints the answer. Exit code 2 means "no answer" (cancel or timeout).

## Config

The global file is `~/.config/herdr-hive/harnesses.toml`. `hive` writes a default file on first use. A repo can override keys in `<repo>/.hive.toml`. The schema is in SPEC.md §4.

Extra keys:

| Key | Default | Meaning |
|---|---|---|
| `defaults.auto_open` | `true` | Prompt to open a hive when a plain repo workspace is created |
| `defaults.ask_timeout` | `"30m"` | Timeout for popups |
| `defaults.allowed_harnesses` | all | Per-repo allow list |
| `harness.<x>.headless_args` | — | Arguments for the AI review run, for example `["-p"]` for claude |

List the files to copy into each new worktree in `<repo>/.worktreeinclude` (gitignore syntax).

## Differences from the spec

| Spec | Implementation | Reason |
|---|---|---|
| Handle `brain@<hive>`, `w<N>@<hive>` | `brain.<hive>`, `w<N>.<hive>` | AgentRadio handles allow only `[A-Za-z0-9_.-]`. `@` in a pane label means a radio frequency. |
| Worker branch `<hive-branch>/w<N>` | `hive/<hive-branch>/w<N>` | git cannot have both the ref `x` and the ref `x/w1`. |
| `radio join` launches the harness | `radio join --no-launch`, then `hive` starts the harness | `radio join` cannot pass a model or approval flags for every harness. |
| State in `$(herdr plugin state-dir hive)` | `~/.local/state/herdr-hive/hive.db` (`HIVE_STATE_DIR`) | herdr has no `state-dir` command, and the brain's `hive` calls run outside the plugin environment. |
| Q1 (auto-open) | Prompt only. If you accept, the plain workspace closes. | As proposed in the spec. |
| Q2 (popup missing) | Fall back to a `split` plugin pane | As proposed in the spec. |

## Test

```bash
PYTHONPATH=. ~/.local/state/herdr-hive/venv/bin/python -m unittest discover -s tests -v
```

## License

MIT
