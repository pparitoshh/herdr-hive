# Progress

Status on 2026-11-06.

## Milestones

| M | Deliverable | Status | Evidence |
|---|---|---|---|
| M0 | Skeleton, manifest, `setup.sh`, config loader | Done | `herdr plugin link .` registers the plugin. `hive status` runs. |
| M1 | `hive new`: branch picker, worktree, brain pane, radio, role | Done, tested live | Hive `exist1` opened. `brain.exist1` is live on the radio. The brain replied "ready". The bench tab opened. |
| M2 | `hive spawn`: picker, worker worktree, split, radio, task | Code done, live test partly done | 2 workers reached `running` in stacked splits. The full test (3 workers, then a 4th refused) did not finish. |
| M3 | Report, `hive merge`, cleanup, conflict | Code done, not tested live | `merge_no_ff` with abort is covered by unit tests. |
| M4 | Bench: dropdown, diff, comment, approve/reject | Code done, unit tests | The Textual pilot tests check that a comment goes to the worker and to the brain, and that a verdict goes to the brain. |
| M5 | `hive ask`, `hive finish`, auto-open hook | Code done, not tested live | — |
| M6 | AI review key, README, topic, v0.1.0 tag | Done | The GitHub topic `herdr-plugin` is set. The tag `v0.1.0` is pushed. The repo is private, so the marketplace does not list it. |

Unit tests: 24 pass (`PYTHONPATH=. ~/.local/state/herdr-hive/venv/bin/python -m unittest discover -s tests`).

## Problems found in the live test, and fixed

1. A popup does not open with `--workspace`. Hive now opens popups without it.
2. The `split` fallback needs `--target-pane`. Hive now passes it.
3. Claude Code shows a folder trust dialog in each new worktree. Hive now sends a toast and waits up to 30 min.
4. The bench tab label was a number. Hive now renames it to `review`.

## Open work

1. Finish the live tests for M2 and M3: the worker cap, the conflict, the rebase and the merge.
2. Live test of M5: `hive ask`, `hive finish` with push or PR, and the `workspace.created` hook.
3. Make the repo public, so that the herdr marketplace lists the plugin.
4. Spec Q2: check popups in the herdr desktop app.
