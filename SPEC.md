# herdr-hive — Spec v0.1

Repo: `pparitoshh/herdr-hive` · Status: draft · Date: 2026-11-05

A herdr plugin that runs one **brain** and N **workers** per workspace. Every agent works in its own git worktree, and all agents talk over AgentRadio. A **review bench** shows the diffs to the human.

---

## 1. Goals / Non-goals

**Goals**
- One workspace = one branch = one worktree. No two agents share a working directory.
- The brain plans, spawns workers on demand, reviews their work and merges it. It never edits code itself.
- Each worker gets its own worktree, on a branch made from the brain's branch.
- Harness (claude, opencode, …) and model are chosen per pane from a global config.
- Brain↔worker and worker↔worker messages go over AgentRadio.
- The human reviews per-worker and whole-workspace diffs in a bench TUI, comments on lines and approves or rejects.

**Non-goals (v0)**
- Multi-machine or remote federation (AgentRadio is local only).
- An always-on AI reviewer. AI review is opt-in, on a key press in the bench.
- Replacing `claude-skills/brain-worker`. That skill stays as it is.
- A web UI.

---

## 2. Dependencies

| Dependency | Version | Use |
|---|---|---|
| herdr (TUI or desktop app) | ≥ 0.9.0 | worktrees, panes, plugins, agent state |
| AgentRadio (`detailles/AgentRadio`) | latest | message bus (`radio` CLI) |
| python | ≥ 3.10 | `hive` CLI, hooks, bench |
| Textual | latest | bench + pickers (plugin-local venv) |
| git | ≥ 2.38 | worktrees, merges |
| gh | optional | PR at finish |

Herdr integrations installed for every harness used (`herdr integration install claude|opencode|…`).

---

## 3. Concepts

| Term | Meaning |
|---|---|
| **Hive** | One herdr workspace managed by this plugin. 1 hive = 1 branch = 1 worktree. |
| **Brain** | Supervisor agent. Runs in the hive worktree. Radio handle `brain@<hive>`. |
| **Worker** | Executor agent. Has its own worktree and branch `<hive-branch>/w<N>`. Handle `w<N>@<hive>`. |
| **Bench** | Textual TUI in the hive's `review` tab. Human only. |
| **Harness** | An agent CLI defined in `harnesses.toml` (claude, opencode, codex, …). |

Handles carry the hive suffix so that several hives can share one radio bus without name clashes.

---

## 4. Config

### 4.1 Global: `~/.config/herdr-hive/harnesses.toml`

```toml
[defaults]
max_workers = 3
base_branch = "main"
worktree_root = "~/.herdr/worktrees"   # herdr [worktrees].directory is used if unset

[harness.claude]
label   = "Claude Code"
command = ["claude"]
model_arg = ["--model", "{model}"]
auto_approve_args = ["--permission-mode", "acceptEdits"]   # workers only; tune per taste
models  = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
herdr_kind = "claude"

[harness.opencode]
label   = "OpenCode"
command = ["opencode"]
model_arg = ["--model", "{model}"]
auto_approve_args = []            # radio join writes opencode permission config
models  = ["anthropic/claude-sonnet-5-5", "openai/gpt-x", "ollama/qwen3-coder"]
models_command = ["opencode", "models"]  # optional: one model per output line, added after `models`
herdr_kind = "opencode"

[review]                          # optional AI review key in bench
harnesses = ["claude", "opencode"]
```

### 4.2 Per-repo override: `<repo>/.hive.toml` (optional)
Same schema. Keys are merged over the global file (`max_workers`, `base_branch`, allowed harnesses/models).

### 4.3 `<repo>/.worktreeinclude`
Gitignore-syntax list of ignored files to copy into every new hive or worker worktree (`.env`, `settings.local.json`, …). Copies come from the primary checkout.

---

## 5. Layout

```
Workspace "<branch>"  (worktree of <branch>)
├── Tab "hive"
│   ┌──────────────┬──────────────┐
│   │              │  w1          │
│   │   brain      ├──────────────┤
│   │              │  w2          │
│   │              ├──────────────┤
│   │              │  w3          │
│   └──────────────┴──────────────┘
└── Tab "review"
    └── bench (Textual)
```

