"""Routing step 3a: workers table migration, status tier column, worker TMPDIR,
startup socket cleanup (plan §Config, §State and feedback, §Spawn flow)."""
import contextlib, io, json, os, socket, sqlite3, subprocess, tempfile, unittest
from contextlib import redirect_stdout
from argparse import Namespace
from pathlib import Path
from unittest import mock
os.environ.setdefault("HIVE_STATE_DIR", tempfile.mkdtemp())
from hive import bus, cli, commands, config, herdr_ops, state

ROOT = Path(__file__).resolve().parent.parent

OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS hives(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, repo TEXT, branch TEXT, base TEXT, worktree TEXT,
  workspace_id TEXT, brain_pane TEXT, brain_harness TEXT, brain_model TEXT,
  status TEXT DEFAULT 'open', created_at REAL);
CREATE TABLE IF NOT EXISTS workers(
  id INTEGER PRIMARY KEY, hive_id INTEGER REFERENCES hives(id), n INTEGER, branch TEXT, worktree TEXT,
  pane_id TEXT, harness TEXT, model TEXT, title TEXT, task_ref TEXT,
  status TEXT DEFAULT 'spawning', created_at REAL, merged_at REAL, UNIQUE(hive_id, n));
"""


def _d() -> Path:
    return Path(tempfile.mkdtemp())


class MigrationTest(unittest.TestCase):
    def _old_db(self):
        db = _d() / "x.db"
        conn = sqlite3.connect(db)
        conn.executescript(OLD_SCHEMA)
        conn.execute("INSERT INTO hives(id, name, status) VALUES(1, 'h', 'open')")
        conn.execute("INSERT INTO workers(id, hive_id, n, status) VALUES(1, 1, 1, 'running')")
        conn.commit()
        conn.close()
        return db

    def test_old_db_gets_tier_columns(self):
        db = self._old_db()
        conn = state.connect(db)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(workers)")]
        self.assertEqual([c for c in cols if c in ("tier", "route_json")], ["tier", "route_json"])
        conn.execute("UPDATE workers SET tier=?, route_json=? WHERE id=1", ("think", "{}"))
        self.assertEqual(conn.execute("SELECT tier FROM workers WHERE id=1").fetchone()[0], "think")

    def test_migrate_twice_is_idempotent(self):
        db = self._old_db()
        state.connect(db).close()
        state.connect(db).close()
        conn = state.connect(db)
        self.assertEqual(len(state.workers(conn, 1)), 1)

    def test_new_db_has_columns(self):
        conn = state.connect(_d() / "x.db")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workers)")}
        self.assertTrue({"tier", "route_json"} <= cols)


class StatusTierTest(unittest.TestCase):
    def setUp(self):
        self.saved = state.STATE_DIR
        state.STATE_DIR = _d()
        self.conn = state.connect()  # hive.db inside STATE_DIR
        hid = state.add_hive(self.conn, name="tst", worktree=str(_d() / "wt"), status="open")
        state.add_worker(self.conn, hive_id=hid, n=2, worktree=str(_d() / "w"), harness="claude",
                         model="claude-haiku-5-5", tier="think", pane_id="p9", status="running")
        state.add_worker(self.conn, hive_id=hid, n=3, worktree=str(_d() / "w"), harness="claude",
                         model="claude-haiku-5-5", pane_id="p10", status="running")

    def tearDown(self):
        self.conn.close()
        state.STATE_DIR = self.saved

    def test_status_shows_tier(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.cmd_status(Namespace(json=False, all=True))
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("tier=think", text)
        self.assertIn("tier=-", text)

    def test_status_json_includes_tier(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.cmd_status(Namespace(json=True, all=True))
        self.assertEqual(rc, 0)
        hives = json.loads(out.getvalue())
        tiers = {w["tier"] for h in hives for w in h["workers"]}
        self.assertEqual(tiers, {"think", None})


class TmpdirTest(unittest.TestCase):
    def _launch(self, worktree):
        calls = {}

        def fake_run(pane, argv):
            calls["argv"] = argv

        harness = config.Harness(name="opencode", label="O", command=["opencode"],
                                 model_arg=["--model", "{model}"])
        patches = [mock.patch.object(herdr_ops, "pane_rename"),
                   mock.patch.object(herdr_ops, "wait_shell"),
                   mock.patch.object(herdr_ops, "wait_agent_ready", return_value=True),
                   mock.patch.object(herdr_ops, "agent_prompt"),
                   mock.patch.object(bus, "join"),
                   mock.patch.object(herdr_ops, "pane_run", side_effect=fake_run)]
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            commands.launch_agent("p1", "w1.t", harness, "m", worker=True, role="r",
                                  workspace="ws", worktree=worktree)
        return calls["argv"]

    def test_worker_gets_worktree_tmpdir(self):
        wt = _d() / "wt"
        self.assertFalse((wt / ".tmp").exists())
        argv = self._launch(str(wt))
        self.assertEqual(argv[0:2], ["env", f"TMPDIR={wt / '.tmp'}"])
        self.assertEqual(argv[2:], ["opencode", "--model", "m"])
        self.assertTrue((wt / ".tmp").is_dir())

    def test_brain_gets_no_tmpdir(self):
        wt = _d() / "wt2"
        harness = config.Harness(name="opencode", label="O", command=["opencode"])
        calls = {}

        def fake_run(pane, argv):
            calls["argv"] = argv

        patches = [mock.patch.object(herdr_ops, "pane_rename"),
                   mock.patch.object(herdr_ops, "wait_shell"),
                   mock.patch.object(herdr_ops, "wait_agent_ready", return_value=True),
                   mock.patch.object(herdr_ops, "agent_prompt"),
                   mock.patch.object(bus, "join"),
                   mock.patch.object(herdr_ops, "pane_run", side_effect=fake_run)]
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            commands.launch_agent("p1", "brain.t", harness, None, worker=False, role="r", workspace="ws")
        self.assertEqual(calls["argv"], ["opencode"])
        self.assertFalse((wt / ".tmp").exists())


class StartupSocketTest(unittest.TestCase):
    def _run_startup(self, state_dir):
        stub = _d() / "bin"
        stub.mkdir()
        herdr = stub / "herdr"
        herdr.write_text("#!/bin/sh\nexit 0\n")
        herdr.chmod(0o755)
        env = dict(os.environ, HIVE_STATE_DIR=str(state_dir), PATH=f"{stub}:{os.environ['PATH']}")
        return subprocess.run(["sh", str(ROOT / "scripts" / "startup.sh")],
                              env=env, capture_output=True, text=True, timeout=60)

    def _socket(self, path):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(path))
        return s

    def test_stale_socket_is_removed(self):
        sd = _d(); sd.mkdir(exist_ok=True)
        dead = sd / "router.sock"
        s = self._socket(dead)
        s.close()  # no listener, but the socket file stays
        self.assertTrue(dead.exists())
        r = self._run_startup(sd)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(dead.exists())

    def test_live_socket_is_kept(self):
        sd = _d(); sd.mkdir(exist_ok=True)
        alive = sd / "router.sock"
        s = self._socket(alive)
        s.listen(1)
        try:
            r = self._run_startup(sd)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(alive.exists())
        finally:
            s.close()
            alive.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
