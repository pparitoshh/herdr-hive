# Task E: routing step 2, human takeover path + routing eval set (branch `routing-takeover`)

Worktree: this directory. Plan: `.hive/tasks/ROUTING_PLAN.md` (read "Router" item 9 and "Spawn flow").
Commit on `routing-takeover`. Do not push or merge. No network calls. Use TMPDIR=./.tmp for temp files.
Worker 2 writes `hive/router.py` on another branch. Do NOT create or edit `hive/router.py` or `cmd_spawn`.

## Part 1: `hive/takeover.py` (new file only)

Interface (keep it exactly):

    @dataclass
    class Route: tier: str | None; probs: dict[str, float]; confidence: float; decided_by: str  # "router"|"human"|"flag"
    class RouterUnavailable(Exception): reason: str
    def decide_tier(task_text, title, tiers: list[str], mode: str, min_confidence: float,
                    ask_router: Callable[[str, str], dict[str, float]],
                    ask_human: Callable[[str, dict[str, float], str | None], str | None],
                    flag_tier: str | None = None) -> Route

Rules:
1. `flag_tier` set: return it, decided_by="flag". Do not call the router.
2. `ask_router` raises RouterUnavailable (missing deps, missing model, server down, 2 s timeout):
   call `ask_human(reason, {}, None)`.
3. mode "auto": confidence = max prob. If >= min_confidence, use it. Else `ask_human("low confidence ...", probs, top)`.
4. mode "ask": always `ask_human("ask mode", probs, top)`.
5. `ask_human` returns None (human cancels): Route with tier=None. The caller stops the spawn. No silent default tier.
6. Add `human_prompt(reason, probs, default) -> str`: the popup text (reason, probs sorted, default first).

Tests `tests/test_takeover.py` with fake callables: flag, router ok, each failure reason, low confidence,
ask mode, human cancel. Run the full test suite.

## Part 2: routing eval set `tests/data/route_cases.json`

Write 24 realistic hive task texts (8 per tier) with the expected tier, as
`[{"title":..., "task":..., "tier":"fast|regular|think", "why":...}]`. Tiers:
- fast: small, mechanical, 1-2 files (rename, typo, add a flag, a doc line).
- regular: normal feature or bug fix, several files, tests.
- think: hard design, unclear bug, concurrency, perf, multi-module refactor.
Include 4 borderline cases (mark `"borderline": true`). This set is used later to tune the routing prompt.

## Report

herdr agent prompt w1:p2 "WORKER REPORT E: <commit, tests pass/fail, open issues>"
