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
    parser.add_argument("--mic-test", action="store_true", help="show live mic level and wake-word score, then exit")
    parser.add_argument("--train-wake", action="store_true",
                        help="record your voice to make the wake word recognise you better, then exit")
    parser.add_argument("--install-shortcut", action="store_true",
                        help="put a JARVIS icon on the desktop that starts JARVIS, then exit")
    parser.add_argument("--mic", metavar="NAME",
                        help='use this microphone (part of its name, e.g. "Nord Buds") instead of audio.input_device')
    parser.add_argument("--browser-login", action="store_true",
                        help="open JARVIS's Chrome profile normally so you can sign in to YouTube once")
    return parser.parse_args(argv)


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(log_dir: Path = ROOT / "logs") -> None:
    log_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(log_dir / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def quiet_closed_pipe_noise() -> None:
    """Ctrl+C reaches every process in the console, including Chrome's automation driver, so its pipes
    are already gone when asyncio garbage-collects them. Hide only that harmless 'closed pipe' noise."""
    default_hook = sys.unraisablehook

    def hook(args) -> None:
        text = f"{args.exc_value}"
        if isinstance(args.exc_value, (ValueError, RuntimeError)) and (
            "closed pipe" in text or "Event loop is closed" in text
        ) and "Transport" in type(args.object).__qualname__ + repr(getattr(args, "object", "")):
            return
        default_hook(args)

    sys.unraisablehook = hook


def find_chrome() -> str | None:
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                return winreg.QueryValue(key, None)
        except OSError:
            continue
    return None


def browser_login() -> int:
    """One-time: sign in to YouTube inside JARVIS's own Chrome profile (ordinary Chrome, not automated)."""
    import subprocess

    from actions.browser import write_profile_preferences

    chrome = find_chrome()
    if chrome is None:
        print("Google Chrome wasn't found.")
        return 1
    profile = ROOT / "data" / "browser_profile"
    write_profile_preferences(profile)
    print("Opening Chrome with JARVIS's own profile (your normal Chrome profile is not touched).")
    print("  1. Sign in with your own Google account and pick YOUR YouTube profile.")
    print('  2. If Chrome offers to "Turn on sync" or save a password: choose No thanks.')
    print("  3. Close that Chrome window when you're done. JARVIS reuses the sign-in from then on.")
    subprocess.run([chrome, f"--user-data-dir={profile}", "--no-first-run",
                    "https://accounts.google.com/ServiceLogin?continue=https://www.youtube.com/"])
    print("Done. The sign-in is saved in data/browser_profile (never committed to git).")
    return 0


def main(argv: list[str] | None = None) -> int:
    from core.app_mode import claim_single_instance, is_windowless, redirect_console, tell_user

    windowless = is_windowless()  # started from the desktop icon (pythonw): no terminal
    if windowless:
        redirect_console(ROOT / "logs")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # Hindi etc.
    args = parse_args(argv)
    setup_logging()
    config = load_config()
    if args.mic:
        config["audio"]["input_device"] = args.mic
    if args.install_shortcut:
        from core.shortcut import install_shortcut

        return install_shortcut()
    if args.train_wake:
        from actions import Services
        from core.wake_training import run_wake_training

        return run_wake_training(config, Services().data_dir)
    if args.mic_test:
        from core.mictest import run_mic_test

        return run_mic_test(config)
    if args.browser_login:
        return browser_login()
    load_dotenv(ROOT / ".env")
    from core.secrets import env_key

    api_key = env_key("GOOGLE_API_KEY")
    if not api_key:
        tell_user("GOOGLE_API_KEY is missing. Copy .env.example to .env and add your key.", windowless)
        return 1
    if not args.text and not claim_single_instance():
        tell_user("JARVIS is already running. Look for its window at the bottom-right of the screen, "
                  "or its icon by the clock.", windowless, warning=False)
        return 1
    if windowless:
        # Without a terminal, the window or the tray is the only way to reach (and quit) JARVIS.
        if not (config["features"].get("window") or config["features"].get("tray")):
            config["features"]["tray"] = True

    from google import genai

    from core.runtime import Assistant, Options

    assistant = Assistant(config, genai.Client(api_key=api_key), Options(text_mode=args.text, dry_run=args.dry_run))
    quiet_closed_pipe_noise()
    try:
        asyncio.run(assistant.run())
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.getLogger("jarvis").exception("JARVIS stopped because of an error")
        tell_user(f"JARVIS stopped because of an error. Details are in {ROOT / 'logs' / 'app.log'}.", windowless)
        return 1
    print("JARVIS offline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