- First worker: split the brain pane `--direction right`.
- Next workers: split the last worker pane `--direction down`.
- When a worker is removed, its pane closes and herdr reflows the layout.

---

## 6. Flows

### 6.1 Open hive
Triggers:
- **Primary:** the action `hive.new` (key, e.g. `prefix+shift+h`, or the command palette) run from any git workspace.
- **Auto:** an event hook on `workspace.created`. If the new workspace is a git repo, is not already a hive and is not a worker worktree, it opens the same popup. It can be turned off with `auto_open = false`.

Steps:
1. Popup picker: branch name. An existing branch is checked out; a new one is created from `base_branch`.
2. `herdr worktree create --cwd <repo> --branch <b> --no-focus` → new workspace grouped under the repo.
   - If the trigger was the auto hook on a plain workspace, the plain workspace is closed after the hive opens. **Open question Q1.**
3. Copy the `.worktreeinclude` files.
4. Popup picker: brain harness → brain model.
5. Rename tab 1 to `hive`. `radio join brain@<hive>` launches the brain harness and model in the root pane.
6. Inject the brain role prompt (§8.1).
7. Create the `review` tab and open the bench.
8. Write the hive record to the state DB (§9).

### 6.2 Spawn worker (brain-initiated)
1. Brain runs `hive spawn --task-file <path> [--title "..."]`.
2. `hive` checks `max_workers` and refuses with a clear message if the hive is already at the cap.
3. Popup picker for the human: worker harness → model. The human can cancel, and the brain is told it was declined.
4. `git worktree add <root>/<repo>/<hive>-w<N> -b <hive-branch>/w<N> <hive-branch>`.
   - Uses plain git, **not** `herdr worktree create`, so herdr doesn't open a separate workspace for the worker.
5. Copy the `.worktreeinclude` files.
6. Split a pane in the `hive` tab (§5) with `--cwd` set to the worker worktree.
7. `radio join w<N>@<hive>` launches the harness with the chosen model and `auto_approve_args`.
8. Inject the worker role prompt (§8.2), then send a `task` message with a `--ref` to the task file.
9. State: worker `running`.

### 6.3 Worker report → review → merge
1. The worker commits on its branch, then sends `report` to the brain (summary, commits, tests run, open issues).
2. The bench lists the worker's diff. The human can comment or approve/reject at any point (§7).
3. The brain checks the report: reads the diff, and may ask the worker to run tests.
4. The brain decides (bench verdicts and AI review are advisory):
   - **Fix:** sends `task` (kind=fix) to the same worker.
   - **Merge:** `hive merge w<N>`
     - `git merge --no-ff <hive-branch>/w<N>` in the hive worktree.
     - **Conflict:** `git merge --abort`, then a `conflict` message to the worker. The worker rebases on the hive branch, fixes the conflicts and reports again.
     - **Success:** close the pane, `radio part`, `git worktree remove`, `git branch -d`. State: `merged`.

### 6.4 Finish
1. Brain runs `hive finish`.
2. Popup for the human: **Push** / **Push + PR (gh)** / **Nothing**.
3. Any workers still live are listed. Finishing is refused until each one is merged or discarded (`hive discard w<N>`, which needs a popup confirmation).

### 6.5 Close hive
Action `hive.close`. Refused if workers are live, unless the human confirms in a popup. It removes the worker worktrees and branches. The hive worktree is kept; the user can delete it through herdr's own "Delete worktree checkout".

---

## 7. Review bench (Textual)

**Dropdown**
- `w<N> — <title> [running|reported|fixing]`: commits on `<hive-branch>/w<N>` vs `<hive-branch>`, plus uncommitted changes in that worktree.
- `Workspace total`: `<base_branch>...<hive-branch>`, merged work only.

**Main view:** a file list, plus a side-by-side or unified diff (toggle). The view refreshes on a file watcher and every 5 s.

**Actions**

