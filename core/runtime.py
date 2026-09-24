"""Wires audio, hotkeys, wake word and the Live session into one running assistant."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import os

from actions import Services, load_actions
from core.audio import (
    Mic, MicGate, PlaybackState, Player, chime, default_output_name, resolve_echo_mode, resolve_input_device,
)
from core.confirm import Confirmer, UtteranceTracker
from core.dispatcher import AuditLog, Dispatcher
from core.hotkeys import Hotkeys
from core.llm import LLM
from core.secrets import env_key
from core.prompts import build_system_instruction
from core.registry import Registry, Tier, Tool
from core.session import LiveSession, build_live_config
from core.wakeword import WakeState

log = logging.getLogger(__name__)


@dataclass
class Options:
    text_mode: bool = False
    dry_run: bool = False
    log_dir: Path = Path(__file__).resolve().parent.parent / "logs"


class Transcript:
    """Collects streamed transcript fragments and prints whole lines."""

    def __init__(self, user_label: str) -> None:
        self._labels = {"user": user_label, "jarvis": "JARVIS"}
        self._buf = {"user": "", "jarvis": ""}

    def add(self, who: str, text: str) -> None:
        if who == "jarvis":
            self.flush("user")
        self._buf[who] += text

    def flush(self, who: str | None = None) -> None:
        for w in [who] if who else ["user", "jarvis"]:
            line = " ".join(self._buf[w].split())
            if line:
                print(f"{self._labels[w]}: {line}", flush=True)
                log.info("%s: %s", w, line)
            self._buf[w] = ""


class Assistant:
    """The running voice assistant: audio + session + tools behind the dispatcher."""

    def __init__(self, config: dict, client, options: Options) -> None:
        self.config = config
        self.options = options
        mode = config["listening"]["mode"]
        self.mode = "text" if options.text_mode else mode
        audio_cfg = config["audio"]

        self.playback = PlaybackState(tail_s=audio_cfg["echo_tail_ms"] / 1000)
        self.output_name = default_output_name()
        self.echo_mode = resolve_echo_mode(audio_cfg.get("echo_mode", "auto"), self.output_name)
        # On speakers JARVIS would hear itself and cut itself off, so the mic is ignored while it talks.
        # Push-to-talk doesn't need this: pressing the key stops playback first.
        speakers_gate = self.echo_mode == "speakers" and self.mode in {"always_on", "wake_word"}
        self.gate = MicGate(
            playback=self.playback,
            push_to_talk=self.mode == "push_to_talk",
            mute_while_speaking=audio_cfg["mute_mic_while_speaking"] or speakers_gate,
        )
        self._drop_reply = False  # set by the stop key: discard the rest of the current reply
        self.player = Player(self.playback)
        self.transcript = Transcript(config["user"].get("name") or "You")

        self.wake: WakeState | None = None
        self.detector = None
        if self.mode == "wake_word":
            if not config["features"].get("wake_word"):
                raise SystemExit("listening.mode is wake_word but features.wake_word is false in config.yaml")
            from core.wakeword import WakeWordDetector

            ww = config["wake_word"]
            self.wake = WakeState(active_window_s=ww["active_window_s"])
            self.detector = WakeWordDetector(ww["model"], ww["threshold"])

        models = config["models"]
        llm = None
        if client is not None:
            limits = config.get("limits", {})
            llm = LLM(
                client,
                [models.get("flash"), *(models.get("flash_fallbacks") or [])],
                rpm={models.get("flash"): (limits.get("flash") or {}).get("rpm")},
                openrouter_key=env_key("OPENROUTER_API_KEY"),
                openrouter_model=models.get("openrouter"),
                base_delay_s=0.5,
                thinking=models.get("thinking"),
            )
        self.registry = load_actions(Registry(), config, Services(llm=llm))
        if self.wake:
            self.registry.register(Tool(
                name="go_to_sleep",
                description="Stop listening until the user says the wake word again (e.g. 'go to sleep').",
                func=self._go_to_sleep,
                tier=Tier.SAFE,
                read_only=True,  # changes nothing outside JARVIS
            ))
        confirm_cfg = config["confirm"]
        self.dispatcher = Dispatcher(
            self.registry,
            Confirmer(self.playback, timeout_s=confirm_cfg["timeout_s"]),
            UtteranceTracker(self.playback),
            AuditLog(options.log_dir / "actions.jsonl"),
            notify=self._notify,
            accept_phrases=confirm_cfg["accept_phrases"],
            dry_run=options.dry_run,
        )
        self.gate.confirmation_pending = lambda: self.dispatcher.awaiting_confirmation

        instruction = build_system_instruction(config["user"].get("name"), self.registry.summaries())
        declarations = self.registry.declarations()
        self.session = LiveSession(
            client,
            config["models"]["live"],
            lambda handle: build_live_config(
                system_instruction=instruction,
                voice=config["voice"].get("name"),
                push_to_talk=self.gate.push_to_talk,
                resume_handle=handle,
                tools=declarations,
                languages=audio_cfg.get("transcription_languages"),
                vocabulary=["Jarvis", "hey Jarvis", *confirm_cfg["accept_phrases"]],
            ),
            self,
        )
        self.hotkeys = Hotkeys(
            {k: v for k, v in config["hotkeys"].items() if k in {"mute", "push_to_talk", "stop", "confirm", "cancel"}},
            self._hotkey_from_thread,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mic: Mic | None = None

    # ---- lifecycle ----

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.player.start()
        self.hotkeys.start()
        tasks = [asyncio.create_task(self.session.run(), name="session")]
        if self.mode == "text":
            self._start_stdin_reader()
        else:
            device = resolve_input_device(self.config["audio"].get("input_device"))
            self._mic = Mic(self._loop, self._on_mic_chunk, device)
            self._mic.start()
        tasks.append(asyncio.create_task(self._ticker(), name="ticker"))
        self._print_banner()
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.close()

    def close(self) -> None:
        if self._mic:
            self._mic.stop()
        self.hotkeys.stop()
        self.player.stop()
        self.transcript.flush()

    def _print_banner(self) -> None:
        keys = self.config["hotkeys"]
        dry = " [DRY RUN: actions are only logged]" if self.options.dry_run else ""
        print(f"JARVIS online ({self.mode} mode){dry}. Ctrl+C to quit.", flush=True)
        print(f"  mute: {keys['mute']}   push-to-talk: {keys['push_to_talk']}   stop: {keys['stop']}")
        print(f"  confirm an action: say \"confirm\" or {keys['confirm']}   cancel: {keys['cancel']}")
        print(f"  tools: {', '.join(t.name for t in self.registry.visible()) or 'none'}")
        if self.mode in {"always_on", "wake_word"}:
            if self.gate.mute_while_speaking:
                print(f"  Output: {self.output_name or 'unknown'} -> speaker mode: I stop listening while I talk.")
                print(f"  Interrupt me with {keys['stop']}, or use headphones to interrupt by voice.")
            else:
                print(f"  Output: {self.output_name or 'unknown'} -> headphone mode: interrupt me just by talking.")
        if self.mode == "wake_word":
            print('  Say "hey jarvis" to wake me.')
        if self.mode == "text":
            print("  Type a message and press Enter.")

    # ---- input ----

    def _on_mic_chunk(self, pcm: bytes) -> None:
        if self.wake and not self.wake.awake:
            if self.detector.detect(pcm):
                self.wake.wake()
                self.player.enqueue(chime())
                print("(awake)", flush=True)
            return
        if self.gate.allows():
            self.session.send_audio(pcm)

    def _start_stdin_reader(self) -> None:
        def reader() -> None:
            for line in sys.stdin:
                if line.strip():
                    asyncio.run_coroutine_threadsafe(self._on_typed(line.strip()), self._loop)

        threading.Thread(target=reader, name="stdin", daemon=True).start()

    async def _on_typed(self, text: str) -> None:
        if not await self.dispatcher.on_user_input(text, "text"):
            self.session.send_text(text)

    def _hotkey_from_thread(self, name: str, pressed: bool) -> None:
        self._loop.call_soon_threadsafe(self.on_hotkey, name, pressed)

    def on_hotkey(self, name: str, pressed: bool) -> None:
        if name == "mute" and pressed:
            self.gate.muted = not self.gate.muted
            print("(mic muted)" if self.gate.muted else "(mic live)", flush=True)
        elif name == "push_to_talk" and self.gate.push_to_talk:
            self.gate.ptt_held = pressed
            if pressed:
                self.player.clear()  # barge-in
                self.session.activity_start()
            else:
                self.session.activity_end()
        elif name == "stop" and pressed:
            self._drop_reply = True  # the model keeps streaming this reply; don't play the rest
            self.player.clear()
            asyncio.ensure_future(self.dispatcher.on_hotkey("stop"))
        elif name in {"confirm", "cancel"} and pressed:
            asyncio.ensure_future(self.dispatcher.on_hotkey(name))

    async def _ticker(self) -> None:
        """Housekeeping: confirmation arming/timeouts, quiet utterances, wake-word sleep."""
        while True:
            await asyncio.sleep(0.2)
            try:
                await self.dispatcher.tick()
            except Exception:
                log.exception("dispatcher tick failed")
            if self.wake and self.wake.check_sleep(self.playback.playing or self.dispatcher.awaiting_confirmation):
                self.transcript.flush()
                print("(sleeping)", flush=True)

    async def _go_to_sleep(self) -> str:
        """Rung 2 (app state). Tier SAFE. Ends the wake window; the reply still finishes playing."""
        self.wake.awake = False
        self.transcript.flush()
        print("(sleeping)", flush=True)
        return 'Now asleep. Say a short goodbye; the user wakes you with "hey jarvis".'

    def _notify(self, text: str) -> None:
        log.info("notice: %s", text.splitlines()[0])
        self.session.send_notice(text)

    # ---- SessionEvents ----

    def on_audio(self, pcm: bytes) -> None:
        self.dispatcher.flush_utterance()
        if self.wake:
            self.wake.touch()
        if not self._drop_reply:
            self.player.enqueue(pcm)

    def on_interrupted(self) -> None:
        self.player.clear()

    def on_input_transcript(self, text: str) -> None:
        if self.wake:
            self.wake.touch()
        self.transcript.add("user", text)
        self.dispatcher.on_input_fragment(text)

    def on_output_transcript(self, text: str) -> None:
        self.dispatcher.flush_utterance()
        self.transcript.add("jarvis", text)

    def on_turn_complete(self) -> None:
        self._drop_reply = False
        if self.wake:
            self.wake.touch()
        self.dispatcher.flush_utterance()
        self.dispatcher.on_turn_complete()
        self.transcript.flush()

    async def on_tool_call(self, name: str, args: dict, call_id: str | None) -> dict:
        self.transcript.flush()
        print(f"  [tool] {name}({', '.join(f'{k}={v!r}' for k, v in args.items())})", flush=True)
        result = await self.dispatcher.handle_call(name, args, call_id)
        status = result.get("status")
        if status == "awaiting_confirmation":
            keys = self.config["hotkeys"]
            print(f"  [confirm?] {result['summary']}  (say \"confirm\" / {keys['confirm']}, or {keys['cancel']})", flush=True)
        else:
            print(f"  [tool] -> {status}", flush=True)
        return result

    def on_tool_cancellation(self, ids: list[str]) -> None:
        self.dispatcher.on_tool_cancellation(ids)

    def on_status(self, message: str, *, problem: bool = False) -> None:
        print(f"[{'!' if problem else 'i'}] {message}", flush=True)
        if problem:
            self.player.enqueue(chime(440, 0.2) + chime(330, 0.3))
