"""Routing step 3b: cmd_spawn uses the router + takeover, --tier flag, retrain
log lines and `hive router export` (plan §Spawn flow, §State and feedback)."""
import contextlib, io, json, os, tempfile, unittest
from contextlib import redirect_stdout
from argparse import Namespace
from pathlib import Path
from unittest import mock

os.environ.setdefault("HIVE_STATE_DIR", tempfile.mkdtemp())
from hive import commands, config, routelog, router, state

PROBS_AUTO = {"fast": 0.1, "regular": 0.6, "think": 0.3}
PROBS_LOW = {"fast": 0.34, "regular": 0.33, "think": 0.33}


def _d() -> Path:
    return Path(tempfile.mkdtemp())


class Picker:
    """Fake picker popup; records calls and returns queued answers."""

    def __init__(self, *answers):
        self.calls = []
        self.answers = iter(answers)

    def __call__(self, title, prompt, choices, **kw):
        self.calls.append({"title": title, "prompt": prompt, "choices": choices})
        try:
            return next(self.answers)
        except StopIteration:
            raise AssertionError(f"unexpected picker call: {prompt}") from None


def fake_ask_server(capture, probs=None, error=None):
    def f(request, timeout=2.0, start_ok=True):
        capture["request"] = request
        capture["timeout"] = timeout
        if error is not None:
            raise router.RouterError(error)
        return {"decision_id": "d1", "model_version": "v",
                "answers": [{"id": "tier", "type": "choice", "probs": dict(probs or {}), "confidence": 0.0}]}
    return f


class BaseSpawnTest(unittest.TestCase):
    def setUp(self):
        self.saved_dir = state.STATE_DIR
        state.STATE_DIR = _d()
        self.saved_global = config.GLOBAL_PATH
        self.config_path = _d() / "harnesses.toml"
        config.ensure_global(self.config_path)
        config.GLOBAL_PATH = self.config_path
        self.repo = _d() / "repo"
        self.repo.mkdir()
        (self.repo / "task.txt").write_text("Fix the login bug. One file only.")
        self.conn = state.connect(state.STATE_DIR / "hive.db")
        self.hid = state.add_hive(self.conn, name="t", repo=str(self.repo), branch="b",
                                  worktree=str(_d() / "wt"), workspace_id="ws", brain_pane="bp",
                                  status="open")
        self.out = io.StringIO()
        self.capture = {}

    def tearDown(self):
        self.conn.close()
        state.STATE_DIR = self.saved_dir
        config.GLOBAL_PATH = self.saved_global
        routelog.log_dir()

    def _patch_io(self, picker=None, router_fake=None):
        """Patch git, herdr splits, the agent launcher and the router client."""
        from hive import git_ops
        patches = [mock.patch.object(git_ops, "worktree_add"),
                   mock.patch.object(git_ops, "copy_worktreeinclude"),
                   mock.patch.object(git_ops, "add_info_exclude"),
                   mock.patch.object(commands, "_current_hive",
                                     lambda conn: conn.execute("SELECT * FROM hives WHERE id=?",
                                                               (self.hid,)).fetchone()),
                   mock.patch.object(commands.H, "split", return_value="pane-x"),
                   mock.patch.object(commands.bus, "send", return_value="m1"),
                   mock.patch.object(commands, "launch_agent")]
        if router_fake is not None:
            patches.append(mock.patch.object(router, "ask_server", router_fake))
        if picker is not None:
            patches.append(mock.patch.object(commands, "ask", picker))
        st = contextlib.ExitStack()
        for p in patches:
            st.enter_context(p)
        return st

    def spawn(self, **argv):
        ns = Namespace(task_file=str(self.repo / "task.txt"), title=None, tier=None)
        ns.__dict__.update(argv)
        with redirect_stdout(self.out):
            rc = commands.cmd_spawn(ns)
        return rc

    def worker(self):
        return state.workers(self.conn, self.hid)[0]

    def event_kinds(self):
        return [r["kind"] for r in self.conn.execute("SELECT kind FROM events ORDER BY id")]


