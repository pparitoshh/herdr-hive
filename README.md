# herdr-hive

A [herdr](https://herdr.dev) plugin that runs one **brain** and N **workers** in each workspace.

- One hive is one branch and one git worktree.
- Each worker has its own worktree, on a branch made from the hive branch.
- All agents talk over a local message bus that stores messages in SQLite and delivers them into pane prompts.
- A **review bench** tab shows each worker's diff and the whole-workspace diff to the human.

The design is in [SPEC.md](SPEC.md).

## Requirements

| Tool | Version |
|---|---|
| herdr | 0.9.0 or later |
| python | 3.11 or later (`tomllib`) |
| git | 2.38 or later |
| gh | optional, for "Push + PR" |

Install the herdr integration for each harness that you use, for example `herdr integration install claude`.

## Install

```bash
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
hive msg <handle> 'text' [--ref F] [--reply-required]
hive inbox                      messages for this pane's handle
hive show <id>                  print one message again
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

## Message bus

Handles have the form `name.<hive>`, for example `brain.myhive` and `w1.myhive`. The hive name is the part after the first dot.

Messages live in the `messages` table in `hive.db`. `hive msg` sends one message and prints the message id. Every agent reads messages in this envelope:

```
[HIVE_MESSAGE id=42 from=w1.myhive reply=required]
HIVE/1 type=report hive=myhive worker=w1
<free text>
Ref: <path>
[END_HIVE_MESSAGE id=42]
```

The first body line is a HIVE/1 header. If the text already starts with `HIVE/1 `, the CLI does not add a second header. Text over 4 KB goes to `<hive worktree>/.hive/msgs/<id>.md` in mode 0600. The body then keeps the first 300 chars, and the envelope carries a `Ref:` line. A `--ref` file must sit in the hive worktree or in a worker worktree.

Delivery works this way:

| Event | Result |
|---|---|
| Target agent idle, pane not focused | The bus runs `herdr agent prompt <pane>` with the envelope. The row becomes `delivered`. |
| Target agent `working` or `blocked`, or its pane focused | The row stays `pending`. The bus counts no attempt. |
| Prompt error | The count of attempts goes up by one. After 5 errors the row is `failed`. |
| Pane gone | The row is `failed` at once. |

Pending rows go to the targets in the order of the ids, oldest first. Each flush sends at most one message per target, because herdr can still report idle right after a prompt. The plugin hooks `pane.agent_status_changed` and `pane.focused` run `hive hook agent-status | pane-focused`. They flush the held messages when an agent goes idle or the human moves the focus away. The bench also flushes every 5 s. Every `hive` command flushes after it runs. `hive reconcile` fails the rows of a gone pane and removes its handle.

## Config

The global file is `~/.config/herdr-hive/harnesses.toml`. `hive` writes a default file on first use. A repo can override keys in `<repo>/.hive.toml`. The schema is in SPEC.md §4.

Extra keys:

| Key | Default | Meaning |
|---|---|---|
| `defaults.auto_open` | `true` | Prompt to open a hive when a plain repo workspace is created |
| `defaults.ask_timeout` | `"30m"` | Timeout for popups |
| `defaults.allowed_harnesses` | all | Per-repo allow list |
| `harness.<x>.headless_args` | — | Arguments for the AI review run, for example `["-p"]` for claude |
| `harness.<x>.models_command` | — | A command that prints one model on each line, for example `["opencode", "models"]`. The model picker lists these after `models`. If the command fails, the picker shows only `models`. |

List the files to copy into each new worktree in `<repo>/.worktreeinclude` (gitignore syntax).

## Differences from the spec

| Spec | Implementation | Reason |
|---|---|---|
| Handle `brain@<hive>`, `w<N>@<hive>` | `brain.<hive>`, `w<N>.<hive>` | The dot keeps the busy pane labels short and unambiguous. |
| Worker branch `<hive-branch>/w<N>` | `hive/<hive-branch>/w<N>` | git cannot have both the ref `x` and the ref `x/w1`. |
| The spec `join` starts the harness | `hive` binds the handle to the pane, then starts the harness | The start must pass a model and approval flags for each harness. |
| State in `$(herdr plugin state-dir hive)` | `~/.local/state/herdr-hive/hive.db` (`HIVE_STATE_DIR`) | herdr has no `state-dir` command, and the brain's `hive` calls run outside the plugin environment. |
| Q1 (auto-open) | Prompt only. If you accept, the plain workspace closes. | As proposed in the spec. |
| Q2 (popup missing) | Fall back to a `split` plugin pane | As proposed in the spec. |

## Credit

The envelope format and the busy-hold idea come from AgentRadio (detailles/AgentRadio, MIT).

## Test

```bash
PYTHONPATH=. ~/.local/state/herdr-hive/venv/bin/python -m unittest discover -s tests -v
```

## Router (routing step 1)

`hive router` picks the worker tier with a local decision model. No HTTP
endpoint and no network at run time.

```bash
hive router setup            # install deps, download the model files (one time)
hive router serve            # server on the Unix socket (~/.local/state/herdr-hive/router.sock)
hive router ask --task-file F --title T   # print the tier probabilities for a task file
hive router ping             # check the server
```

No `pyproject.toml` exists here, so the optional deps list replaces the extra
`hive[router]`:

| Dependency | Installed by | Used for |
|---|---|---|
| `onnxruntime` | `hive router setup` (into the plug venv) | The ONNX session |
| `tokenizers` | `hive router setup` | Token ids and spans |
| `numpy` | `hive router setup` | Masks and softmax |

Model files land in `~/.cache/herdr-hive/decision-model/`
(`model_fp16.onnx`, `tokenizer.json`, `decision_config.json`, `VERIFY.json`,
from `gparitosh007/Open-Decision-Model-150M-ONNX`). The download shows progress
and skips existing files. `$HIVE_DECISION_MODEL_DIR` moves the cache.
`$HIVE_ROUTER_MODEL_FILE` overrides the file pick.

Measured on this machine (one server process, `intra_op_num_threads = 2`):
model load about 3 s, p50 138 ms, p95 152 ms for one 3-option question over
the Unix socket, server RSS about 620 MB. The fp32 copy measured slower and
heavier (p50 162 ms, RSS 942 MB); int8 quantization does not load for this
graph. When the deps or files are missing, the server does not start and the
call fails with a reason (step 2 adds the human takeover).

## License

MIT
