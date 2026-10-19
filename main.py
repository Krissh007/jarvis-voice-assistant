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
    parser.add_argument("--bench-apps", action="store_true",
                        help="measure how fast JARVIS reads each open app (read-only, shows no app text), then exit")
    parser.add_argument("--browser-login", action="store_true",
                        help="open JARVIS's Chrome profile normally so you can sign in to YouTube once")
    parser.add_argument("--route", metavar="REQUEST",
                        help="show which route and tools the reasoning layer would pick for a request, then exit")
    parser.add_argument("--setup-memory", action="store_true",
                        help="download the small on-device model that lets memory search by meaning (~90 MB), "
                             "then exit")
    parser.add_argument("--ask", metavar="REQUEST",
                        help="run one request through the reasoning layer (ask_brain) in this terminal; it never "
                             "says yes to a confirmation, it only shows what it would ask")
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


def show_route(config: dict, request: str) -> int:
    """--route: run the intent router once (Agent Hub registry if it answers, else JARVIS's own skills)."""
    import asyncio
    import json

    from actions import Services, load_actions
    from core.registry import Registry
    from core.router import Router, as_dict

    load_dotenv(ROOT / ".env")
    services = Services()
    registry = load_actions(Registry(), config, services)
    router = Router(registry, hub=services.hub, log_path=ROOT / "logs" / "routing.jsonl")
    decision = asyncio.run(router.route(request, source="cli"))
    print(json.dumps(as_dict(decision), indent=2))
    return 0


def setup_memory(config: dict) -> int:
    """--setup-memory: fetch the pinned embedding model into data/models, check it, embed saved facts."""
    import time

    from actions import Services
    from actions.memory import MemoryStore
    from core.embed import Embedder, download, model_dir

    data = Services().data_dir
    folder = model_dir(data)
    print(f"Memory model (all-MiniLM-L6-v2, runs on this PC) -> {folder}")
    try:
        download(folder)
    except Exception as exc:
        print(f"Couldn't get the model: {exc}")
        return 1
    embedder = Embedder(folder)
    started = time.monotonic()
    a, b, c = embedder.encode(["When is my wife's birthday?", "Priya was born on 3 March.", "I parked on level 3."])
    print(f"Loaded and checked in {time.monotonic() - started:.1f}s "
          f"(similar meanings score {float(a @ b):.2f}, unrelated {float(a @ c):.2f}).")
    store = MemoryStore(data / "memory.sqlite", legacy_json=data / "memory.json", embedder=embedder)
    print(f"Embedded {store.backfill()} saved facts. Memory now searches by meaning.")
    return 0


def show_ask(config: dict, request: str) -> int:
    """--ask: one ask_brain run with the real tools and models, no voice. Confirmations are shown, never given."""
    import asyncio
    import time

    from google import genai

    from actions import Services, load_actions
    from core import catalog
    from core.audio import PlaybackState
    from core.confirm import Confirmer, UtteranceTracker
    from core.dispatcher import AuditLog, Dispatcher
    from core.llm import LLM
    from core.registry import NeedsConfirmation, Registry
    from core.secrets import env_key
    from core.untrusted import unwrap

    load_dotenv(ROOT / ".env")
    if not config["features"].get("brain"):
        print("features.brain is off in config.yaml.")
        return 1
    models = config["models"]
    key = env_key("GOOGLE_API_KEY")
    llm = LLM(genai.Client(api_key=key), [models.get("flash"), *(models.get("flash_fallbacks") or [])],
              thinking=models.get("thinking")) if key else None
    services = Services(llm=llm)
    registry = load_actions(Registry(), config, services)
    playback = PlaybackState(tail_s=0.4)
    services.dispatcher = Dispatcher(registry, Confirmer(playback, timeout_s=30), UtteranceTracker(playback),
                                     AuditLog(ROOT / "logs" / "actions.jsonl"), notify=lambda text: None,
                                     accept_phrases=[])
    print("Groq:", "ready" if services.brain.groq.key else "no GROQ_API_KEY (falls back to Gemini Flash)")

    async def run():
        if services.hub is not None:  # the Hub ranks JARVIS's skills too, as the running JARVIS's poller arranges
            try:
                await services.hub.sync_tools(catalog.export(registry))
            except Exception as exc:  # an older Hub or none running: routing falls back to JARVIS's own skills
                print(f"(Agent Hub registry not updated: {type(exc).__name__})")
        start = time.monotonic()
        out = await services.brain.ask(request)
        if isinstance(out, NeedsConfirmation):
            print(f"\nIt would ask you: {out.summary}\n(Not confirmed: --ask never says yes.)")
            if out.on_cancel:
                await out.on_cancel()
        else:
            print(f"\n{unwrap(out)}")
        print(f"\n({time.monotonic() - start:.1f} s; details in logs/brain.jsonl and logs/routing.jsonl)")

    asyncio.run(run())
    return 0


