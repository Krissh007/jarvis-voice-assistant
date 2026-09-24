"""`--mic-test`: live mic level and wake-word score, to diagnose "it doesn't hear me"."""

from __future__ import annotations

import time

import numpy as np

from core.audio import INPUT_RATE, MIC_BLOCK, resolve_input_device


def list_input_devices() -> None:
    import sounddevice as sd

    default = sd.default.device[0]
    for i, d in enumerate(sd.query_devices()):
        api = sd.query_hostapis(d["hostapi"])["name"]
        if d["max_input_channels"] > 0 and api in {"MME", "Windows WASAPI"}:
            mark = "*" if i == default else " "
            print(f" {mark} {i:>2}  {d['name']}  ({api})")


def run_mic_test(config: dict, seconds: float = 20.0) -> int:
    import sounddevice as sd

    device = resolve_input_device(config["audio"].get("input_device"))
    info = sd.query_devices(device if device is not None else sd.default.device[0])
    print("Input devices (* = Windows default):")
    list_input_devices()
    print(f"\nListening on: {info['name']}")
    print(f'Speak normally and say "hey jarvis" a few times. {seconds:.0f} seconds...\n')

    detector = None
    if config["features"].get("wake_word"):
        from core.wakeword import WakeWordDetector

        ww = config["wake_word"]
        detector = WakeWordDetector(ww["model"], ww["threshold"])
        model = detector._model

    loudest, best_score, window, window_score = 0, 0.0, [], 0.0
    started = last_print = time.monotonic()
    with sd.InputStream(samplerate=INPUT_RATE, channels=1, dtype="int16", blocksize=MIC_BLOCK, device=device) as stream:
        while time.monotonic() - started < seconds:
            block, _ = stream.read(MIC_BLOCK)
            pcm = block[:, 0]
            window.append(int(np.abs(pcm.astype(np.int32)).max()))
            if detector:
                score = max(model.predict(pcm).values(), default=0.0)
                window_score = max(window_score, score)
            if time.monotonic() - last_print >= 0.5:
                peak = max(window)
                loudest, best_score = max(loudest, peak), max(best_score, window_score)
                bar = "#" * min(40, peak * 40 // 8000)
                extra = f"  wake score {window_score:.2f}" + ("  <-- HEARD" if window_score >= detector.threshold else "") if detector else ""
                print(f"  level {peak:>5} |{bar:<40}|{extra}", flush=True)
                window, window_score, last_print = [], 0.0, time.monotonic()

    print(f"\nLoudest: {loudest} (speech is usually 2000+). Best wake score: {best_score:.2f}", end="")
    print(f" (threshold {config['wake_word']['threshold']})" if detector else "")
    if loudest < 300:
        print("Almost no sound. Check the mic isn't muted (keyboard mic key, Windows Sound settings > Input),")
        print("or set audio.input_device in config.yaml to another device from the list above.")
    elif detector and best_score < config["wake_word"]["threshold"]:
        print("The mic works but the wake word wasn't recognised. Try lowering wake_word.threshold (e.g. 0.3).")
    return 0
