"""Human takeover when the routing step cannot decide (plan §Router item 9).

The router, the ask functions and the caller live elsewhere. This module only
decides who picks the tier and returns a `Route`.

`ask_router(task_text, title) -> probs` raises `RouterUnavailable` when it has
no probabilities (missing deps, missing model, server down, timeout).
`ask_human(reason, probs, default) -> tier or None` is the picker popup.
Human `None` means cancel: the caller stops the spawn. There is no silent
default tier.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class Route:
    tier: str | None
    probs: dict[str, float]
    confidence: float
    decided_by: str  # "router"|"human"|"flag"


class RouterUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def human_prompt(reason: str, probs: dict[str, float], default: str | None) -> str:
    """Text for the picker popup: reason, probabilities sorted, default first."""
    lines = [reason]
    if probs:
        items = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
        if default is not None and default in probs:
            items = [(default, probs[default])] + [(t, p) for t, p in items if t != default]
        lines.append("router probabilities:")
        for tier, p in items:
            lines.append(f"  {tier} {p:.2f}")
    else:
        lines.append("no router probabilities")
    if default is not None:
        lines.append(f"default pick: {default}")
    return "\n".join(lines)


def _top(probs: dict[str, float]) -> tuple[str | None, float]:
    if not probs:
        return None, 0.0
    tier = max(probs, key=lambda t: probs[t])
    return tier, probs[tier]


def _human_route(tier: str | None, probs: dict[str, float]) -> Route:
    return Route(tier=tier, probs=probs, confidence=0.0, decided_by="human")


def decide_tier(task_text, title, tiers: list[str], mode: str, min_confidence: float,
                ask_router: Callable[[str, str], dict[str, float]],
                ask_human: Callable[[str, dict[str, float], str | None], str | None],
                flag_tier: str | None = None) -> Route:
    if flag_tier:
        return Route(tier=flag_tier, probs={}, confidence=1.0, decided_by="flag")
    try:
        probs = ask_router(task_text, title)
    except RouterUnavailable as e:
        answer = ask_human(e.reason, {}, None)
        return _human_route(answer, {})
    tier, conf = _top(probs)
    if mode == "ask":
        answer = ask_human("ask mode", probs, tier)
        return _human_route(answer, probs)
    if mode != "auto":
        raise ValueError(f"unknown routing mode: {mode}")
    if tier is not None and conf >= min_confidence:
        return Route(tier=tier, probs=probs, confidence=conf, decided_by="router")
    reason = (f"low confidence {conf:.2f} < {min_confidence:.2f}"
              if tier is not None else "router gave no tiers")
    answer = ask_human(reason, probs, tier)
    return _human_route(answer, probs)
