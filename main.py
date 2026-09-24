"""JARVIS entry point: loads config, starts session + hotkeys + scheduler.

Flags:
    --dry-run  log tool calls instead of performing side effects
    --text     keyboard chat instead of voice (for testing)
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jarvis", description="JARVIS desktop voice assistant")
    parser.add_argument("--dry-run", action="store_true", help="log tool calls instead of executing side effects")
    parser.add_argument("--text", action="store_true", help="type instead of speak")
    return parser.parse_args(argv)


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config()
    mode = "text" if args.text else config["listening"]["mode"]
    print(f"JARVIS skeleton loaded (mode={mode}, dry_run={args.dry_run}). Voice loop arrives in Phase 1.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
