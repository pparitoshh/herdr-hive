# herdr-hive

A [herdr](https://herdr.dev) plugin that runs one **brain** and N **workers** in each workspace.

- One hive is one branch and one git worktree.
- Each worker has its own worktree, on a branch made from the hive branch.
- All agents talk over a local message bus that stores messages in SQLite and delivers them into pane prompts.
- A **review bench** tab shows each worker's diff and the whole-workspace diff to the human.

The design is in [SPEC.md](SPEC.md). The status of each part is in [PROGRESS.md](PROGRESS.md).

## Features

| Feature | What it does | Status |
|---|---|---|
| Hive | `hive new` opens a branch, a worktree, a brain pane and a review tab. | Tested live |
| Workers | `hive spawn` starts a worker in its own worktree and branch. A limit (`max_workers`) applies. | Tested live |
| Message bus | Local SQLite bus. It holds a message while the target is busy and delivers it when the target is idle. | Tested live |
| Merge, fix, discard | `hive merge wN`, `hive fix wN`, `hive discard wN`. | Merge tested live |
| Review bench | A Textual tab with the diff of each worker, comments and approve/reject. | Unit tests |
| Ask and finish | `hive ask` popup for the human. `hive finish` pushes, or pushes and opens a PR. | Unit tests |
| Worker tiers | fast, regular and think. Each tier has a harness and a model. | Tested live |
| Router | A local decision model (ONNX, no network) picks the tier. The human picks when confidence is low or the router is down. | Tested live |
| `--tier` flag and fixed mode | Skip the router for one spawn, or for all spawns (`routing.mode = "fixed"`). | Tested live |
| Retrain log | Records each route and its outcome. `hive router export` writes labeled cases. | Tested live |
| Worker permissions | Each worker launch allows the `hive` bus commands and the hive worktree. No user config is changed. | Unit tests, live test open |

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
4. Give the brain a goal. The brain writes task files and runs `hive spawn`. The router picks the tier for each
   worker; you confirm in a popup only when the tier is unclear.
5. Workers commit and send a `report`. The brain reviews and runs `hive merge wN` or `hive fix wN`.
6. The brain runs `hive finish`. You pick **Push**, **Push + PR (gh)** or **Nothing**.

## CLI

```
hive new [--cwd DIR]            open a hive
hive status [--json] [--all]    who is where
hive msg <handle> 'text' [--ref F] [--reply-required]
hive inbox                      messages for this pane's handle
hive show <id>                  print one message again
hive spawn --task-file F [--title T] [--tier fast|regular|think]
hive fix wN --task-file F | TEXT
hive router setup | serve | ask | ping | export
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

| `workers.pretrust` | `false` | Reserved for a later fix that pre-trusts a worker folder. Not used yet. |

Environment variables:

| Variable | Meaning |
|---|---|
| `HIVE_STATE_DIR` | The folder for `hive.db`, the router socket and the retrain log |
| `HIVE_ASK_MODE=plain` | Ask in the terminal with numbered choices, not in a Textual popup. Use it for scripts and tests. |
| `HIVE_SAVE_CASES=0` | Turn off the retrain log |

## Worker permissions

`hive spawn` gives each worker the permissions for one launch only. It does not write to `~/.claude.json` or to the OpenCode config.

| Harness | What hive passes | What it allows |
|---|---|---|
| Claude Code | `--settings <worktree>/.tmp/hive-perms.json` and `--add-dir <hive worktree>` | `hive msg`, `hive inbox`, `hive show`, and access to the hive worktree |
| OpenCode | The `OPENCODE_PERMISSION` environment variable | The same commands, and `external_directory` access to the hive worktree. Other bash commands still ask. |

Claude Code shows a folder trust dialog in each new worktree. Hive does not answer it. Hive sends a toast and tells the brain which pane waits on the dialog.

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

## Worker tiers and routing

The brain gives only the task. A local router picks the worker tier. The tier
sets the harness and the model for the worker. You never pick a harness or a
model for a worker; the brain never picks one either.

The tiers come from config (`[tiers.*]` in `harnesses.toml` or `.hive.toml`).
The defaults:

| Tier | Harness / model | Use |
|---|---|---|
| `fast` | opencode / `opencode-go/qwen3.8-flash` | Short and simple edits: one or two files, a clear change, no design |
| `regular` | claude / `claude-haiku-5-5` | Normal coding work: a clear feature or bug fix, several files, tests |
| `think` | opencode / `opencode-go/glm-5.3-flash` | Long or hard work: design, debugging, many files, unclear goal |

The tier `description` is the option text the router model reads. Tune it in
config, not in code.

How the router decides:

- The model runs only on this machine. There is no HTTP endpoint and no
  network call at run time.
- `hive router serve` loads the ONNX model once and answers on the Unix socket
  `$HIVE_STATE_DIR/router.sock` in mode 0600. No TCP port.
- The server uses 2 ONNX threads and exits after 30 idle minutes, so it uses
  no memory while hive is not in use.
- Measured on one machine (3-option question, after warm-up): model load about
  3 s, p50 about 140 ms, p95 about 150 ms, server RSS about 620 MB.

How a spawn picks the tier (`routing.mode` in config):

| Mode | Behaviour |
|---|---|
| `auto` (default) | The router picks the tier when its confidence reaches `routing.min_confidence`. <!-- min_confidence: set by step 2b --> Below that, or when the router is down or times out, the human picks in the popup: three tier choices, the router pick first, the reason and the probabilities in the prompt. There is no silent default tier. Cancel stops the spawn. |
| `ask` | The popup always asks, with the router pick first. |
| `fixed` | No router. The old flow: the human picks harness and model. |

`hive spawn --tier fast|regular|think` skips the router. The brain may add
`--tier` only with a strong reason (see `hive/roles/brain.md`).

Each spawn stores `tier` and `route_json` (probs, confidence, decided_by =
router | human | flag) on the worker row, writes a `route` event, and shows
the tier in `hive status`.

Router commands:

```bash
hive router setup            # install deps, download the model files (one time)
hive router serve            # server on the Unix socket (~/.local/state/herdr-hive/router.sock)
hive router ask --task-file F --title T   # print the tier probabilities for a task file
hive router export [--since D] [--out F]  # retraining rows (choice format) from the route log
hive router ping             # check the server
```

`herdr-plugin.toml` registers the action **Hive: router setup** for the
`workspace` context. It runs `bin/hive router setup`: it installs `onnxruntime`,
`tokenizers` and `numpy` into the plugin venv and downloads the model files one
time. There is no `pyproject.toml` here, so the optional deps list replaces the
extra `hive[router]`:

| Dependency | Installed by | Used for |
|---|---|---|
| `onnxruntime` | `hive router setup` (into the plugin venv) | The ONNX session |
| `tokenizers` | `hive router setup` | Token ids and spans |
| `numpy` | `hive router setup` | Masks and softmax |

Model files land in `~/.cache/herdr-hive/decision-model/`
(`model_fp16.onnx`, `tokenizer.json`, `decision_config.json`, `VERIFY.json`,
from `gparitosh007/Open-Decision-Model-150M-ONNX`). The download shows progress
and skips existing files. `$HIVE_DECISION_MODEL_DIR` moves the cache.
`$HIVE_ROUTER_MODEL_FILE` overrides the file pick.
The fp32 copy measured slower and heavier (p50 162 ms, RSS 942 MB); int8
quantization does not load for this graph, so `serve` loads `model_fp16.onnx`.

## Retrain log

The retrain log collects the routing cases you can retrain the decision model
on. It is ON by default (`routing.save_cases = true`): `hive` appends
`$HIVE_STATE_DIR/route_log/YYYY-MM-DD.jsonl` (one file per local day;
`$HIVE_STATE_DIR` is `~/.local/state/herdr-hive` by default).

Turn it off for one repo in `<repo>/.hive.toml`:

```toml
[routing]
save_cases = false
```

Or once for the machine with `HIVE_SAVE_CASES=0` in the environment. With the
log off, `hive` writes nothing and creates nothing; `hive router export`
still works on old files and prints 0 rows when there are none.

`hive` appends one case line per spawn when the router did not decide:

- the human picked the tier (low confidence, router failure, `routing.mode = ask`),
- or the confidence stayed below `routing.min_confidence` while the router
  still had probabilities.
<!-- min_confidence: set by step 2b -->

A case line holds:

```json
{"ts", "worker_id", "repo", "title", "task_text" (first 2000 chars),
 "state_features": {"title", "file_count", "task_chars"},
 "state", "instructions",
 "options": {tier: description}, "probs", "router_pick", "confidence",
 "reason", "human_pick", "final_tier", "outcome": null}
```

`state` and `instructions` are the exact texts of the request, so an export row
reproduces it. A high-confidence router decision is not logged: no human saw it,
and it has no label.

On `merge`, `discard` or `fix`, `hive` appends one outcome line instead of
rewriting the case line (append is safe across processes; the simpler schema of
the two in the plan wins):

```json
{"type": "outcome", "worker_id": 7, "ts": 1762000000.0, "outcome": "merge"}
```

`hive router export [--since YYYY-MM-DD] [--out F]` writes training rows, one
per case that has a label:

```json
{"state": "...", "instructions": "...", "options": {"fast": "..."}, "label": "think"}
```

Label rules: use `human_pick` when the human picked a tier; otherwise use
`final_tier` when the last outcome line for that worker is `merge`. Rows
without a label are skipped. `--since` filters the case dates; outcome lines
join in from all dates.

Daily retrain example (cron line):

```
# export the routing cases once a day at 05:15
15 5 * * * /path/to/herdr-hive/bin/hive router export --out $HOME/.cache/herdr-hive/retrain/latest.jsonl
```

Import `latest.jsonl` into your retrain pipeline and train the decision model
on it; then swap the model file in `~/.cache/herdr-hive/decision-model/`.

## License

MIT
