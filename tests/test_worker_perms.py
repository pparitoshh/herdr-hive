"""Task L: per-launch worker permissions and the trust-dialog notice.

- Claude worker: --settings <worktree>/.tmp/hive-perms.json + --add-dir <hive worktree>.
- OpenCode worker: OPENCODE_PERMISSION env var (external_directory + bash).
- A blocked worker pane showing the trust dialog toasts and messages the brain.
"""
import contextlib, json, os, tempfile, unittest
from pathlib import Path
from unittest import mock
os.environ.setdefault("HIVE_STATE_DIR", tempfile.mkdtemp())
from hive import bus, commands, config, herdr_ops, state

TRUST_TEXT = "Do you trust the files in this folder? claude code may read files here."
OTHER_TEXT = "Some other approval dialog text."
ALLOWED = ("Bash(hive msg:*)", "Bash(hive inbox:*)", "Bash(hive show:*)")


def _d() -> Path:
    return Path(tempfile.mkdtemp())


class BusFixup(unittest.TestCase):
    """A hive row, so bus.send works without herdr."""

    def setUp(self):
        self.saved_dir = state.STATE_DIR
        state.STATE_DIR = _d()
        self.conn = state.connect()
        self.hid = state.add_hive(self.conn, name="tst", worktree=str(_d() / "hive"),
                                  status="open", brain_pane="pB")
        self.hive_wt = Path(self.conn.execute("SELECT worktree FROM hives WHERE id=?",
                                              (self.hid,)).fetchone()[0])
        bus.join(bus.brain_handle("tst"), "pB", conn=self.conn)

    def tearDown(self):
        self.conn.close()
        state.STATE_DIR = self.saved_dir


class ClaudePermsTest(BusFixup):
    def _launch(self, worker=True, worktree=None, hive_worktree=None, kind="claude"):
        calls = {}

        def fake_run(pane, argv):
            calls["argv"] = argv

        harness = config.Harness(name=kind, label=kind.title(), command=[kind],
                                 model_arg=["--model", "{model}"],
                                 auto_approve_args=["--permission-mode", "acceptEdits"],
                                 herdr_kind=kind)
        patches = [mock.patch.object(herdr_ops, "pane_rename"),
                   mock.patch.object(herdr_ops, "wait_shell"),
                   mock.patch.object(herdr_ops, "wait_agent_ready", return_value=True),
                   mock.patch.object(herdr_ops, "agent_prompt"),
                   mock.patch.object(bus, "join"),
                   mock.patch.object(herdr_ops, "pane_run", side_effect=fake_run)]
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            commands.launch_agent("p1", "w1.tst", harness, "m", worker=worker, role="r",
                                  workspace="ws", worktree=worktree, hive_worktree=hive_worktree)
        return calls["argv"]

    def test_settings_file_and_add_dir(self):
        wt, hive_wt = _d() / "wt", _d() / "hive"
        argv = self._launch(worktree=str(wt), hive_worktree=str(hive_wt))
        self.assertEqual(argv[:2], ["env", f"TMPDIR={wt / '.tmp'}"])
        self.assertEqual(argv[2:3], ["claude"])
        self.assertIn("--add-dir", argv)
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(hive_wt))
        self.assertIn("--settings", argv)
        s = Path(argv[argv.index("--settings") + 1])
        self.assertEqual(s.parent, wt / ".tmp")
        self.assertTrue(s.is_file())
        perm = oct(s.stat().st_mode)[-3:]
        self.assertEqual(perm, "600")

    def test_settings_allow_only_hive_bus_commands(self):
        wt, hive_wt = _d() / "wt2", _d() / "hive2"
        argv = self._launch(worktree=str(wt), hive_worktree=str(hive_wt))
        s = Path(argv[argv.index("--settings") + 1])
        allow = json.loads(s.read_text())["permissions"]["allow"]
        self.assertEqual(sorted(allow), sorted(ALLOWED))

    def test_no_grant_without_hive_worktree(self):
        wt = _d() / "wt3"
        argv = self._launch(worktree=str(wt))
        self.assertNotIn("--settings", argv)
        self.assertNotIn("--add-dir", argv)
        self.assertEqual(argv[:2], ["env", f"TMPDIR={wt / '.tmp'}"])


