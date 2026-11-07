"""Unit tests for the human takeover path (plan §Router item 9, step 2)."""
import unittest
from hive.takeover import Route, RouterUnavailable, decide_tier, human_prompt

TIERS = ["fast", "regular", "think"]

GOOD = {"fast": 0.4, "regular": 0.6, "think": 0.0}
LOW = {"fast": 0.3, "regular": 0.35, "think": 0.35}


class Human:
    """Fake picker popup; records calls and returns queued answers."""

    def __init__(self, *answers):
        self.calls = []
        self.answers = iter(answers)

    def __call__(self, reason, probs, default):
        self.calls.append((reason, dict(probs), default))
        try:
            return next(self.answers)
        except StopIteration:
            raise AssertionError("unexpected human call")


def fail_router(reason):
    def ask(task_text, title):
        raise RouterUnavailable(reason)
    return ask


def ok_router(probs):
    def ask(task_text, title):
        return probs
    return ask


def decide(human, router=None, mode="auto", flag_tier=None, min_confidence=0.5):
    return decide_tier("do the thing", "t", TIERS, mode, min_confidence,
                       router or ok_router(GOOD), human, flag_tier=flag_tier)


class FlagTest(unittest.TestCase):
    def test_flag_skips_router_and_human(self):
        human = Human()
        r = decide(human, router=fail_router("server down"), flag_tier="think")
        self.assertEqual(r, Route(tier="think", probs={}, confidence=1.0, decided_by="flag"))
        self.assertEqual(human.calls, [])

    def test_empty_flag_tier_is_not_set(self):
        human = Human("regular")
        r = decide(human, router=fail_router("missing deps"), flag_tier=None)
        self.assertEqual(r.tier, "regular")


class RouterOkTest(unittest.TestCase):
    def test_high_confidence_router_decides(self):
        human = Human()
        r = decide(human)
        self.assertEqual(r.tier, "regular")
        self.assertEqual(r.probs, GOOD)
        self.assertEqual(r.confidence, 0.6)
        self.assertEqual(r.decided_by, "router")
        self.assertEqual(human.calls, [])

    def test_low_confidence_asks_human_with_top_default(self):
        human = Human("regular")
        r = decide(human, router=ok_router(LOW))
        self.assertEqual(r.tier, "regular")
        self.assertEqual(r.probs, LOW)
        self.assertEqual(r.decided_by, "human")
        reason, probs, default = human.calls[0]
        self.assertIn("low confidence", reason)
        self.assertIn("0.35", reason)
        self.assertEqual(probs, LOW)
        self.assertEqual(default, "regular")


class FailureTest(unittest.TestCase):
    def test_each_failure_reason_asks_human_without_probs(self):
        for reason in ("missing deps", "missing model", "server down", "timeout after 2 s"):
            with self.subTest(reason=reason):
                human = Human("think")
                r = decide(human, router=fail_router(reason))
                self.assertEqual(r.tier, "think")
                self.assertEqual(r.probs, {})
                self.assertEqual(r.decided_by, "human")
                self.assertEqual(human.calls, [(reason, {}, None)])

    def test_no_probs_no_default(self):
        human = Human("fast")
        r = decide(human, router=ok_router({}))
        self.assertNotIn("default pick", human.calls[0][0])
        self.assertEqual(r.tier, "fast",)


class AskModeTest(unittest.TestCase):
    def test_always_asks_human(self):
        human = Human("think")
        r = decide(human, mode="ask")
        self.assertEqual(r.tier, "think")
        self.assertEqual(r.decided_by, "human")
        self.assertEqual(human.calls[0], ("ask mode", GOOD, "regular"))


class CancelTest(unittest.TestCase):
    def test_human_cancel_means_no_tier(self):
        for mode, router in (("auto", ok_router(LOW)), ("auto", fail_router("server down")),
                             ("ask", ok_router(GOOD))):
            with self.subTest(mode=mode):
                human = Human(None)
                r = decide(human, router=router, mode=mode)
                self.assertIsNone(r.tier)
                self.assertEqual(r.decided_by, "human")

    def test_low_confidence_cancel(self):
        human = Human(None)
        r = decide(human, router=ok_router(LOW))
        self.assertIsNone(r.tier)


class ModeTest(unittest.TestCase):
    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            decide_tier("t", "t", TIERS, "fixed", 0.5, ok_router(GOOD), Human())


class PromptTest(unittest.TestCase):
    def test_sorted_probs_default_first(self):
        text = human_prompt("server down", GOOD, "regular")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        self.assertEqual(lines[0], "server down")
        self.assertIn("default pick: regular", lines)
        self.assertLess(lines.index("regular 0.60"), lines.index("fast 0.40"))

    def test_empty_probs(self):
        text = human_prompt("missing model", {}, None)
        self.assertIn("missing model", text)
        self.assertIn("no router probabilities", text)
        self.assertNotIn("default pick", text)


if __name__ == "__main__":
    unittest.main()
