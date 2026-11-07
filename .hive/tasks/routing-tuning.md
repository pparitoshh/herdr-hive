# Routing prompt tuning (routing step 2b)

Data: `tests/data/route_cases.json` (24 cases). Model: `model_fp16.onnx` from
`~/.cache/herdr-hive/decision-model/`. Runner: `scripts/eval_routing.py`
(deterministic: repeat runs give identical numbers). Temp details:
`.tmp/routing-eval.json`.

Holdout: 6 cases (2 per tier), titles fixed in the runner:
`README typo`, `version flag` (fast), `dry-run flag`, `stale picker answer`
(regular), `schema source of truth`, `status latency spike` (think). Tuning used
only the other 18.

## Variants

| variant | descs | prefs | state text | option order |
|---|---|---|---|---|
| baseline | old short text | `cost: low cost before speed` | title + task | fast, regular, think |
| rich_desc | rich | base | title + task | base |
| prefs_only | base | rich | title + task | base |
| state_feats | base | base | length + keyword signals + 40 files | base |
| rich+feats | rich | base | length + signals + 40 files (no title) | base |
| rich+basefeats | rich | base | title + task + length + signals + 40 files | base |
| rich+prefs | rich | rich | title + task | base |
| order_rev | base | base | title + task | think, regular, fast |
| all | rich | rich | length + signals + 40 files | base |
| all_rev | rich | rich | length + signals + 40 files | reversed |

State text variants: `title + task` = the old builder. `feats` appends "The task
text is about N characters long.", "Signals in the text: ..." (keyword hints for
design, concurrency, perf, hard debug, tiny edits) and a fixed "The repo has 40
files." (file count was a constant, real runs use the real count).

## Results (all 24 / tune 18 / holdout 6 / borderline 4)

| variant | acc | accT | accH | accB | mean conf | conf right | conf wrong | gap |
|---|---|---|---|---|---|---|---|---|
| baseline | 0.67 | 0.56 | 1.00 | 0.50 | 0.446 | 0.459 | 0.419 | 0.040 |
| rich_desc | 0.67 | 0.61 | 0.83 | 0.25 | 0.412 | 0.429 | 0.377 | 0.052 |
| prefs_only | 0.29 | 0.28 | 0.33 | 0.25 | 0.454 | 0.512 | 0.431 | 0.082 |
| state_feats | 0.67 | 0.67 | 0.67 | 0.50 | 0.431 | 0.451 | 0.390 | 0.061 |
| rich+feats | 0.83 | 0.78 | 1.00 | 0.75 | 0.400 | 0.404 | 0.377 | 0.027 |
| rich+basefeats | 0.71 | 0.67 | 0.83 | 0.50 | 0.412 | 0.417 | 0.398 | 0.019 |
| rich+prefs | 0.71 | 0.67 | 0.83 | 0.25 | 0.397 | 0.410 | 0.366 | 0.045 |
| order_rev | 0.71 | 0.61 | 1.00 | 0.50 | 0.441 | 0.453 | 0.413 | 0.040 |
| all | 0.67 | 0.67 | 0.67 | 0.50 | 0.389 | 0.399 | 0.367 | 0.032 |
| all_rev | 0.38 | 0.39 | 0.33 | 0.50 | 0.408 | 0.435 | 0.391 | 0.044 |

Confusion for the keeper (want -> pick): fast->fast 7, fast->regular 1,
regular->regular 6, regular->think 2, think->regular 1, think->think 7.

## Pick

Best by tune accuracy, then confidence gap: `rich+feats` (rich descriptions +
state features, no title). Tune acc 0.78, all acc 0.83, holdout 1.00,
borderline 0.75, gap 0.027.

Keeper text shipped in `hive/router.py` and `hive/config.py`:

- Tier descriptions: rich text (size, files, risk, examples) for fast, regular,
  think. Same string in `TIER_OPTIONS` and `DEFAULT_TOML`/`TIERS`.
- State builder: keeps the title first (the API test pins
  `title. task.` at the start), then task text, task length sentence, keyword
  signals, real repo file count. Title-inclusive state measured 0.67 tune /
  0.83 holdout (`rich+basefeats` row). A later live step can measure the
  no-title state one more time before dropping the title for good.
- Preferences: kept `cost: low cost before speed`. The richer route preference
  made the model route almost everything to `fast` (prefs_only 0.29, rich+prefs
  confusion shows regular->fast 5).

## min_confidence proposal

From the keeper thresholds (send to the human below t):

| t | wrong below t | right below t | wrong % | right % |
|---|---|---|---|---|
| 0.35 | 0 | 1 | 0 | 0.05 |
| 0.40 | 4 | 11 | 1.00 | 0.55 |
| 0.45 | 4 | 17 | 1.00 | 0.85 |
| 0.50 | 4 | 20 | 1.00 | 1.00 |

Proposed `min_confidence`: **0.40**. It catches every wrong pick of the eval,
but 55% of right picks also fall below it, so the human confirms over half the
routes. Confidence sits in a narrow band (mean 0.40), so no threshold separates
right from wrong well. Keep the config default 0.5 until live data (step 4)
shows the human-interruption rate is worth 0.40.

## Known limits

- 24 cases, one small model run each: tuning deltas under 0.1 are noise level.
- The borderline cases were written to straddle fast/regular and
  regular/think; borderline accuracy is the weakest metric everywhere.
- Baseline state text in the runner had no repo file count; the current
  `task_question` appends one at spawn time.
