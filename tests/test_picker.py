import asyncio, io, json, os, tempfile, unittest
from pathlib import Path
from unittest import mock
from hive import picker


class PickerTest(unittest.TestCase):
    def _main(self, req, answers):
        d = Path(tempfile.mkdtemp())
        (d / "req.json").write_text(json.dumps(req))
        env = {"HIVE_PICKER_REQ": str(d / "req.json"), "HIVE_PICKER_OUT": str(d / "out.json")}
        it = iter(answers)
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(picker, "ask_inline", lambda r: picker.ask_plain(r, input_fn=lambda _: next(it), out=io.StringIO())):
            rc = picker.main([])
        return rc, json.loads((d / "out.json").read_text())

    def test_choice_by_number(self):
        rc, out = self._main({"title": "t", "prompt": "p", "choices": ["x", "y"], "allow_text": False}, ["2"])
        self.assertEqual((rc, out), (0, {"answer": "y", "cancelled": False}))

    def test_default_text_and_cancel(self):
        req = {"title": "t", "prompt": "p", "choices": [], "allow_text": True, "default": "main"}
        self.assertEqual(self._main(req, [""])[1]["answer"], "main")
        self.assertEqual(self._main(req, ["feat/x"])[1]["answer"], "feat/x")
        rc, out = self._main(req, ["q"])
        self.assertEqual((rc, out), (1, {"answer": None, "cancelled": True}))

    def test_textual_filter_and_escape(self):
        async def run(req, keys):
            app = picker.build_app(req)
            async with app.run_test() as pilot:
                for k in keys:
                    await pilot.press(k)
                await pilot.pause()
            return app.result
        req = {"title": "t", "prompt": "", "choices": ["alpha", "beta", "gamma"], "allow_text": True}
        self.assertEqual(asyncio.run(run(req, ["g", "enter"])), {"answer": "gamma", "cancelled": False})
        self.assertEqual(asyncio.run(run(req, ["down", "enter"])), {"answer": "beta", "cancelled": False})
        self.assertEqual(asyncio.run(run(req, ["z", "enter"])), {"answer": "z", "cancelled": False})
        self.assertTrue(asyncio.run(run(req, ["escape"]))["cancelled"])
        req = {"title": "t", "prompt": "", "choices": [], "allow_text": True, "default": None}
        self.assertEqual(asyncio.run(run(req, ["f", "o", "enter"]))["answer"], "fo")


if __name__ == "__main__":
    unittest.main()
