"""Live-test support: the `plain` picker mode (routing step 4, task K).

`hive` popups never show app notifications on demand, so scripts and live
tests answer with a numbered terminal prompt instead. HIVE_ASK_MODE=plain
fills this gap; the Textual picker (`ask_inline`) blocks on non-tty stdin,
which hung `hive new` on the first routing live test run.
"""
import unittest

from hive import picker


class PlainAskTest(unittest.TestCase):
    def test_plain_prompt_picks_choice_number(self):
        lines = []

        def input_fn(_prompt: str) -> str:
            lines.append(_prompt)
            return "2"

        import io
        out = io.StringIO()
        ans = picker.ask_plain({"title": "t", "prompt": "pick", "choices": ["a", "b"],
                                "allow_text": False, "default": "a"}, input_fn=input_fn, out=out)
        self.assertEqual(ans, {"answer": "b", "cancelled": False})
        self.assertEqual(len(lines), 1)
        self.assertIn("2. b", out.getvalue())

    def test_plain_prompt_empty_input_picks_default(self):
        import io
        out = io.StringIO()
        ans = picker.ask_plain({"title": "t", "prompt": "pick", "choices": ["a", "b"],
                                "default": "a"}, input_fn=lambda _: "", out=out)
        self.assertEqual(ans, {"answer": "a", "cancelled": False})

    def test_plain_prompt_q_cancels(self):
        import io
        out = io.StringIO()
        ans = picker.ask_plain({"title": "t", "prompt": "pick", "choices": ["a", "b"],
                                "default": "a"}, input_fn=lambda _: "q", out=out)
        self.assertEqual(ans, {"answer": None, "cancelled": True})


if __name__ == "__main__":
    unittest.main()
