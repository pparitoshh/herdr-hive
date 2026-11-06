import subprocess, tempfile, unittest
from pathlib import Path
from hive import diffs


def git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)


class DiffsTest(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.repo = d / "repo"; self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t"); git(self.repo, "config", "user.name", "t")
        (self.repo / "a.py").write_text("one\ntwo\nthree\n")
        git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "base")
        git(self.repo, "checkout", "-qb", "hive")
        (self.repo / "h.txt").write_text("hive\n")
        git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "hive work")
        self.wt = d / "w1"
        git(self.repo, "worktree", "add", "-q", "-b", "hive-w1", str(self.wt), "hive")
        (self.wt / "a.py").write_text("one\nTWO\nthree\nfour\n")
        git(self.wt, "commit", "-qam", "w1 commit")
        (self.wt / "a.py").write_text("one\nTWO\nthree\nfour\nfive\n")  # unstaged
        (self.wt / "new.txt").write_text("fresh\n")  # untracked

    def test_worker_diff(self):
        d = diffs.worker_diff(str(self.wt), "hive", "hive-w1")
        files = {f.path: f for f in diffs.parse_unified(d)}
        self.assertEqual(set(files), {"a.py", "new.txt"})
        a = files["a.py"]
        adds = [(n, t) for k, o, n, t in a.lines if k == "add"]
        dels = [(o, t) for k, o, n, t in a.lines if k == "del"]
        self.assertEqual(adds, [(2, "TWO"), (4, "four"), (5, "five")])
        self.assertEqual(dels, [(2, "two")])
        idx = next(i for i, l in enumerate(a.lines) if l[0] == "del")
        self.assertEqual(a.location(idx), "a.py:-2")
        idx = next(i for i, l in enumerate(a.lines) if l[3] == "five")
        self.assertEqual(a.location(idx), "a.py:5")
        self.assertEqual([l[2] for l in files["new.txt"].lines if l[0] == "add"], [1])
        rows = diffs.side_by_side(a)
        self.assertIn(("two", "TWO", "chg"), rows)
        self.assertIn(("", "four", "add"), rows)

    def test_total_diff(self):
        d = diffs.total_diff(str(self.repo), "main", "hive")
        self.assertEqual([f.path for f in diffs.parse_unified(d)], ["h.txt"])


if __name__ == "__main__":
    unittest.main()
