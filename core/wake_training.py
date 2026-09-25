"""`--train-wake`: teach the wake word your voice (openWakeWord custom verifier).

The stock "hey jarvis" model scores some accents low (0.05-0.45 in this user's mic tests), so it
misses. A custom verifier is a small classifier trained on *your* "hey jarvis" clips versus your
ordinary speech: the stock model proposes candidates and the verifier decides. Recordings and the
verifier stay in data/wake/ on this PC and are never uploaded.

openWakeWord's own `train_custom_verifier` only keeps clips the stock model already scores >= 0.5,
which is exactly what fails for a low-scoring voice, so this uses a lower capture threshold.
"""

from __future__ import annotations

import pickle
import time
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from core.audio import INPUT_RATE, resolve_input_device
from core.wakeword import VERIFIER_GATE, verifier_path

POSITIVE_CLIPS = 8
CLIP_S = 2.0
NEGATIVE_S = 40.0
NEGATIVE_CHUNK_S = 4.0
PAD_S = 1.0  # silence before each clip: the model ignores its first frames after a reset
CAPTURE_THRESHOLD = 0.05  # stock-model score at which a frame of a positive clip counts as the wake word
MIN_LEVEL = 300


def pad(clip: np.ndarray) -> np.ndarray:
    silence = np.zeros(int(PAD_S * INPUT_RATE), dtype=np.int16)
    return np.concatenate([silence, clip.astype(np.int16), silence[: INPUT_RATE // 2]])


def save_wav(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(INPUT_RATE)
        w.writeframes(samples.astype(np.int16).tobytes())


def train_from_clips(positives: list[np.ndarray], negatives: list[np.ndarray], model_name: str, out_path: Path,
                     extract: Callable | None = None, new_model: Callable | None = None) -> dict[str, int]:
    """Fit and save the verifier. Returns counts; raises ValueError if the clips are unusable."""
    from openwakeword.custom_verifier_model import get_reference_clip_features, train_verifier_model

    if new_model is None:
        from openwakeword.model import Model

        def new_model():
            return Model(wakeword_models=[model_name], inference_framework="onnx")

    extract = extract or get_reference_clip_features
    oww = new_model()
    pos_parts, used = [], 0
    for clip in positives:
        oww.reset()
        feats = extract(pad(clip), oww, model_name, threshold=CAPTURE_THRESHOLD, N=5)
        if len(feats):
            pos_parts.append(feats)
            used += 1
    if used < max(3, len(positives) // 2):
        raise ValueError(f"only {used} of {len(positives)} clips sounded like the wake word to the base model")
    neg_parts = []
    for clip in negatives:
        oww.reset()
        feats = extract(pad(clip), oww, model_name, threshold=0.0, N=1)
        if len(feats):
            neg_parts.append(feats)
    if not neg_parts:
        raise ValueError("no usable normal-speech audio")
    pos, neg = np.vstack(pos_parts), np.vstack(neg_parts)
    model = train_verifier_model(np.vstack([pos, neg]), np.array([1] * len(pos) + [0] * len(neg)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        pickle.dump(model, f)
    return {"clips_used": used, "positive_frames": len(pos), "negative_frames": len(neg)}


def evaluate(detector_factory: Callable, positives: list[np.ndarray], negatives: list[np.ndarray],
             chunk: int = 1280) -> tuple[int, int]:
    """(wake words caught, false wakes in normal speech), replaying the recordings through a detector."""
    def wakes(clip: np.ndarray) -> int:
        det = detector_factory()
        audio = pad(clip)
        return sum(det.detect(audio[i:i + chunk].tobytes()) for i in range(0, len(audio) - chunk + 1, chunk))

    return sum(wakes(c) > 0 for c in positives), sum(wakes(c) for c in negatives)


def _record(stream, seconds: float) -> np.ndarray:
    frames, _ = stream.read(int(seconds * INPUT_RATE))
    return frames[:, 0].copy()


def run_wake_training(config: dict, data_dir: Path) -> int:
    import sounddevice as sd

    from core.wakeword import WakeWordDetector

    ww = config["wake_word"]
    model_name = ww["model"]
    device = resolve_input_device(config["audio"].get("input_device"))
    info = sd.query_devices(device if device is not None else sd.default.device[0])
    folder = Path(data_dir) / "wake"
    print(f"Teaching the wake word your voice. Microphone: {info['name']}")
    print("Sit where you normally do and speak at your normal volume. The recordings stay in")
    print(f"{folder} on this PC and are never uploaded.\n")
    input("Press Enter when ready...")

    positives: list[np.ndarray] = []
    with sd.InputStream(samplerate=INPUT_RATE, channels=1, dtype="int16", device=device) as stream:
        n = 0
        while n < POSITIVE_CLIPS:
            print(f"  [{n + 1}/{POSITIVE_CLIPS}] get ready...", end="", flush=True)
            time.sleep(1.0)
            _record(stream, 0.1)  # drop buffered audio
            print('  say "hey jarvis" NOW', flush=True)
            clip = _record(stream, CLIP_S)
            if int(np.abs(clip.astype(np.int32)).max()) < MIN_LEVEL:
                print("     didn't hear anything, let's try that one again")
                continue
            positives.append(clip)
            n += 1
        print(f"\nNow talk normally for {NEGATIVE_S:.0f} seconds: read something aloud or describe your day.")
        print('Anything EXCEPT "jarvis". Short pauses are fine.')
        input("Press Enter to start...")
        _record(stream, 0.1)
        started = time.monotonic()
        chunks = []
        while (elapsed := time.monotonic() - started) < NEGATIVE_S:
            chunks.append(_record(stream, NEGATIVE_CHUNK_S))
            print(f"  {min(elapsed + NEGATIVE_CHUNK_S, NEGATIVE_S):.0f}s", end="\r", flush=True)
    print()

    for i, clip in enumerate(positives):
        save_wav(folder / "positive" / f"hey_jarvis_{i + 1}.wav", clip)
    for i, clip in enumerate(chunks):
        save_wav(folder / "negative" / f"speech_{i + 1}.wav", clip)

    out = verifier_path(data_dir, model_name)
    print("Training...")
    try:
        stats = train_from_clips(positives, chunks, model_name, out)
    except ValueError as exc:
        print(f"Couldn't train: {exc}. Try again closer to the mic, saying it clearly.")
        return 1

    threshold = float(ww.get("verifier_threshold", 0.5))
    caught, false_wakes = evaluate(lambda: WakeWordDetector(model_name, threshold, float(ww.get("gain", 1.0)), out),
                                   positives, chunks)
    print(f"Done: used {stats['clips_used']} of {POSITIVE_CLIPS} clips. Saved {out.name}.")
    print(f"On your recordings: caught {caught}/{len(positives)} wake words, {false_wakes} false wakes in normal speech.")
    print("(Those are the same clips it learned from, so the real test is live.)")
    print("Next: .\\jarvis\\run.bat --mic-test  and say \"hey jarvis\" a few times.")
    print("To go back to the stock model: delete that file, or set wake_word.personal_verifier: false.")
    return 0