class OpencodePermsTest(BusFixup):
    def _launch(self, worktree=None, hive_worktree=None):
        argv = ClaudePermsTest._launch(
            self, worker=True, worktree=worktree, hive_worktree=hive_worktree, kind="opencode")
        return argv

    def _env(self, argv):
        return dict(kv.split("=", 1) for kv in argv[1:argv.index("opencode")])

    def test_env_and_permission_json(self):
        wt, hive_wt = _d() / "wt", _d() / "hive3"
        argv = self._launch(worktree=str(wt), hive_worktree=str(hive_wt))
        self.assertEqual(argv[0], "env")
        env = self._env(argv)
        self.assertEqual(env["TMPDIR"], str(wt / ".tmp"))
        self.assertIn("OPENCODE_PERMISSION", env)
        perm = json.loads(env["OPENCODE_PERMISSION"])
        self.assertEqual(perm["permission"]["external_directory"],
                         {f"{hive_wt}/**": "allow"})
        bash = perm["permission"]["bash"]
        self.assertEqual(bash["hive msg*"], "allow")
        self.assertEqual(bash["hive inbox*"], "allow")
        self.assertEqual(bash["hive show*"], "allow")
        # only the hive worktree path is allowed
        self.assertEqual(list(perm["permission"]["external_directory"]), [f"{hive_wt}/**"])
        # other bash commands stay unapproved
        self.assertEqual(bash["*"], "ask")

    def test_no_opencode_permission_without_hive_worktree(self):
        wt = _d() / "wt2"
        argv = self._launch(worktree=str(wt))
        self.assertNotIn("OPENCODE_PERMISSION", self._env(argv))


class TrustNoticeTest(BusFixup):
    def setUp(self):
        super().setUp()
        self.toasts = []

        def fake_toast(title, body="", sound="request"):
            self.toasts.append((title, body))

        self.patches = [mock.patch.object(herdr_ops, "toast", side_effect=fake_toast),
                        mock.patch.object(herdr_ops, "pane_state",
                                          return_value={"agent_status": "idle", "focused": False}),
                        mock.patch.object(herdr_ops, "agent_prompt")]

    def _notice(self, handle="w1.tst"):
        return commands.blocked_notice(handle, "pW1")

    def test_trust_dialog_toasts_and_tells_brain(self):
        sends = []

        def fake_send(to, type_, hive, text, **kw):
            sends.append((to, type_, hive, text, kw))
            return "1"

        with contextlib.ExitStack() as st:
            for p in self.patches:
                st.enter_context(p)
            st.enter_context(mock.patch.object(bus, "send", side_effect=fake_send))
            self._notice()({"label": "w1-x", "agent_status": "blocked"}, TRUST_TEXT)
        self.assertIn(("Hive: worker blocked", "worker w1.tst waits on the trust dialog in pane w1-x"),
                      self.toasts)
        self.assertEqual(sends, [("brain.tst", "system", "tst",
                                  "worker w1.tst waits on the trust dialog in pane w1-x",
                                  {"sender": "hive.tst"})])

    def test_other_dialog_gets_generic_toast_only(self):
        with contextlib.ExitStack() as st:
            for p in self.patches:
                st.enter_context(p)
            st.enter_context(mock.patch.object(bus, "send", side_effect=AssertionError))
            self._notice()({"label": "w1-y", "agent_status": "blocked"}, OTHER_TEXT)
        self.assertIn(("Hive: agent needs you", "Answer the dialog in pane w1-y"), self.toasts)
        self.assertEqual(len(self.toasts), 1)

    def test_brain_blocked_does_not_send_bus_message(self):
        with contextlib.ExitStack() as st:
            for p in self.patches:
                st.enter_context(p)
            st.enter_context(mock.patch.object(bus, "send", side_effect=AssertionError))
            notice = commands.blocked_notice(bus.brain_handle("tst"), "pB")
            notice({"label": "hive", "agent_status": "blocked"}, TRUST_TEXT)
        self.assertIn(("Hive: worker blocked", "worker brain.tst waits on the trust dialog in pane hive"),
                      self.toasts)

    def test_wait_agent_ready_calls_on_dialog_once(self):
        pane = {"pane_id": "p1", "label": "w1", "agent_status": "blocked"}
        ready = {"pane": dict(pane, agent_status="done", agent="claude")}
        seen = []
        with contextlib.ExitStack() as st:
            t = mock.MagicMock()
            t.time.side_effect = [0, 1, 2, 99999]
            t.sleep = lambda s: None
            st.enter_context(mock.patch.object(herdr_ops, "time", t))
            st.enter_context(mock.patch.object(herdr_ops, "pane_get",
                                               side_effect=[pane, ready["pane"]]))
            st.enter_context(mock.patch.object(herdr_ops, "pane_text", return_value="TRUST"))
            st.enter_context(mock.patch.object(herdr_ops, "agent_prompt"))
            rc = herdr_ops.wait_agent_ready("p1", on_dialog=lambda p, txt: seen.append((p, txt)))
        self.assertFalse(rc)  # it exits on the blocked branch timeout, not on ready
        self.assertEqual(seen, [(pane, "TRUST")])


class PretrustConfigTest(unittest.TestCase):
    def test_default_false(self):
        d = _d()
        cfg = config.load(path=d / "h.toml")
        self.assertFalse(cfg.workers.pretrust)

    def test_override_true(self):
        d = _d()
        g = d / "h.toml"
        g.write_text(config.DEFAULT_TOML.replace("pretrust = false", "pretrust = true"))
        cfg = config.load(path=g)
        self.assertTrue(cfg.workers.pretrust)

    def test_default_toml_has_key(self):
        self.assertIn("pretrust = false", config.DEFAULT_TOML)


if __name__ == "__main__":
    unittest.main()
