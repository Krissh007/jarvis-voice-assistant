"""JARVIS entry point: loads config, starts session + hotkeys + scheduler.

Flags:
    --dry-run  log tool calls instead of performing side effects
    --text     keyboard chat instead of voice (for testing)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jarvis", description="JARVIS desktop voice assistant")
    parser.add_argument("--dry-run", action="store_true", help="log tool calls instead of executing side effects")
    parser.add_argument("--text", action="store_true", help="type instead of speak")
    return parser.parse_args(argv)


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(log_dir: Path = ROOT / "logs") -> None:
    log_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(log_dir / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)
    setup_logging()
    config = load_config()
    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("GOOGLE_API_KEY is missing. Copy .env.example to .env and add your key.")
        return 1

    from google import genai

    from core.runtime import Assistant, Options

    assistant = Assistant(config, genai.Client(api_key=api_key), Options(text_mode=args.text, dry_run=args.dry_run))
    try:
        asyncio.run(assistant.run())
    except KeyboardInterrupt:
        pass
    print("JARVIS offline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
