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


def gains_to_try(current: float) -> list[float]:
    return sorted({current, 1.0, 2.0, 3.0, 4.0})


def suggest_gain(wakes: dict[float, int], current: float) -> float | None:
    """Smallest gain that caught the most wake words (a lower gain means fewer false wakes)."""
    best = max(wakes.values(), default=0)
    if best == 0:
        return None
    return min(g for g, n in wakes.items() if n == best) if wakes.get(current, 0) < best else current


def run_mic_test(config: dict, seconds: float = 30.0) -> int:
    import sounddevice as sd

    device = resolve_input_device(config["audio"].get("input_device"))
    info = sd.query_devices(device if device is not None else sd.default.device[0])
    print("Input devices (* = Windows default):")
    list_input_devices()
    print(f"\nListening on: {info['name']}")

    ww = config["wake_word"]
    detectors = {}
    if config["features"].get("wake_word"):
        from core.wakeword import WakeWordDetector

        current = float(ww.get("gain", 1.0))
        detectors = {g: WakeWordDetector(ww["model"], ww["threshold"], g) for g in gains_to_try(current)}
        print(f'Say "hey jarvis" about 5 times in your NORMAL voice, a few seconds apart. {seconds:.0f} seconds...')
        print(f"Scores are shown at each gain; {ww['threshold']} or more wakes JARVIS.\n")
    else:
        print(f"Speak normally. {seconds:.0f} seconds...\n")

    wakes = dict.fromkeys(detectors, 0)
    best = dict.fromkeys(detectors, 0.0)
    window_score = dict.fromkeys(detectors, 0.0)
    loudest, window = 0, []
    started = last_print = time.monotonic()
    with sd.InputStream(samplerate=INPUT_RATE, channels=1, dtype="int16", blocksize=MIC_BLOCK, device=device) as stream:
        while time.monotonic() - started < seconds:
            block, _ = stream.read(MIC_BLOCK)
            pcm = block[:, 0]
            window.append(int(np.abs(pcm.astype(np.int32)).max()))
            for g, det in detectors.items():
                score = det.score(pcm)
                window_score[g] = max(window_score[g], score)
                best[g] = max(best[g], score)
                if score >= det.threshold:
                    wakes[g] += 1
                    det._model.reset()  # same as a real wake: one count per "hey jarvis"
            if time.monotonic() - last_print >= 0.5:
                peak = max(window)
                loudest = max(loudest, peak)
                bar = "#" * min(30, peak * 30 // 8000)
                cols = "  ".join(f"x{g:g} {window_score[g]:.2f}" + ("*" if window_score[g] >= ww["threshold"] else " ")
                                 for g in detectors)
                print(f"  level {peak:>5} |{bar:<30}| {cols}", flush=True)
                window, window_score, last_print = [], dict.fromkeys(detectors, 0.0), time.monotonic()

    print(f"\nLoudest: {loudest} (normal speech is usually 2000+).")
    if loudest < 300:
        print("Almost no sound. Check the mic isn't muted (keyboard mic key, Windows Sound settings > Input),")
        print("or set audio.input_device in config.yaml to another device from the list above.")
        return 0
    if not detectors:
        return 0
    print("Times JARVIS would have woken:")
    for g in detectors:
        mark = "  (current)" if g == current else ""
        print(f"  gain {g:g}: {wakes[g]}   best score {best[g]:.2f}{mark}")
    pick = suggest_gain(wakes, current)
    if pick is None:
        print("It never woke, even boosted. Try moving closer to the mic, or lower wake_word.threshold (e.g. 0.2).")
    elif pick == current:
        print(f"Your current gain ({current:g}) already works best. No change needed.")
    else:
        print(f"Suggestion: set wake_word.gain: {pick:g} in config.yaml.")
    return 0
