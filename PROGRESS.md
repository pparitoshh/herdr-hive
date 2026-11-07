# Progress

Status on 2026-11-07.

## Milestones

| M | Deliverable | Status | Evidence |
|---|---|---|---|
| M0 | Skeleton, manifest, `setup.sh`, config loader | Done | `herdr plugin link .` registers the plugin. `hive status` runs. |
| M1 | `hive new`: branch picker, worktree, brain pane, radio, role | Done, tested live | Hive `exist1` opened. `brain.exist1` is live on the radio. The brain replied "ready". The bench tab opened. |
| M2 | `hive spawn`: picker, worker worktree, split, radio, task | Done, tested live | 6 workers were spawned in stacked splits in hive `test_mx`. With `max_workers = 2` and 2 workers live, the next spawn was refused with exit code 3 and no popup. The brain merged 3 workers on its own, so 3 workers were never live at the same time. |
| M3 | Report, `hive merge`, cleanup, conflict | Code done, not tested live | `merge_no_ff` with abort is covered by unit tests. |
| M4 | Bench: dropdown, diff, comment, approve/reject | Code done, unit tests | The Textual pilot tests check that a comment goes to the worker and to the brain, and that a verdict goes to the brain. |
| M5 | `hive ask`, `hive finish`, auto-open hook | Code done, not tested live | — |
| M6 | AI review key, README, topic, v0.1.0 tag | Done | The GitHub topic `herdr-plugin` is set. The tag `v0.1.0` is pushed. The repo is private, so the marketplace does not list it. |

Unit tests: 26 pass (`PYTHONPATH=. ~/.local/state/herdr-hive/venv/bin/python -m unittest discover -s tests`).

## Problems found in the live test, and fixed

1. A popup does not open with `--workspace`. Hive now opens popups without it.
2. The `split` fallback needs `--target-pane`. Hive now passes it.
3. Claude Code shows a folder trust dialog in each new worktree. Hive now sends a toast and waits up to 30 min.
4. The bench tab label was a number. Hive now renames it to `review`.
5. The model picker showed only the static `models` list. A harness can now set `models_command` (for example `opencode models`). The picker adds each output line after the static list.
6. `hive merge` did not delete the worker branch. `git branch -d` checks against `main` in the primary checkout, not against the hive branch. Hive now checks that the branch is in the hive branch, then deletes it with `-D`.

## Open work

1. Finish the live test for M3: the conflict and the rebase. The merge works live. Check the branch delete fix live.
2. One popup did not show in a live run, but herdr reported it as opened. A retry worked. Find the cause.
3. Live test of M5: `hive ask`, `hive finish` with push or PR, and the `workspace.created` hook.
4. Make the repo public, so that the herdr marketplace lists the plugin.
5. Spec Q2: check popups in the herdr desktop app.
