"""Review bench (spec §7): browse worker diffs, comment, approve/reject, AI review."""
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Label, OptionList, Select, Static

from .. import config, diffs, radio_ops, state

# Injectable seams (tests monkeypatch these module attributes).
radio_send = radio_ops.send
load_worker_diff = diffs.worker_diff
load_total_diff = diffs.total_diff
run_subprocess = subprocess.run
load_config = config.load

TOTAL = "total"
HIDDEN_STATUSES = ("merged", "discarded")
INLINE_DIFF_LIMIT = 20000


class TextModal(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = "TextModal { align: center middle; } TextModal > Vertical { width: 80; height: auto; border: round $accent; padding: 1; background: $surface; }"

    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.prompt)
            yield Input(id="modal-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        self.dismiss(ev.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChoiceModal(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = "ChoiceModal { align: center middle; } ChoiceModal > Vertical { width: 60; height: auto; border: round $accent; padding: 1; background: $surface; }"

    def __init__(self, prompt: str, choices: list[str]):
        super().__init__()
        self.prompt = prompt
        self.choices = choices

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.prompt)
            yield OptionList(*self.choices)

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, ev: OptionList.OptionSelected) -> None:
        self.dismiss(self.choices[ev.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)


class BenchApp(App):
    TITLE = "hive bench"
    CSS = """
    #files { width: 32; }
    #diff { width: 1fr; }
    #ai { width: 50; display: none; border-left: solid $accent; }
    #ai.shown { display: block; }
    #status { height: 1; background: $boost; }
    """
    BINDINGS = [
        Binding("t", "toggle_view", "Unified/SxS"),
        Binding("c", "comment", "Comment"),
        Binding("a", "approve", "Approve"),
        Binding("r", "reject", "Reject"),
        Binding("i", "ai_review", "AI review"),
        Binding("f", "forward", "Forward AI"),
        Binding("o", "open_editor", "Open"),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, hive_name: str, conn=None):
        super().__init__()
        self.conn = conn or state.connect()
        self.hive = state.hive_by_name(self.conn, hive_name)
        if self.hive is None:
            raise SystemExit(f"hive bench: no open hive {hive_name!r}")
        self.hive_name = hive_name
        self.entry: str | None = None  # first refresh picks the first worker
        self.files: list[diffs.FileDiff] = []
        self.file_path: str | None = None
        self.side = False
        self.row_map: list[int] = []  # diff table row -> index into FileDiff.lines
        self.ai_findings: str | None = None
        self.ai_entry: str | None = None
        self._entries: list[tuple[str, str]] = []

    # ---------- layout ----------
    def compose(self) -> ComposeResult:
        yield Select([("Workspace total", TOTAL)], id="entry", allow_blank=False, value=TOTAL)
        with Horizontal():
            yield DataTable(id="files", cursor_type="row")
            yield DataTable(id="diff", cursor_type="row")
            with VerticalScroll(id="ai"):
                yield Static("", id="ai-text")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#files", DataTable).add_column("file")
        self.refresh_all()
        self.set_interval(5, self.refresh_all)

    # ---------- helpers ----------
    @property
    def msgs_dir(self) -> Path:
        return Path(self.hive["worktree"]) / ".hive" / "msgs"

    def set_status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)
        self.last_status = msg

    def _live_workers(self):
        return [w for w in state.workers(self.conn, self.hive["id"]) if w["status"] not in HIDDEN_STATUSES]

    def _worker_row(self, n: int):
        return state.worker(self.conn, self.hive["id"], n)

    def _entry_n(self) -> int | None:
        return int(self.entry[1:]) if self.entry != TOTAL else None

    def _send(self, to: str, type_: str, text: str, **kw) -> bool:
        try:
            radio_send(to, type_, self.hive_name, text, sender=radio_ops.bench_handle(self.hive_name),
                       msgs_dir=self.msgs_dir, workspace=self.hive["workspace_id"], **kw)
            return True
        except Exception as e:  # radio failures must not crash the bench
            self.set_status(f"radio error ({type_} -> {to}): {e}")
            return False

    def current_diff_text(self) -> str:
        if self.entry == TOTAL:
            base = self.hive["base"] or load_config(Path(self.hive["repo"] or ".")).base_branch
            return load_total_diff(self.hive["worktree"], base, self.hive["branch"])
        w = self._worker_row(self._entry_n())
        return load_worker_diff(w["worktree"], self.hive["branch"], w["branch"])

    def current_file(self) -> diffs.FileDiff | None:
        for f in self.files:
            if f.path == self.file_path:
                return f
        return None

    def selected_location(self) -> tuple[str, str] | None:
        """(file, line) for the diff cursor, or None."""
        fd = self.current_file()
        table = self.query_one("#diff", DataTable)
        if fd is None or not self.row_map or table.cursor_row >= len(self.row_map):
            return None
        loc = fd.location(self.row_map[table.cursor_row])
        if loc is None:
            return None
        f, _, line = loc.rpartition(":")
        return f, line

    # ---------- refresh ----------
    def refresh_all(self) -> None:
        sel = self.query_one("#entry", Select)
        entries = [(f"w{w['n']} — {w['title'] or ''} [{w['status']}]", f"w{w['n']}") for w in self._live_workers()]
        entries.append(("Workspace total", TOTAL))
        if entries != self._entries:
            self._entries = entries
            keys = [k for _, k in entries]
            if self.entry not in keys:
                self.entry = keys[0]
            with self.prevent(Select.Changed):
                sel.set_options(entries)
                sel.value = self.entry
        try:
            self.files = diffs.parse_unified(self.current_diff_text())
        except Exception as e:
            self.files = []
            self.set_status(f"diff error: {e}")
        self._fill_files()
        self._fill_diff()

    def _fill_files(self) -> None:
        t = self.query_one("#files", DataTable)
        paths = [f.path for f in self.files]
        if self.file_path not in paths:
            self.file_path = paths[0] if paths else None
        t.clear()
        for p in paths:
            t.add_row(p, key=p)
        if self.file_path:
            t.move_cursor(row=paths.index(self.file_path))

    def _fill_diff(self) -> None:
        t = self.query_one("#diff", DataTable)
        keep = t.cursor_row
        t.clear(columns=True)
        self.row_map = []
        fd = self.current_file()
        if fd is None:
            return
        style = {"add": "green", "del": "red", "hunk": "cyan", "meta": "dim", "chg": "yellow"}
        if self.side:
            t.add_columns("old", "new")
            for l, r, kind, idx in diffs.side_by_side_indexed(fd):
                s = style.get(kind, "")
                t.add_row(f"[{s}]{_esc(l)}[/]" if s else _esc(l), f"[{s}]{_esc(r)}[/]" if s else _esc(r))
                self.row_map.append(idx)
        else:
            t.add_columns("old", "new", "")
            sign = {"add": "+", "del": "-", "ctx": " "}
            for idx, (kind, o, n, text) in enumerate(fd.lines):
                s = style.get(kind, "")
                body = sign.get(kind, "") + _esc(text)
                t.add_row(str(o or ""), str(n or ""), f"[{s}]{body}[/]" if s else body)
                self.row_map.append(idx)
        if self.row_map:
            t.move_cursor(row=min(keep, len(self.row_map) - 1))

    # ---------- events ----------
    def on_select_changed(self, ev: Select.Changed) -> None:
        if ev.value is Select.BLANK or ev.value == self.entry or ev.value != ev.select.value:  # stale event
            return
        self.entry = ev.value
        self.file_path = None
        self.refresh_all()

    def on_data_table_row_highlighted(self, ev: DataTable.RowHighlighted) -> None:
        if ev.data_table.id == "files" and ev.row_key is not None and ev.row_key.value != self.file_path:
            self.file_path = ev.row_key.value
            self.query_one("#diff", DataTable).move_cursor(row=0)
            self._fill_diff()

    # ---------- actions ----------
    def action_refresh(self) -> None:
        self.refresh_all()
        self.set_status("refreshed")

    def action_toggle_view(self) -> None:
        self.side = not self.side
        self._fill_diff()

    def action_comment(self) -> None:
        loc = self.selected_location()
        if loc is None:
            self.set_status("select a diff line first")
            return
        f, line = loc
        self.push_screen(TextModal(f"Comment on {f}:{line}"), lambda text: self._do_comment(f, line, text))

    def _do_comment(self, f: str, line: str, text: str | None) -> None:
        if not text:
            return
        body = f"{f}:{line}\n{text}"
        n = self._entry_n()
        brain = radio_ops.brain_handle(self.hive_name)
        sent = []
        if n is not None:
            to = radio_ops.worker_handle(self.hive_name, n)
            if self._send(to, "review_comment", body, worker=n, file=f, line=line):
                sent.append(to)
        if self._send(brain, "review_comment", body, worker=n, file=f, line=line):
            sent.append(brain)
        if sent:
            self.set_status(f"comment {f}:{line} sent to {', '.join(sent)}")

    def _need_worker(self) -> int | None:
        n = self._entry_n()
        if n is None:
            self.set_status("select a worker (verdicts are per worker)")
        return n

    def action_approve(self) -> None:
        n = self._need_worker()
        if n is None:
            return
        if self._send(radio_ops.brain_handle(self.hive_name), "verdict", f"w{n} approved by human",
                      worker=n, value="approve"):
            self.set_status(f"approved w{n}")

    def action_reject(self) -> None:
        n = self._need_worker()
        if n is None:
            return
        self.push_screen(TextModal(f"Reason to reject w{n}"), lambda reason: self._do_reject(n, reason))

    def _do_reject(self, n: int, reason: str | None) -> None:
        if not reason:
            return
        if self._send(radio_ops.brain_handle(self.hive_name), "verdict", f"w{n} rejected: {reason}",
                      worker=n, value="reject"):
            self.set_status(f"rejected w{n}")

    def action_ai_review(self) -> None:
        try:
            cfg = load_config(Path(self.hive["repo"])) if self.hive["repo"] else load_config()
        except Exception as e:
            self.set_status(f"config error: {e}")
            return
        names = cfg.review_harnesses
        if not names:
            self.set_status("no [review] harnesses configured")
            return

        def picked_harness(name):
            if not name:
                return
            h = cfg.harnesses[name]
            if h.models:
                self.push_screen(ChoiceModal(f"Model for {h.label}", h.models), lambda m: m and self._start_ai(h, m))
            else:
                self._start_ai(h, None)

        self.push_screen(ChoiceModal("AI review harness", names), picked_harness)

    def _start_ai(self, harness, model) -> None:
        try:
            diff = self.current_diff_text()
        except Exception as e:
            self.set_status(f"diff error: {e}")
            return
        if not diff.strip():
            self.set_status("empty diff, nothing to review")
            return
        entry = self.entry
        label = f"worker w{self._entry_n()}" if entry != TOTAL else "the workspace total"
        intro = (f"You are reviewing {label} of hive {self.hive_name}. Find bugs, risky changes and missing tests. "
                 "Reply with a concise list of findings as `file:line - issue`.")
        if len(diff) > INLINE_DIFF_LIMIT:
            fd, path = tempfile.mkstemp(prefix="hive-review-", suffix=".diff")
            with os.fdopen(fd, "w") as fh:
                fh.write(diff)
            prompt = f"{intro}\nThe unified diff is in the file {path}. Read it."
        else:
            prompt = f"{intro}\n\n```diff\n{diff}\n```"
        argv = harness.argv(model, worker=False, extra=list(harness.headless_args) + [prompt])
        cwd = self.hive["worktree"]
        if entry != TOTAL:
            w = self._worker_row(self._entry_n())
            cwd = w["worktree"] or cwd
        self.set_status(f"AI review running ({harness.name} {model or ''})...")
        self._show_ai("running...")

        def work():
            try:
                r = run_subprocess(argv, cwd=cwd, capture_output=True, text=True, timeout=1800)
                out = r.stdout.strip() or r.stderr.strip() or f"(no output, exit {r.returncode})"
                self.call_from_thread(self._ai_done, entry, out, None)
            except Exception as e:
                self.call_from_thread(self._ai_done, entry, None, str(e))

        self.run_worker(work, thread=True, exclusive=True, group="ai")

    def _show_ai(self, text: str) -> None:
        self.query_one("#ai").add_class("shown")
        self.query_one("#ai-text", Static).update(_esc(text))

    def _ai_done(self, entry: str, out: str | None, err: str | None) -> None:
        if err:
            self._show_ai(f"error: {err}")
            self.set_status(f"AI review failed: {err}")
            return
        self.ai_findings, self.ai_entry = out, entry
        self._show_ai(out)
        self.set_status("AI review done; press f to forward to the brain")

    def action_forward(self) -> None:
        if not self.ai_findings:
            self.set_status("no AI findings to forward (press i first)")
            return
        try:
            self.msgs_dir.mkdir(parents=True, exist_ok=True)
            path = self.msgs_dir / f"{int(time.time() * 1000)}-ai_review.md"
            path.write_text(self.ai_findings)
        except OSError as e:
            self.set_status(f"write error: {e}")
            return
        n = int(self.ai_entry[1:]) if self.ai_entry and self.ai_entry != TOTAL else None
        summary = self.ai_findings[:300]
        if self._send(radio_ops.brain_handle(self.hive_name), "ai_review", summary, worker=n, ref=str(path)):
            self.set_status(f"AI findings forwarded to brain ({path.name})")

    def action_open_editor(self) -> None:
        if not self.file_path:
            self.set_status("no file selected")
            return
        root = self.hive["worktree"]
        if self.entry != TOTAL:
            root = self._worker_row(self._entry_n())["worktree"] or root
        args = [os.environ.get("EDITOR", "vi")]
        loc = self.selected_location()
        if loc and not loc[1].startswith("-"):
            args.append(f"+{loc[1]}")
        args.append(str(Path(root) / self.file_path))
        try:
            with self.suspend():
                run_subprocess(args)
        except Exception as e:
            self.set_status(f"editor error: {e}")


def _esc(s: str) -> str:
    return s.replace("[", "\\[")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="hive bench")
    p.add_argument("--hive", help="hive name (default: the hive for this workspace/cwd)")
    a = p.parse_args(argv)
    conn = state.connect()
    name = a.hive
    if not name:
        from ..cli import current_hive
        name = current_hive(conn)["name"]
    BenchApp(name, conn=conn).run()
    return 0
