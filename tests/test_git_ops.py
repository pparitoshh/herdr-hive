"""Unittests for hive.git_ops (spec §6.2, §6.3, §6.5). Each test builds a temp repo."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from hive import git_ops
from hive.git_ops import GitError, add_info_exclude, branch_delete, branch_exists
from hive.git_ops import copy_worktreeinclude, current_branch, has_uncommitted
from hive.git_ops import is_ancestor, is_worker_worktree, merge_no_ff, primary_checkout
from hive.git_ops import push, repo_root, worktree_add, worktree_list, worktree_remove


def run_git(cwd, *args) -> str:
    cmd = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args]
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


class GitOpsTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="hive-gitops-")).resolve()  # git reports resolved (/private/var...) paths
        self.repo = self.base / "repo"
        run_git(self.base, "init", "-b", "main", "repo")
        (self.repo / "a.txt").write_text("one\n")
        run_git(self.repo, "add", "a.txt")
        run_git(self.repo, "commit", "-m", "init")

    def worker_worktree(self, name="w1", branch="hive/feat/w1"):
        wt = self.base / name
        worktree_add(self.repo, wt, branch, "main")
        return wt

    def test_repo_root_and_primary_checkout(self):
        self.assertEqual(repo_root(self.repo), self.repo)
        self.assertEqual(repo_root(self.worker_worktree()), self.base / "w1")
        self.assertEqual(primary_checkout(self.repo), self.repo)
        self.assertEqual(primary_checkout(self.base / "w1"), self.repo)

    def test_worktree_list(self):
        self.worker_worktree()
        entries = worktree_list(self.repo)
        self.assertEqual(len(entries), 2)
        self.assertEqual({e["path"] for e in entries}, {self.repo, self.base / "w1"})
        for e in entries:
            self.assertIn("head", e)
            self.assertFalse(e.get("bare", False))
        branches = {e["branch"] for e in entries}
        self.assertEqual(branches, {"main", "hive/feat/w1"})

    def test_is_worker_worktree(self):
        wt = self.worker_worktree()
        self.assertTrue(is_worker_worktree(wt))
        self.assertFalse(is_worker_worktree(self.repo))

    def test_branch_exists(self):
        self.worker_worktree()
        self.assertTrue(branch_exists(self.repo, "hive/feat/w1"))
        self.assertFalse(branch_exists(self.repo, "hive/feat/w2"))

    def test_branch_delete_refuses_missing(self):
        with self.assertRaises(GitError):
            branch_delete(self.repo, "no-such-branch")

    def test_worktree_add_remove(self):
        wt = self.worker_worktree()
        self.assertTrue((wt / "a.txt").exists())
        self.assertEqual(current_branch(wt), "hive/feat/w1")
        worktree_remove(self.repo, wt)
        self.assertFalse(wt.exists())
        branch_delete(self.repo, "hive/feat/w1")
        self.assertFalse(branch_exists(self.repo, "hive/feat/w1"))

    def test_merge_success_no_ff(self):
        wt = self.worker_worktree()
        (wt / "b.txt").write_text("worker\n")
        run_git(wt, "add", "b.txt")
        run_git(wt, "commit", "-m", "worker commit")
        worker_tip = run_git(wt, "rev-parse", "HEAD").strip()

        ok, out = merge_no_ff(self.repo, "hive/feat/w1", "merge w1")
        self.assertTrue(ok)
        self.assertEqual((self.repo / "b.txt").read_text(), "worker\n")
        self.assertEqual(run_git(self.repo, "rev-parse", "main^2").strip(), worker_tip)
        self.assertEqual(
            run_git(self.repo, "log", "-1", "--format=%s", "main").strip(), "merge w1"
        )

    def test_merged_into_hive_branch_not_main(self):
        # The hive branch lives in its own worktree; the primary checkout stays on main.
        hive_wt = self.base / "hive-feat"
        run_git(self.repo, "worktree", "add", "-b", "feat", str(hive_wt), "main")
        wt = self.base / "feat-w1"
        run_git(self.repo, "worktree", "add", "-b", "hive/feat/w1", str(wt), "feat")
        (wt / "b.txt").write_text("worker\n")
        run_git(wt, "add", "b.txt")
        run_git(wt, "commit", "-m", "worker commit")
        self.assertFalse(is_ancestor(self.repo, "hive/feat/w1", "feat"))
        ok, _ = merge_no_ff(hive_wt, "hive/feat/w1", "merge w1")
        self.assertTrue(ok)
        self.assertTrue(is_ancestor(self.repo, "hive/feat/w1", "feat"))
        worktree_remove(self.repo, wt)
        with self.assertRaises(GitError):  # -d checks against main, so it refuses
            branch_delete(self.repo, "hive/feat/w1")
        branch_delete(self.repo, "hive/feat/w1", force=True)
        self.assertFalse(branch_exists(self.repo, "hive/feat/w1"))

    def test_merge_conflict_aborts(self):
        wt = self.worker_worktree()
        (self.repo / "a.txt").write_text("brain side\n")
        run_git(self.repo, "commit", "-am", "brain move")
        (wt / "a.txt").write_text("worker side\n")
        run_git(wt, "commit", "-am", "worker move")

        ok, files = merge_no_ff(self.repo, "hive/feat/w1", "merge w1")
        self.assertFalse(ok)
        self.assertIn("a.txt", files)
        self.assertEqual((self.repo / "a.txt").read_text(), "brain side\n")
        r = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            capture_output=True,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(has_uncommitted(self.repo))
        self.assertEqual((wt / "a.txt").read_text(), "worker side\n")

    def test_has_uncommitted(self):
        self.assertFalse(has_uncommitted(self.repo))
        (self.repo / "u.txt").write_text("untracked\n")
        self.assertTrue(has_uncommitted(self.repo))
        (self.repo / "u.txt").unlink()
        (self.repo / "a.txt").write_text("changed\n")
        self.assertTrue(has_uncommitted(self.repo))

    def test_copy_worktreeinclude(self):
        (self.repo / ".gitignore").write_text("secrets.env\nnested/\n")
        (self.repo / ".worktreeinclude").write_text("secrets.env\nnested/\n")
        (self.repo / "secrets.env").write_text("token=123\n")
        (self.repo / "nested").mkdir()
        (self.repo / "nested" / "deep.env").write_text("deep\n")
        (self.repo / "not-copied.txt").write_text("visible\n")
        run_git(self.repo, "add", ".gitignore", ".worktreeinclude")
        run_git(self.repo, "commit", "-m", "include config")

        wt = self.worker_worktree(name="copy-wt", branch="hive/copy/w1")
        copied = copy_worktreeinclude(self.repo, wt)
        self.assertEqual(sorted(copied), ["nested/deep.env", "secrets.env"])
        self.assertEqual((wt / "secrets.env").read_text(), "token=123\n")
        self.assertEqual((wt / "nested" / "deep.env").read_text(), "deep\n")
        self.assertFalse((wt / "not-copied.txt").exists())

    def test_copy_worktreeinclude_without_file(self):
        copied = copy_worktreeinclude(self.repo, self.base / "empty")
        self.assertEqual(copied, [])

    def test_add_info_exclude_idempotent(self):
        exclude = add_info_exclude(self.repo, ".hive/")
        self.assertTrue(exclude.exists())
        first = exclude.read_text()
        self.assertIn(".hive/", first)
        add_info_exclude(self.repo, ".hive/")
        add_info_exclude(self.repo, "secrets.env")
        second = exclude.read_text()
        self.assertEqual(second.count(".hive/"), 1)
        self.assertIn("secrets.env", second)

    def test_push(self):
        bare = self.base / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(bare)],
            capture_output=True, check=True,
        )
        run_git(self.repo, "remote", "add", "origin", str(bare))
        out = push(self.repo, "main")
        self.assertIn("main", out)
        refs = subprocess.run(
            ["git", "-C", str(bare), "for-each-ref"], capture_output=True, text=True
        ).stdout
        self.assertIn("refs/heads/main", refs)


if __name__ == "__main__":
    unittest.main()
