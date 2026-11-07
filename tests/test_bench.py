import asyncio, os, tempfile, unittest
from pathlib import Path
os.environ.setdefault("HIVE_STATE_DIR", tempfile.mkdtemp())
from hive import state
from hive.bench import app as bench

DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 one
-two
+TWO
+three
"""


class BenchTest(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.conn = state.connect(d / "hive.db")
        hid = state.add_hive(self.conn, name="hv", repo=str(d), branch="hive/hv", base="main",
                             worktree=str(d / "wt"), workspace_id="ws1")
        state.add_worker(self.conn, hive_id=hid, n=1, branch="hive/hv/w1", worktree=str(d / "w1"),
                         title="fix it", status="reported")
        state.add_worker(self.conn, hive_id=hid, n=2, branch="hive/hv/w2", title="old", status="merged")
        self.sent = []
        self.orig = (bench.bus_send, bench.load_worker_diff, bench.load_total_diff)
        bench.bus_send = lambda to, type_, hive, text, **kw: self.sent.append((to, type_, text, kw)) or "ok"
        bench.load_worker_diff = lambda *a: DIFF
        bench.load_total_diff = lambda *a: ""

    def tearDown(self):
        bench.bus_send, bench.load_worker_diff, bench.load_total_diff = self.orig

    def run_keys(self, keys, before=None):
        async def go():
            app = bench.BenchApp("hv", conn=self.conn)
            async with app.run_test() as pilot:
                await pilot.pause()
                if before:
                    before(app)
                    await pilot.pause()
                for k in keys:
                    await pilot.press(k)
                    await pilot.pause()
                return app
        return asyncio.run(go())

    def test_entries_and_approve(self):
        app = self.run_keys(["a"])
        self.assertEqual(app._entries[0][1], "w1")
        self.assertEqual([k for _, k in app._entries], ["w1", "total"])
        self.assertEqual(len(self.sent), 1)
        to, type_, _, kw = self.sent[0]
        self.assertEqual((to, type_, kw["value"], kw["worker"]), ("brain.hv", "verdict", "approve", 1))
        self.assertEqual(kw["workspace"], "ws1")

    def test_comment_goes_to_worker_and_brain(self):
        def pick_line(app):
            # rows: meta, ---, +++, hunk, ctx one, del two, add TWO
            app.query_one("#diff").move_cursor(row=6)
        self.run_keys(["c", "n", "i", "t", "enter"], before=pick_line)
        self.assertEqual([s[0] for s in self.sent], ["w1.hv", "brain.hv"])
        for to, type_, text, kw in self.sent:
            self.assertEqual(type_, "review_comment")
            self.assertEqual(text, "a.py:2\nnit")
            self.assertEqual(kw["sender"], "bench.hv")

    def test_bus_failure_does_not_crash(self):
        def boom(*a, **k):
            raise RuntimeError("no bus")
        bench.bus_send = boom
        app = self.run_keys(["a"])
        self.assertIn("bus error", app.last_status)


if __name__ == "__main__":
    unittest.main()
