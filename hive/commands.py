"""Subcommands beyond `status`. Each milestone adds its commands here."""
from __future__ import annotations


def register(sub) -> None:
    from . import config, state

    def cmd_config(args):
        path = config.ensure_global()
        cfg = config.load()
        print(f"config: {path}")
        print(f"state:  {state.STATE_DIR / 'hive.db'}")
        for h in cfg.harnesses.values():
            print(f"  {h.name:<10} {h.label:<14} models: {', '.join(h.models)}")
        return 0

    s = sub.add_parser("config", help="show the merged config")
    s.set_defaults(func=cmd_config)
