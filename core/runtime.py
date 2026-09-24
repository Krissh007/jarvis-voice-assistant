"""Wires audio, hotkeys, wake word and the Live session into one running assistant."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from dataclasses import dataclass

from core.audio import Mic, MicGate, PlaybackState, Player, chime
from core.hotkeys import Hotkeys
from core.prompts import build_system_instruction
from core.session import LiveSession, build_live_config
from core.wakeword import WakeState

log = logging.getLogger(__name__)


@dataclass
class Options:
    text_mode: bool = False
    dry_run: bool = False


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
    """The running voice assistant (Phase 1: conversation only, no tools)."""

    def __init__(self, config: dict, client, options: Options) -> None:
        self.config = config
        self.options = options
        mode = config["listening"]["mode"]
        self.mode = "text" if options.text_mode else mode
        audio_cfg = config["audio"]

        self.playback = PlaybackState(tail_s=audio_cfg["echo_tail_ms"] / 1000)
        self.gate = MicGate(
            playback=self.playback,
            push_to_talk=self.mode == "push_to_talk",
            mute_while_speaking=audio_cfg["mute_mic_while_speaking"],
        )
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

        instruction = build_system_instruction(config["user"].get("name"))
        self.session = LiveSession(
            client,
            config["models"]["live"],
            lambda handle: build_live_config(
                system_instruction=instruction,
                voice=config["voice"].get("name"),
                push_to_talk=self.gate.push_to_talk,
                resume_handle=handle,
            ),
            self,
        )
        self.hotkeys = Hotkeys(
            {k: v for k, v in config["hotkeys"].items() if k in {"mute", "push_to_talk", "stop"}},
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
            self._mic = Mic(self._loop, self._on_mic_chunk)
            self._mic.start()
        if self.wake:
            tasks.append(asyncio.create_task(self._wake_watchdog(), name="wake"))
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
        print(f"JARVIS online ({self.mode} mode). Ctrl+C to quit.", flush=True)
        print(f"  mute: {keys['mute']}   push-to-talk: {keys['push_to_talk']}   stop talking: {keys['stop']}")
        if self.mode == "always_on" and not self.gate.mute_while_speaking:
            print("  Tip: use headphones, or JARVIS may hear itself through the speakers and interrupt itself.")
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
                    self._loop.call_soon_threadsafe(self.session.send_text, line.strip())

        threading.Thread(target=reader, name="stdin", daemon=True).start()

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
            self.player.clear()

    async def _wake_watchdog(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            if self.wake.check_sleep(self.playback.playing):
                self.transcript.flush()
                print("(sleeping)", flush=True)

    # ---- SessionEvents ----

    def on_audio(self, pcm: bytes) -> None:
        if self.wake:
            self.wake.touch()
        self.player.enqueue(pcm)

    def on_interrupted(self) -> None:
        self.player.clear()

    def on_input_transcript(self, text: str) -> None:
        if self.wake:
            self.wake.touch()
        self.transcript.add("user", text)

    def on_output_transcript(self, text: str) -> None:
        self.transcript.add("jarvis", text)

    def on_turn_complete(self) -> None:
        if self.wake:
            self.wake.touch()
        self.transcript.flush()

    def on_status(self, message: str, *, problem: bool = False) -> None:
        print(f"[{'!' if problem else 'i'}] {message}", flush=True)
        if problem:
            self.player.enqueue(chime(440, 0.2) + chime(330, 0.3))
