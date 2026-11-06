#!/bin/sh
# Make the plugin venv (Textual) and link `hive` into ~/.local/bin.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${HIVE_STATE_DIR:-$HOME/.local/state/herdr-hive}"
VENV="$STATE/venv"
mkdir -p "$STATE"
if [ ! -x "$VENV/bin/python" ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv -q "$VENV"
  else
    python3 -m venv "$VENV"
  fi
fi
if ! "$VENV/bin/python" -c "import textual" 2>/dev/null; then
  if command -v uv >/dev/null 2>&1; then
    uv pip install -q --python "$VENV/bin/python" "textual>=0.80" || echo "hive: textual install failed; bench and picker fall back to plain prompts" >&2
  else
    "$VENV/bin/pip" install -q "textual>=0.80" || echo "hive: textual install failed; bench and picker fall back to plain prompts" >&2
  fi
fi
chmod +x "$ROOT/bin/hive"
if [ -d "$HOME/.local/bin" ]; then
  ln -sfn "$ROOT/bin/hive" "$HOME/.local/bin/hive"
fi
echo "hive: setup done (venv: $VENV)"
