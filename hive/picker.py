"""`hive picker`: popup UI for `hive ask` (spec §10).

Request (file at $HIVE_PICKER_REQ):
  {"title": str, "prompt": str, "choices": [str], "allow_text": bool, "default": str|null}
Answer (written atomically to $HIVE_PICKER_OUT):
  {"answer": str|null, "cancelled": bool}
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CANCELLED = {"answer": None, "cancelled": True}


def _norm(req: dict) -> dict:
    return {
        "title": str(req.get("title") or "hive"),
        "prompt": str(req.get("prompt") or ""),
        "choices": [str(c) for c in (req.get("choices") or [])],
        "allow_text": bool(req.get("allow_text", False)),
        "default": req.get("default"),
    }


def write_answer(path: str | Path, ans: dict) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(ans))
    os.replace(tmp, path)


def ask_plain(req: dict, input_fn=input, out=sys.stderr) -> dict:
    """Numbered terminal prompt. Empty input picks the default; q cancels."""
    req = _norm(req)
    print(f"== {req['title']} ==", file=out)
    if req["prompt"]:
        print(req["prompt"], file=out)
    for i, c in enumerate(req["choices"], 1):
        mark = " (default)" if c == req["default"] else ""
        print(f"  {i}. {c}{mark}", file=out)
    hint = "number" if req["choices"] else ""
    if req["allow_text"]:
        hint = (hint + " or text") if hint else "text"
    while True:
        try:
            s = input_fn(f"[{hint}, q=cancel]> ").strip()
        except (EOFError, KeyboardInterrupt):
            return dict(CANCELLED)
        if s.lower() == "q":
            return dict(CANCELLED)
        if not s:
            if req["default"] is not None:
                return {"answer": str(req["default"]), "cancelled": False}
            continue
        if s.isdigit() and 1 <= int(s) <= len(req["choices"]):
            return {"answer": req["choices"][int(s) - 1], "cancelled": False}
        if s in req["choices"] or req["allow_text"]:
            return {"answer": s, "cancelled": False}
        print("invalid choice", file=out)


def build_app(req: dict):
    """Return a Textual App; app.result holds the answer dict after it exits."""
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.widgets import Input, Label, OptionList

    req = _norm(req)

    class PickerApp(App):
        CSS = """
        Screen { padding: 1 2; }
        #title { text-style: bold; }
        OptionList { height: auto; max-height: 20; }
        """
        BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

        def __init__(self):
            super().__init__()
            self.result = dict(CANCELLED)

        def compose(self) -> ComposeResult:
            yield Label(req["title"], id="title")
            if req["prompt"]:
                yield Label(req["prompt"])
            if req["choices"] or not req["allow_text"]:
                yield Input(placeholder="type to filter" + (" or enter a custom value" if req["allow_text"] else ""),
                            id="filter")
                yield OptionList(*req["choices"], id="opts")
            else:
                yield Input(value=str(req["default"] or ""), placeholder="type a value, Enter to accept", id="text")
            yield Label("Enter: choose   Esc: cancel   arrows: move", id="help")

        def on_mount(self) -> None:
            if req["choices"]:
                opts = self.query_one("#opts", OptionList)
                if req["default"] in req["choices"]:
                    opts.highlighted = req["choices"].index(req["default"])
                elif req["choices"]:
                    opts.highlighted = 0
            self.query_one(Input).focus()

        def _visible(self) -> list[str]:
            f = self.query_one("#filter", Input).value.lower()
            return [c for c in req["choices"] if f in c.lower()]

        def on_input_changed(self, ev: Input.Changed) -> None:
            if ev.input.id != "filter":
                return
            opts = self.query_one("#opts", OptionList)
            opts.clear_options()
            vis = self._visible()
            opts.add_options(vis)
            if vis:
                opts.highlighted = 0

        def on_key(self, ev) -> None:
            if ev.key == "q" and not isinstance(self.focused, Input):
                ev.stop()
                self.action_cancel()
            if ev.key in ("up", "down") and isinstance(self.focused, Input) and self.focused.id == "filter":
                opts = self.query_one("#opts", OptionList)
                ev.stop()
                ev.prevent_default()
                if ev.key == "down":
                    opts.action_cursor_down()
                else:
                    opts.action_cursor_up()

        def _finish(self, answer: str) -> None:
            self.result = {"answer": answer, "cancelled": False}
            self.exit(self.result)

        def on_input_submitted(self, ev: Input.Submitted) -> None:
            val = ev.value.strip()
            if ev.input.id == "text":
                if val:
                    self._finish(val)
                return
            opts = self.query_one("#opts", OptionList)
            vis = self._visible()
            if vis and opts.highlighted is not None and opts.highlighted < len(vis):
                self._finish(vis[opts.highlighted])
            elif val and req["allow_text"]:
                self._finish(val)
            elif not val and req["default"] is not None:
                self._finish(str(req["default"]))

        def on_option_list_option_selected(self, ev: OptionList.OptionSelected) -> None:
            self._finish(str(ev.option.prompt))

        def action_cancel(self) -> None:
            self.result = dict(CANCELLED)
            self.exit(self.result)

    return PickerApp()


def ask_inline(req: dict) -> dict:
    """Run the picker in the current terminal and return the answer dict."""
    try:
        app = build_app(req)
    except ImportError:
        return ask_plain(req)
    app.run()
    return app.result


def main(argv: list[str]) -> int:
    req_path = os.environ.get("HIVE_PICKER_REQ")
    out_path = os.environ.get("HIVE_PICKER_OUT")
    if not req_path or not out_path:
        print("hive picker: HIVE_PICKER_REQ and HIVE_PICKER_OUT must be set", file=sys.stderr)
        return 2
    try:
        req = json.loads(Path(req_path).read_text())
    except (OSError, ValueError) as e:
        print(f"hive picker: bad request: {e}", file=sys.stderr)
        write_answer(out_path, dict(CANCELLED))
        return 2
    try:
        ans = ask_inline(req)
    except Exception:
        ans = dict(CANCELLED)
        write_answer(out_path, ans)
        raise
    write_answer(out_path, ans)
    return 0 if not ans["cancelled"] else 1
