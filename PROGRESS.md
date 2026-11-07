# Progress

Status on 2026-11-07 (end of day).

## Milestones

| M | Deliverable | Status | Evidence |
|---|---|---|---|
| M0 | Skeleton, manifest, `setup.sh`, config loader | Done | `herdr plugin link .` registers the plugin. `hive status` runs. |
| M1 | `hive new`: branch picker, worktree, brain pane, bus, role | Done, tested live | Hive `exist1` opened. `brain.exist1` got the role prompt. The brain replied "ready". The bench tab opened. |
| M2 | `hive spawn`: picker, worker worktree, split, bus, task | Done, tested live | 6 workers were spawned in stacked splits in hive `test_mx`. With `max_workers = 2` and 2 workers live, the next spawn was refused with exit code 3 and no popup. The brain merged 3 workers on its own, so 3 workers were never live at the same time. |
| M3 | Report, `hive merge`, cleanup, conflict | Code done, not tested live | `merge_no_ff` with abort is covered by unit tests. |
| M4 | Bench: dropdown, diff, comment, approve/reject | Code done, unit tests | The Textual pilot tests check that a comment goes to the worker and to the brain, and that a verdict goes to the brain. |
| M5 | `hive ask`, `hive finish`, auto-open hook | Code done, not tested live | — |
| M6 | AI review key, README, topic, v0.1.0 tag | Done | The GitHub topic `herdr-plugin` is set. The tag `v0.1.0` is pushed. The repo is private, so the marketplace does not list it. |

Unit tests (`PYTHONPATH=. python3 -m unittest discover -s tests`): 45 tests, 43 pass. The 2 failures are the old import errors for the missing `textual` module on this machine.

## Message bus (plan: `.hive/tasks/PLAN.md`)

| Step | Deliverable | Status | Evidence |
|---|---|---|---|
| 1 | `bus.py`, `handles` and `messages` tables, file modes | Done | 11 unit tests in `tests/test_bus.py` |
| 2 | `flush()` with the herdr calls | Done | Unit tests with a fake herdr cover idle, working, focused, 5 errors and a gone pane |
| 3 | Swap the callers, roles, CLI | Done | The old module `radio_ops.py` is deleted. A grep over `hive/` and `tests/` gives no match |
| 4 | Live hive test of the bus | Code done, first live test passed | Bus live test on 2026-11-07: brain round trip with `hive msg`, envelope, inbox, show, 4 KB spill and refusals all behaved as designed. The full flow test of the plan (2 workers report and a merge) is open |
| 5 | Docs | Done | README and SPEC describe the bus; no "radio" except the credit line |

## Problems found in the live test, and fixed

1. A popup does not open with `--workspace`. Hive now opens popups without it.
2. The `split` fallback needs `--target-pane`. Hive now passes it.
3. Claude Code shows a folder trust dialog in each new worktree. Hive now sends a toast and waits up to 30 min.
4. The bench tab label was a number. Hive now renames it to `review`.
5. The model picker showed only the static `models` list. A harness can now set `models_command` (for example `opencode models`). The picker adds each output line after the static list.
6. `hive merge` did not delete the worker branch. `git branch -d` checks against `main` in the primary checkout, not against the hive branch. Hive now checks that the branch is in the hive branch, then deletes it with `-D`.

## Worker tiers and routing (plan: `.hive/tasks/ROUTING_PLAN.md`)

| Step | Deliverable | Status | Evidence |
|---|---|---|---|
| 1-3b, 5 | Tiers fast/regular/think, local decision model router, human picker, retrain log, docs | Done | Merged in `worker-routing`. Router p50 138 ms (idle box), RSS 653 MB. |
| J | `routing.mode = "fixed"` | Done | Merged (a00ebba). |
| K | Live end-to-end test (7 tests) | Done | bfc3ad8: plain picker mode, early task-file check, log confidence fix. 100 tests passed. |
| L | Worker permissions for each launch | Done, live test open | b0d5d82, merged 5c891c7. Claude: `--settings` (allow only `hive msg/inbox/show`) and `--add-dir`. OpenCode: `OPENCODE_PERMISSION`. Trust dialog notice to the brain. 112 tests pass. |
| M | Short live test of Task L | Stopped (laptop closed) | Partial. Task file: `.hive/tasks/worker-perms-live-test.md`. |

`main` has the merge of a00ebba (dd7d2aa). `main` does not have Task K and Task L yet.

## Next (2026-11-08)

1. Run Task M again with a Haiku brain. Check: trust notice, `hive msg` with no dialog, OpenCode with no `external_directory` dialog, the exact `hive msg` command form.
2. If Task M passes, merge `origin/worker-routing-v2` into `main`.
3. Fix: the trust dialog also shows on the hive brain. Task L covers only workers.
4. Decide on `workers.pretrust` (writes `~/.claude.json`, needs the user).
5. Router: all confidences were near 0.40, and the simple task got regular, not fast. Collect cases in the retrain log, then tune.

## Open work

1. Finish the live test for M3: the conflict and the rebase. The merge works live. Check the branch delete fix live.
2. One popup did not show in a live run, but herdr reported it as opened. A retry worked. Find the cause.
3. Live test of M5: `hive ask`, `hive finish` with push or PR, and the `workspace.created` hook.
4. Make the repo public, so that the herdr marketplace lists the plugin.
5. Spec Q2: check popups in the herdr desktop app.
