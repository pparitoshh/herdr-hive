import os, stat, tempfile, unittest
import unittest.mock as mock
from pathlib import Path
os.environ.setdefault("HIVE_STATE_DIR", tempfile.mkdtemp())
from hive import bus, herdr_ops, state

STATE = {"agent_status": "idle", "focused": False}


def _pane_state(pane):
    return None if STATE["agent_status"] is None else dict(STATE)


class BusTest(unittest.TestCase):
    def setUp(self):
        state.STATE_DIR = Path(tempfile.mkdtemp())
        self.conn = state.connect()
        d = Path(tempfile.mkdtemp())
        self.wt = d / "wt"
        self.hid = state.add_hive(self.conn, name="myhive", repo=str(d), branch="hive/x",
                                  base="main", worktree=str(self.wt))
        bus.join("w1.myhive", "pane-w1", conn=self.conn)
        bus.join("brain.myhive", "pane-brain", conn=self.conn)
        STATE.update(agent_status="idle", focused=False)
        self.calls = []

    def fake_herdr(self):
        ps = mock.patch.object(herdr_ops, "pane_state", side_effect=_pane_state)
        ap = mock.patch.object(herdr_ops, "agent_prompt",
                               side_effect=lambda pane, text: self.calls.append(text))
        return ps, ap

    def send(self, **kw):
        text = kw.pop("body", "hello body")
        return bus.send("w1.myhive", "report", "myhive", text,
                        sender="brain.myhive", conn=self.conn, **kw)

    def test_send_ok(self):
        ps, ap = self.fake_herdr()
        with ps, ap:
            mid = self.send()
        row = bus.show(mid, conn=self.conn)
        self.assertEqual(row["status"], "delivered")
        self.assertEqual(len(self.calls), 1)
        env = self.calls[0]
        self.assertIn(f"[HIVE_MESSAGE id={mid} from=brain.myhive]", env)
        self.assertIn("HIVE/1 type=report hive=myhive", env)
        self.assertIn("hello body", env)
        self.assertIn(f"[END_HIVE_MESSAGE id={mid}]", env)
        self.assertNotIn("reply=required", env)

    def test_reply_required_flag(self):
        ps, ap = self.fake_herdr()
        with ps, ap:
            mid = self.send(reply_required=True)
        self.assertIn("reply=required", self.calls[0].splitlines()[0])

    def test_unknown_target_refused(self):
        with self.assertRaises(ValueError):
            bus.send("w9.myhive", "report", "myhive", "hi", sender="brain.myhive", conn=self.conn)
        with self.assertRaises(ValueError):
            bus.send("w1.nohive", "report", "nohive", "hi", sender="brain.myhive", conn=self.conn)

    def test_ref_outside_refused(self):
        with self.assertRaises(ValueError):
            self.send(ref="/etc/passwd")
        wk = state.add_worker(self.conn, hive_id=self.hid, n=1, branch="b", title="t")
        self.conn.execute("UPDATE workers SET worktree=? WHERE id=?",
                          (str(self.wt.parent / "w1wt"), wk))
        ps, ap = self.fake_herdr()
        with ps, ap:
            self.send(ref=str(self.wt / "notes.md"))  # inside the hive worktree: ok
            self.send(ref=str(self.wt.parent / "w1wt" / "notes.md"))  # inside a worker worktree: ok
        with self.assertRaises(ValueError):
            self.send(ref=str(self.wt.parent.parent / "elsewhere.md"))

    def test_4kb_spill(self):
        ps, ap = self.fake_herdr()
        text = "".join(f"line {i:04d} lorem ipsum\n" for i in range(200))
        self.assertEqual(len(text), 4400)
        text += "trailing padding " * 20
        self.assertGreater(len(text), 4096)
        with ps, ap:
            mid = self.send(body=text.replace("\n", " "))
        row = bus.show(mid, conn=self.conn)
        p = Path(row["ref"])
        self.assertTrue(p.exists())
        self.assertTrue(str(p).startswith(str(self.wt / ".hive" / "msgs") + os.sep))
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        body = row["body"].split("\n", 1)[1]
        self.assertLessEqual(len(body), 300)

    def test_idle_delivered(self):
        STATE.update(agent_status="working")
        ps, ap = self.fake_herdr()
        with ps, ap:
            self.send()
        row = bus.show(1, conn=self.conn)
        self.assertEqual(row["status"], "pending")
        STATE.update(agent_status="idle")
        with ps, ap:
            n = bus.flush(conn=self.conn)
        self.assertEqual(n, 1)
        self.assertEqual(bus.show(1, conn=self.conn)["status"], "delivered")
        self.assertGreater(bus.show(1, conn=self.conn)["delivered_at"], 0)

    def test_busy_stays_pending(self):
        ps, ap = self.fake_herdr()
        STATE.update(agent_status="working")
        with ps, ap:
            mid = self.send()
        self.assertEqual(bus.show(mid, conn=self.conn)["status"], "pending")
        self.assertEqual(self.calls, [])
        STATE.update(agent_status="blocked")
        with ps, ap:
            bus.flush(conn=self.conn)
        self.assertEqual(bus.show(mid, conn=self.conn)["status"], "pending")
        self.assertEqual(self.calls, [])
        self.assertEqual(bus.show(mid, conn=self.conn)["attempts"], 0)

    def test_focused_stays_pending(self):
        ps, ap = self.fake_herdr()
        STATE.update(focused=True)
        with ps, ap:
            mid = self.send()
        self.assertEqual(bus.show(mid, conn=self.conn)["status"], "pending")
        self.assertEqual(self.calls, [])

    def test_five_prompt_errors_fail(self):
        ps = mock.patch.object(herdr_ops, "pane_state", side_effect=_pane_state)
        ap = mock.patch.object(herdr_ops, "agent_prompt", side_effect=RuntimeError("kaboom"))
        with ps, ap:
            self.send()  # first try counts as attempt 1
            for _ in range(4):
                self.assertEqual(bus.flush(conn=self.conn), 0)
        row = bus.show(1, conn=self.conn)
        self.assertEqual((row["status"], row["attempts"]), ("failed", 5))

    def test_pane_gone_fails(self):
        ps, ap = self.fake_herdr()
        STATE.update(agent_status=None)
        with ps, ap:
            bus.part("w1.myhive", conn=self.conn)
            with self.assertRaises(ValueError):
                self.send()
            bus.join("w1.myhive", "pane-w1", conn=self.conn)  # handle live, pane gone
            self.send()
        self.assertEqual(bus.show(1, conn=self.conn)["status"], "failed")
        STATE.update(agent_status="idle")
        with ps, ap:
            bus.flush(conn=self.conn)
        self.assertEqual(bus.show(1, conn=self.conn)["status"], "failed")

    def test_flush_keeps_order_and_skips_stuck_target(self):
        def st(pane):
            return {"agent_status": "working" if pane == "pane-w1" else "idle", "focused": False}
        ps = mock.patch.object(herdr_ops, "pane_state", side_effect=st)
        ap = mock.patch.object(herdr_ops, "agent_prompt",
                               side_effect=lambda pane, text: self.calls.append((pane, text)))
        with ps, ap:
            old = self.send()
            bus.join("w2.myhive", "pane-w2", conn=self.conn)
            other = bus.send("w2.myhive", "note", "myhive", "for w2", sender="brain.myhive",
                             conn=self.conn)
            bus.flush(conn=self.conn)
        self.assertEqual(bus.show(old, conn=self.conn)["status"], "pending")
        self.assertEqual(bus.show(other, conn=self.conn)["status"], "delivered")
        for pane, text in self.calls:
            self.assertEqual(pane, "pane-w2")
            self.assertIn("for w2", text)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
