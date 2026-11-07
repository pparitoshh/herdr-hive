import json, os, sys, tempfile, unittest
import unittest.mock as mock
from argparse import Namespace
from pathlib import Path
os.environ.setdefault("HIVE_STATE_DIR", tempfile.mkdtemp())
from hive import bus, commands, herdr_ops, state

STATE = {"agent_status": "idle", "focused": False}


def _pane_state(pane):
    return None if STATE["agent_status"] is None else dict(STATE)


def hook_args(event):
    return Namespace(event=event)


class HookTest(unittest.TestCase):
    def setUp(self):
        state.STATE_DIR = Path(tempfile.mkdtemp())
        self.conn = state.connect()
        self.d = Path(tempfile.mkdtemp())
        self.hid = state.add_hive(self.conn, name="bt", repo=str(self.d), branch="hive/bt",
                                  base="main", worktree=str(self.d / "wt"))
        bus.join("w1.bt", "pane-w1", conn=self.conn)
        bus.join("brain.bt", "pane-brain", conn=self.conn)
        STATE.update(agent_status="idle", focused=False)
        self.monitor = []
        self.patchers = [
            mock.patch.object(herdr_ops, "pane_state", side_effect=_pane_state),
            mock.patch.object(herdr_ops, "agent_prompt",
                              side_effect=lambda pane, text: self.monitor.append((pane, text))),
        ]
        for pr in self.patchers:
            pr.start()
        self.ev_saved = os.environ.get("HERDR_PLUGIN_EVENT_JSON")

    def tearDown(self):
        for pr in self.patchers:
            pr.stop()
        if self.ev_saved is None:
            os.environ.pop("HERDR_PLUGIN_EVENT_JSON", None)
        else:
            os.environ["HERDR_PLUGIN_EVENT_JSON"] = self.ev_saved

    def set_event(self, **kw):
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(kw)

    def pending_row(self):
        STATE.update(agent_status="working")  # holds the row
        bus.send("w1.bt", "note", "bt", "hold me", sender="brain.bt", conn=self.conn)
        STATE.update(agent_status="idle")
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        assert row["status"] == "pending", row["status"]
        return row

    def test_idle_event_delivers(self):
        self.pending_row()
        self.set_event(pane_id="pane-w1", agent_status="idle")
        rc = commands.cmd_hook(hook_args("agent-status"))
        self.assertEqual(rc, 0)
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual(row["status"], "delivered")
        self.assertEqual(len(self.monitor), 1)

    def test_working_event_no_flush(self):
        self.pending_row()
        self.set_event(pane_id="pane-w1", agent_status="working")
        rc = commands.cmd_hook(hook_args("agent-status"))
        self.assertEqual(rc, 0)
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("pending", 0))
        self.assertEqual(self.monitor, [])

    def test_pane_without_handle_no_flush(self):
        self.pending_row()
        self.set_event(pane_id="pane nobody", agent_status="idle")
        rc = commands.cmd_hook(hook_args("agent-status"))
        self.assertEqual(rc, 0)
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(self.monitor, [])

    def test_pane_keeps_own_queue_only(self):
        self.pending_row()  # for w1.bt (pane-w1)
        self.set_event(pane_id="pane-brain", agent_status="idle")  # other pane, other handle
        rc = commands.cmd_hook(hook_args("agent-status"))
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(self.monitor, [])

    def test_bad_json_exit_0(self):
        self.pending_row()
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = "{not json"
        rc = commands.cmd_hook(hook_args("agent-status"))
        self.assertEqual(rc, 0)
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(self.monitor, [])

    def test_pane_focused_event_delivers(self):
        self.pending_row()
        self.set_event(pane_id="pane-w1")
        rc = commands.cmd_hook(hook_args("pane-focused"))
        self.assertEqual(rc, 0)
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual(row["status"], "delivered")


if __name__ == "__main__":
    unittest.main()