class AutoOkTest(BaseSpawnTest):
    def test_high_confidence_router_decides(self):
        picker = Picker()
        with self._patch_io(picker=picker, router_fake=fake_ask_server(self.capture, PROBS_AUTO)):
            rc = self.spawn()
        self.assertEqual(rc, 0)
        w = self.worker()
        rj = json.loads(w["route_json"])
        self.assertEqual((w["tier"], rj["decided_by"], rj["confidence"]), ("regular", "router", 0.6))
        self.assertEqual(picker.calls, [])
        self.assertIn("route", self.event_kinds())
        self.assertIn(f"tier={w['tier']}", self.out.getvalue())
        self.assertEqual(routelog.cases(), [])  # the router decided: no case line

    def test_tier_harness_and_model_come_from_config(self):
        (self.repo / ".hive.toml").write_text(
            '[tiers.regular]\nharness = "claude"\nmodel = "claude-haiku-5-5"\n')
        with self._patch_io(router_fake=fake_ask_server(self.capture, PROBS_AUTO)):
            rc = self.spawn()
        self.assertEqual(rc, 0)
        w = self.worker()
        self.assertEqual((w["harness"], w["model"]), ("claude", "claude-haiku-5-5"))
        self.assertEqual(self.capture["request"]["questions"][0]["criteria"]["regular"],
                         "Normal coding work: a clear feature or bug fix, several files, tests")

    def test_tier_descriptions_come_from_repo_config(self):
        (self.repo / ".hive.toml").write_text('[tiers.regular]\ndescription = "custom reg description"\n')
        with self._patch_io(router_fake=fake_ask_server(self.capture, PROBS_AUTO)):
            self.spawn()
        self.assertEqual(self.capture["request"]["questions"][0]["criteria"]["regular"],
                         "custom reg description")
        self.assertEqual(self.capture["request"]["questions"][0]["instructions"],
                         router.TIER_QUESTION)


class LowConfidenceTest(BaseSpawnTest):
    def test_low_confidence_asks_human_and_logs(self):
        picker = Picker("think")
        with self._patch_io(picker=picker, router_fake=fake_ask_server(self.capture, PROBS_LOW)):
            rc = self.spawn()
        self.assertEqual(rc, 0)
        w = self.worker()
        rj = json.loads(w["route_json"])
        self.assertEqual((w["tier"], rj["decided_by"]), ("think", "human"))
        call = picker.calls[0]
        self.assertEqual(call["choices"], ["fast", "regular", "think"])  # router pick first
        self.assertIn("low confidence", call["prompt"])
        self.assertIn("fast 0.34", call["prompt"])
        cases = routelog.cases()
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual((case["router_pick"], case["human_pick"], case["final_tier"]),
                         ("fast", "think", "think"))
        self.assertEqual(case["outcome"], None)
        self.assertIn("low confidence", case["reason"])
        self.assertEqual(set(case["options"]), {"fast", "regular", "think"})
        self.assertTrue(case["state"].startswith("task. Fix the login bug"))
        self.assertEqual(case["state_features"]["task_chars"], len("Fix the login bug. One file only."))

    def test_cancel_stops_spawn(self):
        picker = Picker(None)  # the human hits cancel in the picker
        with self._patch_io(picker=picker, router_fake=fake_ask_server(self.capture, PROBS_LOW)):
            rc = self.spawn()
        self.assertEqual(rc, 4)
        self.assertEqual(state.workers(self.conn, self.hid), [])
        self.assertIn("spawn_declined", self.event_kinds())
        self.assertEqual(routelog.cases(), [])  # no worker, nothing to train on

    def test_unknown_tier_flag_fails(self):
        with self._patch_io():
            with self.assertRaises(SystemExit):
                self.spawn(tier="bogus")


class RouterDownTest(BaseSpawnTest):
    def test_router_error_asks_human_and_logs(self):
        picker = Picker("fast")
        fake = fake_ask_server(self.capture, error="router server unreachable: boom")
        with self._patch_io(picker=picker, router_fake=fake):
            rc = self.spawn()
        self.assertEqual(rc, 0)
        w = self.worker()
        rj = json.loads(w["route_json"])
        self.assertEqual((w["tier"], rj["decided_by"]), ("fast", "human"))
        picker_call = picker.calls[0]
        self.assertNotIn("low confidence", picker_call["prompt"])
        self.assertIn("no router probabilities", picker_call["prompt"])  # failure path shows no probs
        cases = routelog.cases()
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual((case["probs"], case["router_pick"]), ({}, None))
        self.assertEqual(case["reason"], "router server unreachable: boom")
        self.assertEqual(case["human_pick"], "fast")


