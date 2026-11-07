import os, tempfile, unittest
from pathlib import Path
os.environ["HIVE_STATE_DIR"] = tempfile.mkdtemp()
from hive import config, state


class ConfigTest(unittest.TestCase):
    def test_default_and_override(self):
        d = Path(tempfile.mkdtemp())
        g = d / "h.toml"
        cfg = config.load(path=g)
        self.assertEqual(cfg.max_workers, 3)
        self.assertIn("claude", cfg.harnesses)
        repo = d / "repo"; repo.mkdir()
        (repo / ".hive.toml").write_text('[defaults]\nmax_workers = 1\nbase_branch = "dev"\n')
        cfg = config.load(repo, path=g)
        self.assertEqual((cfg.max_workers, cfg.base_branch), (1, "dev"))
        argv = cfg.harness("claude").argv("claude-haiku-5-5", worker=True)
        self.assertEqual(argv, ["claude", "--model", "claude-haiku-5-5", "--permission-mode", "acceptEdits"])

    def test_models_command(self):
        h = config.Harness(name="x", label="X", command=["x"], models=["a", "b"],
                           models_command=["printf", "b\\nc\\n\\nd\\n"])
        self.assertEqual(h.model_choices(), ["a", "b", "c", "d"])
        h.models_command = ["false"]
        self.assertEqual(h.model_choices(), ["a", "b"])
        h.models_command = ["/no/such/cmd"]
        self.assertEqual(h.model_choices(), ["a", "b"])

    def test_duration(self):
        self.assertEqual(config.parse_duration("30m"), 1800)
        self.assertEqual(config.parse_duration("5"), 5)

    def test_state(self):
        c = state.connect(Path(tempfile.mkdtemp()) / "x.db")
        hid = state.add_hive(c, name="h", worktree="/tmp/h")
        state.add_worker(c, hive_id=hid, n=1, worktree="/tmp/h-w1", status="running")
        self.assertEqual(state.next_worker_n(c, hid), 2)
        self.assertEqual(state.hive_by_worktree(c, "/tmp/h-w1/x")["name"], "h")
        self.assertEqual(len(state.workers(c, hid, live_only=True)), 1)


if __name__ == "__main__":
    unittest.main()