| Key | Action | Effect |
|---|---|---|
| `c` | Comment on selected line | radio `pm w<N>@<hive>` kind=`review_comment` with `file:line` and the text; brain gets a copy in the log |
| `a` | Approve worker | radio `pm brain@<hive>` kind=`verdict` value=approve |
| `r` | Reject worker (+ reason) | radio `pm brain@<hive>` kind=`verdict` value=reject |
| `i` | AI review (optional) | picker: harness/model from `[review]` → runs headless on the selected diff → shows findings in a side panel; `f` forwards them to the brain |
| `o` | Open file in `$EDITOR` | — |

On the "Workspace total" view, comments go to the brain (it has no worker owner).

---

## 8. Roles (injected prompts)

### 8.1 Brain
- You are the BRAIN of hive `<hive>` on branch `<b>`. **Don't edit files or run builds.**
- Tools: `hive spawn|status|merge|finish|discard` and `radio pm|inbox|show|handles`.
- Loop: split the goal into small tasks that can be tested → spawn workers (at most `max_workers` at once) → wait for pushed messages → review → fix or merge → repeat.
- Human verdicts and AI reviews are advisory, and the final decision is yours. Mention any rejected verdict in your report.
- Ask the human (via `hive ask`, §10) before anything destructive or ambiguous.

### 8.2 Worker
- You are WORKER `w<N>` in hive `<hive>`. Your worktree: `<path>`, branch `<hive-branch>/w<N>`.
- Only do the task you were given. Commit with clear messages. Never merge into the hive branch, and never push.
- When you're done, send a `report` to the brain. If blocked, send a `question` to the brain or to the peer `w<M>`.
- Apply `review_comment` messages, then report again.
- On `conflict`: `git rebase <hive-branch>`, resolve the conflicts, run the tests, report.

---

## 9. State

SQLite at `$(herdr plugin state-dir hive)/hive.db`. A new tool has to be able to answer "who is where".

```
hives(id, repo, branch, base, worktree, workspace_id, brain_pane, brain_harness, brain_model, status, created_at)
workers(id, hive_id, n, branch, worktree, pane_id, harness, model, title, task_ref, status, created_at, merged_at)
   status: spawning | running | reported | fixing | merged | discarded | failed
events(id, hive_id, worker_id, kind, payload_json, ts)   -- audit log (spawn, merge, conflict, verdict, ask)
```

Messages themselves stay in AgentRadio's ledger and are not duplicated here.

---

## 10. Asking the human

| Need | Mechanism |
|---|---|
| Structured choice (branch, harness/model, push/PR, discard, approval) | `hive ask --choices ...`: a plugin **popup pane** (Textual picker) + a herdr toast. The brain's call blocks until you answer. Default timeout 30 min, then the brain is told "no answer". |
| Free-text question | The brain asks in its own pane, and herdr marks it blocked in the sidebar. |
| Attention from another workspace | A toast plus the sidebar's blocked/attention state. |

Phone push: out of scope for v0. It can be added through an existing herdr mobile plugin.

---

## 11. Radio message protocol

AgentRadio handles transport and framing (`[RADIO_MESSAGE ...]`). Hive adds a one-line typed header as the first line of the body:

```
HIVE/1 type=<type> hive=<hive> worker=w<N> task=<id> [ref=<path>]
<free text>
```

| type | from → to | meaning |
|---|---|---|
| `task` | brain → w | new task or fix (`kind=new|fix`) |
| `report` | w → brain | done: summary, commits, tests, open issues |
| `question` | w → brain / w → w | blocked, needs an answer (`--reply-required`) |
| `answer` | any → any | reply to a question |
| `review_comment` | bench → w (cc brain) | `file:line` + text |
| `verdict` | bench → brain | approve / reject + reason |
| `conflict` | hive → w | merge conflict, rebase needed |
| `ai_review` | bench → brain | forwarded AI findings (`ref=` file) |
| `system` | hive → any | spawn declined, cap reached, timeout |

Anything longer than ~1200 chars is written to `<hive worktree>/.hive/msgs/<id>.md` and sent with `--ref`. `.hive/` is added to `.git/info/exclude`.

---

## 12. Plugin layout

