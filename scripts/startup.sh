#!/bin/sh
# Herdr startup hook: repair the CLI link and reconcile stale state (spec Q6).
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -d "$HOME/.local/bin" ]; then
  ln -sfn "$ROOT/bin/hive" "$HOME/.local/bin/hive"
fi
exec "$ROOT/bin/hive" reconcile --quiet
