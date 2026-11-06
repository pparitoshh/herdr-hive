"""Subprocess wrappers around git for worktrees, branches, merges and pushes (spec §6.2–§6.5).

Stdlib only. Every function takes an explicit path, so the caller states which
repo or worktree the command runs in.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


WORKER_BRANCH = re.compile(r"\Ahive/[^/]+/w\d+\Z")


class GitError(RuntimeError):
    """A git command failed. The message carries the stderr text."""

    def __init__(self, args: list[str], stderr: str):
        super().__init__(f"git {' '.join(args)} failed: {stderr.strip()}")


def _run(cwd, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise GitError(list(args), r.stderr)
    return r


def _resolve_relative(path, out: str) -> Path:
    p = Path(out)
    if p.is_absolute():
        return p
    return (Path(path) / out).resolve()


def repo_root(path) -> Path:
    """The top level of the worktree that contains `path`."""
    return Path(_run(path, "rev-parse", "--show-toplevel").stdout.strip())


def common_dir(path) -> Path:
    """The shared .git directory of the repository that contains `path`."""
    out = _run(path, "rev-parse", "--git-common-dir").stdout.strip()
    return _resolve_relative(path, out)


def current_branch(path) -> str:
    """The checked out branch of the worktree at `path`, or "" when detached."""
    return _run(path, "branch", "--show-current").stdout.strip()


def is_worker_worktree(path) -> bool:
    """True when the worktree at `path` sits on a `hive/<x>/w<N>` branch."""
    try:
        return bool(WORKER_BRANCH.match(current_branch(path)))
    except GitError:
        return False


def branch_exists(repo, branch: str) -> bool:
    r = _run(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return r.returncode == 0


def worktree_list(repo) -> list[dict]:
    """Every worktree as a dict with keys path, head, branch, bare, detached."""
    text = _run(repo, "worktree", "list", "--porcelain").stdout
    entries: list[dict] = []
    entry: dict = {}
    for line in text.splitlines():
        if not line.strip():
            if entry:
                entries.append(entry)
                entry = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            entry = {"path": Path(value)}
        elif key in ("bare", "detached"):
            entry[key] = True
        elif key == "branch":
            entry["branch"] = value.removeprefix("refs/heads/")
        else:
            entry[key.lower()] = value
    if entry:
        entries.append(entry)
    return entries


def primary_checkout(repo) -> Path:
    """The first entry of `git worktree list --porcelain`, the main checkout."""
    entries = worktree_list(repo)
    if not entries:
        raise GitError(["worktree", "list"], "no worktrees found")
    return entries[0]["path"]


def worktree_add(repo, path, new_branch: str, start_ref: str | None = None) -> Path:
    """Add the worktree at `path` on the new branch `new_branch`, built on `start_ref`."""
    args = ["worktree", "add", str(Path(path)), "-b", new_branch]
    if start_ref is not None:
        args.append(start_ref)
    _run(repo, *args)
    return Path(path)


def worktree_remove(repo, path, force: bool = False) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(Path(path)))
    _run(repo, *args)


def branch_delete(repo, branch: str, force: bool = False) -> None:
    _run(repo, "branch", "-D" if force else "-d", branch)


def merge_no_ff(worktree, branch: str, message: str) -> tuple[bool, str]:
    """Merge `branch` into the worktree at `worktree` with a merge commit.

    On conflict the merge is aborted. The return is then (False, text) where
    text lists the conflicted files. On success the return is (True, stdout).
    """
    args = ["merge", "--no-ff", "-m", message, branch]
    r = _run(worktree, *args, check=False)
    if r.returncode == 0:
        return True, r.stdout
    conflicted = _run(worktree, "diff", "--name-only", "--diff-filter=U", check=False).stdout
    files = [line.strip() for line in conflicted.splitlines() if line.strip()]
    if not files:
        raise GitError(args, r.stderr)
    _run(worktree, "merge", "--abort")
    return False, "\n".join(files)


def has_uncommitted(path) -> bool:
    """True when the worktree at `path` has any uncommitted or untracked change."""
    return bool(_run(path, "status", "--porcelain").stdout.strip())


def copy_worktreeinclude(src_checkout, dst) -> list[str]:
    """Copy ignored files from `src_checkout` to `dst` per src/.worktreeinclude (spec §4.3).

    Patterns use gitignore syntax. Only ignored files that exist in
    `src_checkout` are copied, with their relative paths kept.
    """
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    if not (Path(src_checkout) / ".worktreeinclude").is_file():
        return []
    r = _run(
        src_checkout,
        "ls-files", "--others", "--ignored",
        "--exclude-from=.worktreeinclude", "-z",
    )
    copied: list[str] = []
    for rel in filter(None, r.stdout.split("\0")):
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(src_checkout) / rel, target)
        copied.append(rel)
    return copied


def add_info_exclude(worktree, pattern: str) -> Path:
    """Append `pattern` to `git rev-parse --git-path info/exclude` when not present."""
    out = _run(worktree, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    file = _resolve_relative(worktree, out)
    existing = file.read_text(encoding="utf-8") if file.exists() else ""
    if pattern in existing.splitlines():
        return file
    if existing and not existing.endswith("\n"):
        existing += "\n"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(existing + pattern + "\n", encoding="utf-8")
    return file


def push(worktree, branch: str) -> str:
    """Push `branch` to origin and set upstream. Returns the combined git output."""
    r = _run(worktree, "push", "-u", "origin", branch)
    return (r.stdout + r.stderr).strip()


def local_branches(repo) -> list[str]:
    """Local branch names, newest commit first, without hive worker branches."""
    out = _run(repo, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)", "refs/heads").stdout
    return [b for b in out.split() if not b.startswith("hive/")]
