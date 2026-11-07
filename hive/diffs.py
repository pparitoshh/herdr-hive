"""Pure git diff helpers for the review bench (spec §7). No herdr, no network."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field


def _git(cwd: str, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode not in (0, 1):  # `diff --no-index` returns 1 on differences
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()}")
    return r.stdout


def worker_diff(worker_worktree: str, hive_branch: str, worker_branch: str) -> str:
    """Commits on worker_branch since the merge-base with hive_branch, plus uncommitted
    and untracked changes in the worker worktree, as one unified diff against the merge-base."""
    base = _git(worker_worktree, "merge-base", hive_branch, worker_branch).strip()
    # Diff merge-base against the working tree: committed + staged + unstaged in one go.
    out = _git(worker_worktree, "diff", "--no-color", base)
    untracked = _git(worker_worktree, "ls-files", "--others", "--exclude-standard", "-z")
    for path in filter(None, untracked.split("\0")):
        out += _git(worker_worktree, "diff", "--no-color", "--no-index", "--", "/dev/null", path)
    return out


def total_diff(hive_worktree: str, base_branch: str, hive_branch: str) -> str:
    return _git(hive_worktree, "diff", "--no-color", f"{base_branch}...{hive_branch}")


@dataclass
class FileDiff:
    path: str
    lines: list[tuple[str, int | None, int | None, str]] = field(default_factory=list)

    def location(self, idx: int) -> str | None:
        """`file:line` for line idx (new side; deleted lines give `file:-old`)."""
        kind, old, new, _ = self.lines[idx]
        if kind in ("add", "ctx") and new is not None:
            return f"{self.path}:{new}"
        if kind == "del" and old is not None:
            return f"{self.path}:-{old}"
        return None


_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_unified(diff: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    old = new = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = FileDiff(path=m.group(2) if m else line[11:])
            files.append(cur)
            cur.lines.append(("meta", None, None, line))
            in_hunk = False
            continue
        if cur is None:
            continue
        if line.startswith("+++ ") and not in_hunk:
            p = line[4:]
            if p != "/dev/null":
                cur.path = p[2:] if p.startswith("b/") else p
            cur.lines.append(("meta", None, None, line))
            continue
        if line.startswith("--- ") and not in_hunk:
            p = line[4:]
            if p != "/dev/null" and cur.path == "":
                cur.path = p[2:] if p.startswith("a/") else p
            cur.lines.append(("meta", None, None, line))
            continue
        m = _HUNK.match(line)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            in_hunk = True
            cur.lines.append(("hunk", None, None, line))
        elif in_hunk and line.startswith("+"):
            cur.lines.append(("add", None, new, line[1:]))
            new += 1
        elif in_hunk and line.startswith("-"):
            cur.lines.append(("del", old, None, line[1:]))
            old += 1
        elif in_hunk and line.startswith(" "):
            cur.lines.append(("ctx", old, new, line[1:]))
            old += 1
            new += 1
        elif in_hunk and line == "":
            cur.lines.append(("ctx", old, new, ""))
            old += 1
            new += 1
        else:
            cur.lines.append(("meta", None, None, line))
    return files


def side_by_side_indexed(fd: FileDiff) -> list[tuple[str, str, str, int]]:
    """Like side_by_side, plus the index into fd.lines used for comments (right side preferred)."""
    rows: list[tuple[str, str, str, int]] = []
    dels: list[int] = []
    adds: list[int] = []

    def flush():
        for i in range(max(len(dels), len(adds))):
            d = dels[i] if i < len(dels) else None
            a = adds[i] if i < len(adds) else None
            l = fd.lines[d][3] if d is not None else ""
            r = fd.lines[a][3] if a is not None else ""
            kind = "chg" if d is not None and a is not None else ("del" if d is not None else "add")
            rows.append((l, r, kind, a if a is not None else d))
        dels.clear()
        adds.clear()

    for idx, (kind, _o, _n, text) in enumerate(fd.lines):
        if kind == "del":
            if adds:
                flush()
            dels.append(idx)
        elif kind == "add":
            adds.append(idx)
        else:
            flush()
            rows.append((text, text if kind == "ctx" else "", kind, idx))
    flush()
    return rows


def side_by_side(fd: FileDiff) -> list[tuple[str, str, str]]:
    """Rows (left, right, kind); kind in ctx/add/del/chg/hunk/meta. del+add runs pair as 'chg'."""
    return [(l, r, k) for l, r, k, _ in side_by_side_indexed(fd)]
