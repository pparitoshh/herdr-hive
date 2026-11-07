#!/usr/bin/env python3
"""Routing prompt tuning (routing step 2b).

Runs each case in tests/data/route_cases.json through the local ONNX decision
model for several prompt variants and prints accuracy, mean confidence,
confidence on right vs wrong picks, a confusion table, borderline accuracy and
holdout accuracy. Offline: the model loads from
~/.cache/herdr-hive/decision-model (see hive/router.py), no network.

Run from the repo root:

    TMPDIR=./.tmp uv run --no-project --with onnxruntime --with tokenizers \
        --with numpy python3 scripts/eval_routing.py

The script edits nothing in the repo. The keeper text goes back into
hive/router.py (TIER_OPTIONS, preferences, state builder) and hive/config.py
(DEFAULT_TOML). The table lands in .hive/tasks/routing-tuning.md.

Six cases (2 per tier, title fixed below) are holdout: tuning must not look at
them. The final pick uses tune accuracy, then the confidence gap
(conf on right picks minus conf on wrong picks).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hive.router import DecisionModel, RouterError  # noqa: E402

CASES_PATH = ROOT / "tests" / "data" / "route_cases.json"
TIERS = ("fast", "regular", "think")

HOLDOUT_TITLES = {
    "README typo", "version flag",                     # fast
    "dry-run flag", "stale picker answer",             # regular
    "schema source of truth", "status latency spike",  # think
}

TIER_QUESTION = "Which worker tier must handle this task?"

BASE_DESCS = {  # current text (before step 2b)
    "fast": "Short and simple tasks: one file, clear change, no design",
    "regular": "Normal coding tasks with tests",
    "think": "Long or difficult tasks: design, debugging, many files, unclear goal",
}

RICH_DESCS = {  # size, files, risk, examples
    "fast": "Small mechanical edit: one or two files, obvious and low risk, no tests needed. "
            "Examples: fix a typo, delete an unused import, rename in one file, change one message.",
    "regular": "Normal feature or bug fix: a few files and tests, clear goal, regular patterns. "
               "Examples: add a CLI flag end to end, fix an off-by-one with a regression test.",
    "think": "Hard or unclear work: design, unclear bugs, concurrency, performance, many-file refactor. "
             "Examples: fix a flaky test with no local repro, design a plugin API, find a memory leak.",
}

BASE_PREFS = {"cost": "low cost before speed"}

RICH_PREFS = {
    "cost": "low cost before speed",
    "route": "give small mechanical tasks the cheap tier; never give a task a tier that cannot finish it",
}

SIGNAL_WORDS = (
    ("refactor", "refactor"), ("design", "design"), ("architecture", "design"),
    ("interface", "design"), ("protocol", "design"), ("migration", "design"),
    ("race", "concurrency"), ("deadlock", "concurrency"), ("lock", "concurrency"),
    ("flaky", "hard-debug"), ("memory", "hard-debug"), ("leak", "hard-debug"),
    ("perf", "perf"), ("latency", "perf"), ("slow", "perf"), ("spike", "perf"),
    ("unclear", "unclear"), ("repro", "unclear"), ("investigat", "unclear"),
    ("typo", "tiny"), ("docstring", "tiny"), ("docstring", "tiny"), ("comment", "tiny"),
    ("README", "tiny"), ("import", "tiny"), ("rename", "tiny"), ("one file", "tiny"),
)


def signals(text: str) -> list[str]:
    hits, low = [], text.lower()
    for word, tag in SIGNAL_WORDS:
        if word.lower() in low and tag not in hits:
            hits.append(tag)
    return hits


def state_base(case) -> str:
    title = (case.get("title") or "").strip()
    out = title + ". " if title else ""
    return out + (case["task"] or "")[:2000].strip()


def state_feats(case) -> str:
    task = (case["task"] or "")[:2000].strip()
    parts = [task, f"The task text is about {len(task)} characters long."]
    sig = signals(case["task"])
    if sig:
        parts.append("Signals in the text: " + ", ".join(sig) + ".")
    parts.append("The repo has 40 files.")
    return ". ".join(p.strip() for p in parts if p.strip())


def state_both(case) -> str:
    return state_base(case) + " " + state_feats(case)


STATE_BUILDERS = {
    "base": state_base,      # current: title + task text
    "feats": state_feats,    # length + keyword signals only
    "both": state_both,      # base + features appended
}

BASE_ORDER = list(TIERS)

VARIANTS: dict[str, dict] = {
    "baseline":       dict(descs=BASE_DESCS, prefs=BASE_PREFS, state="base", order=BASE_ORDER),
    "rich_desc":      dict(descs=RICH_DESCS, prefs=BASE_PREFS, state="base", order=BASE_ORDER),
    "prefs_only":     dict(descs=BASE_DESCS, prefs=RICH_PREFS, state="base", order=BASE_ORDER),
    "state_feats":    dict(descs=BASE_DESCS, prefs=BASE_PREFS, state="feats", order=BASE_ORDER),
    "rich+feats":     dict(descs=RICH_DESCS, prefs=BASE_PREFS, state="feats", order=BASE_ORDER),
    "rich+basefeats": dict(descs=RICH_DESCS, prefs=BASE_PREFS, state="both", order=BASE_ORDER),
    "rich+prefs":     dict(descs=RICH_DESCS, prefs=RICH_PREFS, state="base", order=BASE_ORDER),
    "order_rev":      dict(descs=BASE_DESCS, prefs=BASE_PREFS, state="base", order=list(reversed(BASE_ORDER))),
    "all":            dict(descs=RICH_DESCS, prefs=RICH_PREFS, state="both", order=BASE_ORDER),
    "all_rev":        dict(descs=RICH_DESCS, prefs=RICH_PREFS, state="both",
                           order=list(reversed(BASE_ORDER))),
}


def evaluate(model: DecisionModel, variant: dict, cases) -> dict:
    per_case = []
    conf = {"right": [], "wrong": []}
    confusion = {(a, b): 0 for a in TIERS for b in TIERS}
    for c in cases:
        state_text = STATE_BUILDERS[variant["state"]](c)
        options = [(t, variant["descs"][t]) for t in variant["order"]]
        probs, _ = model.choice(state_text, TIER_QUESTION, options, variant["prefs"] or None)
        probs = {t: float(p) for t, p in probs.items()}
        pick = max(probs, key=probs.get)
        conf_i = probs[pick]
        want = c["tier"]
        right = pick == want
        confusion[(want, pick)] += 1
        conf["right" if right else "wrong"].append(conf_i)
        per_case.append({"title": c["title"], "want": want, "pick": pick, "conf": conf_i,
                         "borderline": bool(c.get("borderline"))})
    def acc(rows):
        return sum(1 for r in rows if r["pick"] == r["want"]) / max(len(rows), 1)
    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0
    tune = [r for r in per_case if r["title"] not in HOLDOUT_TITLES]
    hold = [r for r in per_case if r["title"] in HOLDOUT_TITLES]
    bord = [r for r in per_case if r["borderline"]]
    return {
        "per_case": per_case,
        "acc_all": acc(per_case),
        "acc_tune": acc(tune),
        "acc_holdout": acc(hold),
        "acc_borderline": acc(bord),
        "mean_conf": mean([r["conf"] for r in per_case]),
        "conf_right": mean(conf["right"]),
        "conf_wrong": mean(conf["wrong"]),
        "gap": mean(conf["right"]) - mean(conf["wrong"]),
        "confusion": confusion,
        "n_wrong": sum(1 for r in per_case if r["pick"] != r["want"]),
    }


def confusion_row(confusion):
    return "  ".join(f"{a}->{b}:{n}" for (a, b), n in confusion.items() if n)


def min_confidence_grid(res) -> list[tuple[float, int, int, float, float]]:
    rows = []
    wrongs = [r["conf"] for r in res["per_case"] if r["pick"] != r["want"]]
    rights = [r["conf"] for r in res["per_case"] if r["pick"] == r["want"]]
    n_right = len(rights)
    for t in [round(0.05 + 0.05 * i, 2) for i in range(19)]:
        wb = sum(1 for c in wrongs if c < t)
        rb = sum(1 for c in rights if c < t)
        rows.append((t, wb, rb, wb / max(len(wrongs), 1), rb / max(n_right, 1)))
    return rows


def propose_min_confidence(res) -> float:
    """Smallest threshold that keeps every wrong pick below it; else the best recall/effort ratio.

    Below the threshold hive sends the case to the human, so a good threshold
    catches the wrong picks with model-level confidence and as few right picks
    as possible."""
    grid = min_confidence_grid(res)
    full = [r for r in grid if r[4] <= 0.25 and r[3] >= 0.95]
    if full:
        return full[0][0]
    best = max(grid, key=lambda r: (r[3] - r[4], r[0]))
    return best[0]


def main() -> int:
    cases = json.loads(CASES_PATH.read_text())
    try:
        model = DecisionModel()
    except (RouterError, Exception) as e:  # noqa: BLE001 (missing files/deps -> clear exit)
        print(f"eval: cannot load the model: {e}", file=sys.stderr)
        return 2
    print(f"eval: model {model.model_file}, {len(cases)} cases, holdout {len(HOLDOUT_TITLES)}")
    header = (f"{'variant':<14} {'acc':>5} {'accT':>5} {'accH':>5} {'accB':>5} "
              f"{'mconf':>6} {'cR':>5} {'cW':>5} {'gap':>5}  confusion (want->pick)")
    print(header)
    results = {}
    for name, variant in VARIANTS.items():
        res = evaluate(model, variant, cases)
        results[name] = res
        print(f"{name:<14} {res['acc_all']:>5.2f} {res['acc_tune']:>5.2f} {res['acc_holdout']:>5.2f} "
              f"{res['acc_borderline']:>5.2f} {res['mean_conf']:>6.3f} {res['conf_right']:>5.3f} "
              f"{res['conf_wrong']:>5.3f} {res['gap']:>5.3f}  {confusion_row(res['confusion'])}")
    ranked = sorted(results.items(), key=lambda kv: (-kv[1]["acc_tune"], -kv[1]["gap"]))
    best_name, best = ranked[0]
    print(f"\nbest: {best_name} (tune acc {best['acc_tune']:.2f}, gap {best['gap']:.3f}, "
          f"holdout acc {best['acc_holdout']:.2f}, borderline acc {best['acc_borderline']:.2f})")
    wrong = ", ".join(f"{r['title']} (want {r['want']}, got {r['pick']} {r['conf']:.2f})"
                      for r in best["per_case"] if r["pick"] != r["want"]) or "none"
    print(f"best wrong picks: {wrong}")
    print("\nmin_confidence grid for the best variant (t=send to human below t):")
    print("    t     wrong<t  right<t  wrong%   right%")
    for t, wb, rb, wp, rp in min_confidence_grid(best):
        print(f"  {t:.2f}  {wb:>7} {rb:>8} {wp:>7.2f} {rp:>7.2f}")
    print(f"\nproposed min_confidence: {propose_min_confidence(best)}")
    out = ROOT / ".tmp" / "routing-eval.json"
    out.parent.mkdir(exist_ok=True)
    for res in results.values():
        res["confusion"] = {f"{a}->{b}": n for (a, b), n in res["confusion"].items()}
    out.write_text(json.dumps({"best": best_name, "results": results}, default=str))
    print(f"details: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