class FlagTest(BaseSpawnTest):
    def test_tier_flag_skips_router_and_picker(self):
        fake = fake_ask_server(self.capture, error="must not be called")
        with self._patch_io(  # ask_server raises if the router is touched
                picker=Picker(), router_fake=fake):
            rc = self.spawn(tier="think")
        self.assertEqual(rc, 0)
        w = self.worker()
        rj = json.loads(w["route_json"])
        self.assertEqual((w["tier"], rj["decided_by"]), ("think", "flag"))
        self.assertEqual(rj["probs"], {})
        self.assertEqual(routelog.cases(), [])


class OutcomeTest(BaseSpawnTest):
    """The retrain log outcome lines (simpler schema: append, do not rewrite)."""

    def _case(self, wid, human_pick=None, final_tier="regular"):
        config.ensure_global(self.config_path)
        cfg = config.load(path=self.config_path)
        from hive.takeover import Route
        route = Route(tier=final_tier or human_pick,
                      probs={"fast": 0.3, "regular": 0.7, "think": 0.0} if human_pick else {},
                      confidence=0.1 if human_pick else 0.0, decided_by="human")
        request = router.task_question("Fix the login bug.", title="t", file_count=3,
                                       criteria=router.tier_criteria(cfg))
        return routelog.log_case({"repo": str(self.repo)}, wid, "t", "Fix the login bug.",
                                 request, route, cfg, reason="human picked with router probabilities",
                                 state_features={"title": "t", "file_count": 3}, human_pick=human_pick,
                                 now=1731000000.0)

    def test_fix_marks_outcome(self):
        self._case(1, human_pick="regular")
        commands._route_outcome(1, "fix")
        self.assertEqual(routelog.outcomes(), {1: "fix"})

    def test_export_label_rules(self):
        """Rows: one per case with a label; human_pick wins, else the
        final_tier of a merged case."""
        # case 1: human picked think
        self._case(1, human_pick="think", final_tier="think")
        # case 2: human picked regular
        self._case(2, human_pick="regular")
        # case 3: router-decided auto case that the log keeps only when the
        # user records it; hand-write the line, merged later
        with open(routelog.log_dir() / "2024-11-07.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": 1731000000.0, "worker_id": 3, "repo": str(self.repo),
                                "title": "t", "task_text": "x", "state_features": {},
                                "state": "s3", "instructions": router.TIER_QUESTION,
                                "options": {"fast": "f", "regular": "r", "think": "th"},
                                "probs": {}, "router_pick": None, "confidence": 0.0,
                                "reason": "r", "human_pick": None, "final_tier": "regular",
                                "outcome": None}) + "\n")
        routelog.log_outcome(3, "merge", now=1731000001.0)
        out = _d() / "train.jsonl"
        path, rows = routelog.export(out=out)
        data = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual(path, out)
        self.assertEqual(rows, 3)
        by_state = {r["state"]: r for r in data}
        self.assertEqual(by_state["s3"]["label"], "regular")  # outcome merge -> final_tier
        self.assertEqual({r["label"] for r in data}, {"think", "regular"})
        for r in data:
            self.assertEqual(set(r), {"state", "instructions", "options", "label"})
            self.assertEqual(r["instructions"], router.TIER_QUESTION)


class RouterAskTest(unittest.TestCase):
    def test_ask_reads_tier_descriptions_from_config(self):
        d = _d()
        (d / ".hive.toml").write_text('[tiers.regular]\ndescription = "custom reget here"\n')
        task = _d() / "task.txt"
        task.write_text("Do a small thing.")
        capture = {}
        saved_dir = state.STATE_DIR
        state.STATE_DIR = d  # router.ask cmd does not touch state, but keep the env sane
        try:
            with mock.patch.object(config, "GLOBAL_PATH", d / "global.toml"), \
                    mock.patch.dict(os.environ, {"HIVE_CWD": str(d)}), \
                    mock.patch.object(router, "ask_server", fake_ask_server(capture, PROBS_AUTO)):
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = router.cmd_router_ask(Namespace(task_file=str(task), title=None,
                                                         timeout="2", json=False))
            self.assertEqual(rc, 0)
            self.assertEqual(capture["request"]["questions"][0]["criteria"]["regular"],
                             "custom reget here")
            self.assertIn("regular", out.getvalue())
        finally:
            state.STATE_DIR = saved_dir


if __name__ == "__main__":
    unittest.main()
