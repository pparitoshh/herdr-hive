import io, os, stat, sys, tempfile, unittest
from contextlib import redirect_stderr, redirect_stdout
import unittest.mock as mock
from pathlib import Path
os.environ.setdefault("HIVE_STATE_DIR", tempfile.mkdtemp())
from hive import cli, herdr_ops, state
from hive import bus as b

STATE = {"agent_status": "idle", "focused": False}


def _pane_state(pane):
    return None if STATE["agent_status"] is None else dict(STATE)


class CliTest(unittest.TestCase):
    def setUp(self):
        state.STATE_DIR = Path(tempfile.mkdtemp())
        self.conn = state.connect()
        self.d = Path(tempfile.mkdtemp())
        self.hid = state.add_hive(self.conn, name="bt", repo=str(self.d), branch="hive/bt",
                                  base="main", worktree=str(self.d / "wt"))
        b.join("w1.bt", "pane-w1", conn=self.conn)
        b.join("brain.bt", "pane-brain", conn=self.conn)
        self.env = {"HIVE_NAME": "bt"}
        self.saved = {k: os.environ.get(k) for k in ("HIVE_NAME", "HERDR_PANE_ID")}
        os.environ.update(self.env)
        os.environ["HERDR_PANE_ID"] = "pane-w1"
        self.patchers = [
            mock.patch.object(herdr_ops, "pane_state", side_effect=_pane_state),
            mock.patch.object(herdr_ops, "agent_prompt", side_effect=lambda p, t: None),
        ]
        for pr in self.patchers:
            pr.start()

    def tearDown(self):
        for pr in self.patchers:
            pr.stop()
        os.environ.pop("HERDR_PANE_ID", None)
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        self.last_status = STATE["agent_status"]
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_msg_bad_ref_clean_error(self):
        rc, _, err = self.cli("msg", "brain.bt", "hi", "--ref", "/etc/passwd")
        self.assertEqual(rc, 1)
        self.assertIn("hive: ref outside", err)
        self.assertNotIn("Traceback", err)

    def test_msg_unknown_target_clean_error(self):
        rc, _, err = self.cli("msg", "w9.bt", "hi")
        self.assertEqual(rc, 1)
        self.assertIn("hive: unknown target", err)
        self.assertNotIn("Traceback", err)

    def test_msg_ok_without_traceback(self):
        rc, out, err = self.cli("msg", "brain.bt", "HIVE/1 type=report hive=bt worker=w1 hello")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("sent", out)
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual(row["status"], "delivered")
        self.assertEqual(row["sender"], "w1.bt")


if __name__ == "__main__":
    unittest.main()
