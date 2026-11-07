#!/bin/sh
# Herdr startup hook: repair the CLI link, remove a stale router socket,
# reconcile stale state (spec Q6, plan §Router item 5, §Plugin integration).
# The startup hook must not start the router server; the first `hive spawn` does.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -d "$HOME/.local/bin" ]; then
  ln -sfn "$ROOT/bin/hive" "$HOME/.local/bin/hive"
fi
STATE="${HIVE_STATE_DIR:-$HOME/.local/state/herdr-hive}"
SOCK="$STATE/router.sock"
if [ -S "$SOCK" ]; then
  PY="python3"
  if [ -x "$STATE/venv/bin/python" ]; then PY="$STATE/venv/bin/python"; fi
  "$PY" -c 'import os, socket, sys
path = sys.argv[1]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:  # a listener accepts the connect, so the socket is live and stays
    s.connect(path)
    s.close()
except OSError:  # nothing listens: the old server left a stale socket
    try: os.unlink(path)
    except OSError: pass' "$SOCK" 2>/dev/null || true
fi
exec "$ROOT/bin/hive" reconcile --quiet