```
herdr-hive/
├── herdr-plugin.toml
├── bin/hive                 # python entry (CLI used by brain, workers, hooks)
├── hive/
│   ├── config.py            # harnesses.toml + .hive.toml merge
│   ├── state.py             # sqlite
│   ├── git_ops.py           # worktree add/remove, merge, diff
│   ├── herdr_ops.py         # wraps $HERDR_BIN_PATH (pane split, tab, toast, plugin pane open)
│   ├── radio_ops.py         # wraps `radio`
│   ├── roles/brain.md
│   ├── roles/worker.md
│   ├── picker.py            # Textual popup picker
│   └── bench/app.py         # Textual bench
├── scripts/setup.sh         # venv + textual
└── README.md
```

Manifest sketch:

```toml
id = "pparitoshh.hive"
name = "Herdr Hive"
version = "0.1.0"
min_herdr_version = "0.9.0"
platforms = ["linux", "macos"]

[[build]]
command = ["sh", "scripts/setup.sh"]

[[actions]]
id = "new"
title = "Hive: new workspace"
contexts = ["workspace"]
command = ["bin/hive", "new"]

[[actions]]
id = "close"
title = "Hive: close"
contexts = ["workspace"]
command = ["bin/hive", "close"]

[[actions]]
id = "bench"
title = "Hive: open review bench"
contexts = ["workspace"]
command = ["bin/hive", "bench", "--open"]

[[events]]
on = "workspace.created"
command = ["bin/hive", "hook", "workspace-created"]

[[panes]]
id = "picker"
title = "Hive picker"
placement = "popup"
command = ["bin/hive", "picker"]

[[panes]]
id = "bench"
title = "Review bench"
placement = "tab"
command = ["bin/hive", "bench"]
```

`hive` CLI (put on PATH via a symlink, the same way as `radio`):

```
hive new | close | status [--json]
hive spawn --task-file F [--title T]
hive merge wN | discard wN | finish
hive ask --prompt P --choices a,b,c [--timeout 30m]
hive picker            # popup entrypoint (internal)
hive bench [--open]
hive hook <event>      # internal
```

---

## 13. Milestones

| M | Deliverable | Done when |
|---|---|---|
| M0 | Repo skeleton, manifest, `setup.sh`, config loader | `herdr plugin link .` registers; `hive status` runs |
| M1 | `hive new`: branch picker → worktree → brain pane + radio join + role | Open a hive by key in the desktop app; brain is live on the radio |
| M2 | `hive spawn`: harness/model picker → worker worktree + split + radio + task | Brain spawns 3 workers in stacked splits; 4th refused |
| M3 | Report → `hive merge` → cleanup; conflict → send back | End-to-end: 2 workers, 1 conflict, both merged, panes/worktrees/branches gone |
| M4 | Bench: dropdown, diff, comment, approve/reject | Comment reaches the worker; verdict reaches the brain |
| M5 | `hive ask` popups, `hive finish` push/PR, auto-open hook | Full flow with no typing outside popups except goals |
| M6 | AI review key, README, `herdr-plugin` topic, v0.1.0 tag | Installable via `herdr plugin install pparitoshh/herdr-hive` |

---

## 14. Risks / open questions

- **Q1:** Auto-open on `workspace.created`: herdr fires different events for the UI and CLI paths (the UI may only emit `workspace.focused`). Should a plain workspace opened on a repo be **converted** (closed, with a hive worktree opened instead) or **only prompted**? Proposed: prompt only, and close the plain workspace if you accept.
- **Q2:** Desktop app parity: popup panes and toasts need to be verified in the desktop app, not just the TUI. If a popup is missing, fall back to a `split` placement.
- **Q3:** Worker auto-approve: each harness has a different flag. A wide flag like `--dangerously-skip-permissions` is risky even inside a worktree, because the worker can still touch `~` and the network. The default is the narrowest flag that lets a worker run unattended; you choose per harness in `harnesses.toml`.
- **Q4:** AgentRadio has no broadcast. "cc brain" on review comments means two DMs. This is acceptable.
- **Q5:** OpenCode model IDs depend on the provider. `models` lists the IDs exactly as the CLI expects them.
- **Q6:** Stale state after a herdr restart: on startup, `hive` reconciles the DB against `herdr pane list`, `git worktree list` and `radio handles`, and marks any orphans `failed`.