def main(argv: list[str] | None = None) -> int:
    from core.app_mode import (
        claim_single_instance,
        create_show_event,
        is_windowless,
        redirect_console,
        request_show,
        tell_user,
    )

    from core.taskbar import set_process_app_id

    set_process_app_id()  # before any window: groups JARVIS's window under its pinned taskbar icon
    windowless = is_windowless()  # started from the desktop icon (pythonw): no terminal
    if windowless:
        redirect_console(ROOT / "logs")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # Hindi etc.
    args = parse_args(argv)
    setup_logging()
    config = load_config()
    from core.settings_store import apply_overlay

    apply_overlay(config, ROOT / "data" / "settings.json")  # changes made on the app's Settings page
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
    if args.bench_apps:
        from core.desktop_bench import run_benchmark

        return run_benchmark(config)
    if args.browser_login:
        return browser_login()
    if args.route:
        return show_route(config, args.route)
    if args.ask:
        return show_ask(config, args.ask)
    if args.setup_memory:
        return setup_memory(config)
    load_dotenv(ROOT / ".env")
    from core.secrets import env_key

    api_key = env_key("GOOGLE_API_KEY")
    if not api_key:
        tell_user("GOOGLE_API_KEY is missing. Copy .env.example to .env and add your key.", windowless)
        return 1
    if not args.text:
        if not claim_single_instance():  # waits a few seconds if the running JARVIS is quitting
            if request_show():
                print("JARVIS was already running: brought its window to the front.", flush=True)
                return 0
            tell_user("JARVIS is already running. Look for its window at the bottom-right of the screen, "
                      "or its icon by the clock.", windowless, warning=False)
            return 1
        create_show_event()
        try:
            from core.shortcut import desktop_shortcut
            from core.taskbar import tag_shortcut

            tag_shortcut(desktop_shortcut(), ROOT)  # so a pin made from it groups with the window
        except Exception:
            logging.getLogger("jarvis").exception("couldn't tag the desktop shortcut")
    if windowless:
        # Without a terminal, the window or the tray is the only way to reach (and quit) JARVIS.
        if not (config["features"].get("window") or config["features"].get("web_ui") or config["features"].get("tray")):
            config["features"]["tray"] = True

    from google import genai

    from core.runtime import Assistant, Options

    assistant = Assistant(config, genai.Client(api_key=api_key),
                          Options(text_mode=args.text, dry_run=args.dry_run, early_window=True))
    quiet_closed_pipe_noise()
    import time as _time

    try:
        asyncio.run(assistant.run())
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.getLogger("jarvis").exception("JARVIS stopped because of an error")
        tell_user(f"JARVIS stopped because of an error. Details are in {ROOT / 'logs' / 'app.log'}.", windowless)
        return 1
    finally:
        _exit_started = _time.monotonic()
        import atexit

        atexit.register(lambda: logging.getLogger("jarvis").info(
            "process exit %.2fs after the event loop ended", _time.monotonic() - _exit_started))
    print("JARVIS offline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
