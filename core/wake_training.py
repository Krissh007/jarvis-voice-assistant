"""`--train-wake`: teach the wake word your voice (openWakeWord custom verifier).

The stock "hey jarvis" model scores some accents low, so it misses. A custom verifier is a small
classifier trained on *your* "hey jarvis" clips versus your ordinary speech; JARVIS asks it about
every chunk of audio (see core.wakeword). Recordings and the verifier stay in data/wake/ on this PC
and are never uploaded.

openWakeWord's own feature capture only learns from the moments the stock model scores 0.05 or
more, so the clips it misses (the ones that matter for a low-scoring voice) teach the verifier
nothing. The "hard clips" method also learns from those, at the stock model's highest-scoring
moment. On the user's first 8 recordings both woke for 7 of 8 unseen clips, so training tries both
on all the recordings and keeps whichever wakes more often on recordings it did *not* learn from.

Each run adds its recordings to the earlier ones, so the voice model keeps improving.
"""

from __future__ import annotations

import pickle
import time
import wave
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np

from core.audio import INPUT_RATE, resolve_input_device
from core.wakeword import STRICT_MARGIN, verifier_path

POSITIVE_CLIPS = 20
CLIP_S = 2.0
NEGATIVE_S = 40.0
NEGATIVE_CHUNK_S = 4.0
PAD_S = 1.0  # silence before each clip: the model ignores its first frames after a reset
CAPTURE_THRESHOLD = 0.05  # stock-model score at which a frame of a positive clip counts as the wake word
PEAK_FRAMES = 1  # with the "hard clips" method: frames either side of the stock model's best moment
MIN_LEVEL = 300
CHUNK = 1280
FOLDS = 4
# How to say it, 5 clips each: real use isn't always a clear, close "hey jarvis".
STYLES = ["in your normal voice", "a bit more softly", "leaning back, further from the mic",
          "quickly, as if in the middle of something"]


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


def load_clips(folder: Path) -> list[np.ndarray]:
    """Every recording in a folder (all earlier runs), oldest first."""
    clips = []
    for path in sorted(Path(folder).glob("*.wav"), key=lambda p: p.stat().st_mtime):
        with wave.open(str(path)) as w:
            if w.getframerate() == INPUT_RATE and w.getsampwidth() == 2 and w.getnchannels() == 1:
                clips.append(np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).copy())
    return clips


def clip_features(clip: np.ndarray, oww, model_name: str, threshold: float, N: int = 1,
                  hard_clips: bool = False) -> np.ndarray:
    """The model's input features (frames x 16 x 96) to learn from.

    threshold 0: every frame (ordinary speech). Otherwise the frames the stock model scores at
    `threshold` or more; with `hard_clips`, a clip with none gives its best-scoring frames instead.
    """
    rng = np.random.default_rng(len(clip))
    out = []
    for _ in range(N):
        audio = clip[rng.integers(0, CHUNK):] if N > 1 else clip  # small shifts give varied examples
        oww.reset()
        frames, scores = [], []
        for i in range(0, len(audio) - CHUNK + 1, CHUNK):
            scores.append(float(oww.predict(audio[i:i + CHUNK])[model_name]))
            frames.append(oww.preprocessor.get_features(oww.model_inputs[model_name]))
        if threshold <= 0:
            out.extend(frames)
            continue
        picked = [f for f, s in zip(frames, scores) if s >= threshold]
        if not picked and hard_clips and scores and max(scores) > 0:
            best = int(np.argmax(scores))
            picked = frames[max(0, best - PEAK_FRAMES):best + PEAK_FRAMES + 1]
        out.extend(picked)
    return np.vstack(out) if out else np.empty((0, 16, 96))


def _new_model_factory(model_name: str) -> Callable:
    from openwakeword.model import Model

    return lambda: Model(wakeword_models=[model_name], inference_framework="onnx")


def clip_feature_sets(positives: list[np.ndarray], negatives: list[np.ndarray], model_name: str,
                      extract: Callable | None = None, new_model: Callable | None = None, hard_clips: bool = False
                      ) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Features per recording (the slow part), so cross-validation can reuse them."""
    if extract is None:
        def extract(clip, oww, name, threshold, N):
            return clip_features(clip, oww, name, threshold, N, hard_clips=hard_clips)
    oww = (new_model or _new_model_factory(model_name))()
    pos, neg = [], []
    for clip in positives:
        oww.reset()
        pos.append(extract(pad(clip), oww, model_name, threshold=CAPTURE_THRESHOLD, N=5))
    for clip in negatives:
        oww.reset()
        neg.append(extract(pad(clip), oww, model_name, threshold=0.0, N=1))
    return pos, neg


def fit_verifier(pos_sets: list[np.ndarray], neg_sets: list[np.ndarray], out_path: Path) -> dict[str, int]:
    """Fit and save the verifier from per-recording features. Raises ValueError if they're unusable."""
    from openwakeword.custom_verifier_model import train_verifier_model

    used = sum(1 for f in pos_sets if len(f))
    if used < max(3, len(pos_sets) // 2):
        raise ValueError(f"only {used} of {len(pos_sets)} clips had a clear \"hey jarvis\" in them")
    neg_parts = [f for f in neg_sets if len(f)]
    if not neg_parts:
        raise ValueError("no usable normal-speech audio")
    pos, neg = np.vstack([f for f in pos_sets if len(f)]), np.vstack(neg_parts)
    model = train_verifier_model(np.vstack([pos, neg]), np.array([1] * len(pos) + [0] * len(neg)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        pickle.dump(model, f)
    return {"clips_used": used, "positive_frames": len(pos), "negative_frames": len(neg)}


def train_from_clips(positives: list[np.ndarray], negatives: list[np.ndarray], model_name: str, out_path: Path,
                     extract: Callable | None = None, new_model: Callable | None = None) -> dict[str, int]:
    """Fit and save the verifier. Returns counts; raises ValueError if the clips are unusable."""
    return fit_verifier(*clip_feature_sets(positives, negatives, model_name, extract, new_model), out_path)


def evaluate(detector_factory: Callable, positives: list[np.ndarray], negatives: list[np.ndarray],
             chunk: int = CHUNK) -> tuple[int, int]:
    """(wake words caught, false wakes in normal speech), replaying the recordings through a detector."""
    def wakes(clip: np.ndarray) -> int:
        det = detector_factory()
        audio = pad(clip)
        return sum(det.detect(audio[i:i + chunk].tobytes()) for i in range(0, len(audio) - chunk + 1, chunk))

    return sum(wakes(c) > 0 for c in positives), sum(wakes(c) for c in negatives)


def cross_validate(positives: list[np.ndarray], negatives: list[np.ndarray], model_name: str, scratch: Path,
                   detector_factory: Callable[[Path], object], folds: int = FOLDS,
                   feature_sets: tuple[list, list] | None = None) -> tuple[int, int, int]:
    """(caught, tried, false wakes) on recordings each trial voice model did NOT learn from."""
    pos_sets, neg_sets = feature_sets or clip_feature_sets(positives, negatives, model_name)
    import logging

    quiet = logging.getLogger("core.wakeword")  # trial wakes are not real attempts: keep them out of app.log
    level = quiet.level
    quiet.setLevel(logging.WARNING)
    caught = tried = false_wakes = 0
    for k in range(folds):
        train_pos = [f for i, f in enumerate(pos_sets) if i % folds != k]
        train_neg = [f for i, f in enumerate(neg_sets) if i % folds != k]
        test_pos = [c for i, c in enumerate(positives) if i % folds == k]
        test_neg = [c for i, c in enumerate(negatives) if i % folds == k]
        if not test_pos:
            continue
        try:
            fit_verifier(train_pos, train_neg, scratch)
        except ValueError:
            tried += len(test_pos)  # no model could be trained from the rest: these all count as misses
            continue
        c, f = evaluate(lambda: detector_factory(scratch), test_pos, test_neg)
        caught, tried, false_wakes = caught + c, tried + len(test_pos), false_wakes + f
    scratch.unlink(missing_ok=True)
    quiet.setLevel(level)
    return caught, tried, false_wakes


def _wakes(n: int) -> str:
    return f"{n} false wake{'' if n == 1 else 's'}"


def _record(stream, seconds: float) -> np.ndarray:
    frames, _ = stream.read(int(seconds * INPUT_RATE))
    return frames[:, 0].copy()


def run_wake_training(config: dict, data_dir: Path) -> int:
    import sounddevice as sd

    from core.app_mode import claim_single_instance
    from core.wakeword import WakeWordDetector

    if not claim_single_instance(wait_s=0):
        print("JARVIS is running. Quit it first (right-click its window > Quit JARVIS): it would")
        print('wake up and answer every "hey jarvis" you record.')
        return 1
    ww = config["wake_word"]
    model_name = ww["model"]
    device = resolve_input_device(config["audio"].get("input_device"))
    info = sd.query_devices(device if device is not None else sd.default.device[0])
    folder = Path(data_dir) / "wake"
    earlier = len(load_clips(folder / "positive"))
    print(f"Teaching the wake word your voice. Microphone: {info['name']}")
    print("Sit where you normally do. The recordings stay in")
    print(f"{folder} on this PC and are never uploaded.")
    if earlier:
        print(f"Your {earlier} earlier recordings are kept and used too.")
    print(f'\nYou\'ll say "hey jarvis" {POSITIVE_CLIPS} times, then talk normally for {NEGATIVE_S:.0f} seconds.')
    input("Press Enter when ready...")

    positives: list[np.ndarray] = []
    per_style = POSITIVE_CLIPS // len(STYLES)
    with sd.InputStream(samplerate=INPUT_RATE, channels=1, dtype="int16", device=device) as stream:
        n, announced = 0, -1
        while n < POSITIVE_CLIPS:
            if n % per_style == 0 and n != announced:  # not again after a retry
                announced = n
                print(f'\nNext {per_style}: say it {STYLES[n // per_style]}.')
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

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    for i, clip in enumerate(positives):
        save_wav(folder / "positive" / f"{stamp}_hey_jarvis_{i + 1}.wav", clip)
    for i, clip in enumerate(chunks):
        save_wav(folder / "negative" / f"{stamp}_speech_{i + 1}.wav", clip)
    all_pos, all_neg = load_clips(folder / "positive"), load_clips(folder / "negative")

    threshold = float(ww.get("verifier_threshold", 0.5))
    gain = float(ww.get("gain", 1.0))
    margin = float(ww.get("strict_margin", STRICT_MARGIN))
    print(f"Checking on recordings each trial model didn't learn from ({len(all_pos)} \"hey jarvis\", "
          f"{len(all_neg) * NEGATIVE_CHUNK_S:.0f} s of speech)...")
    results = []
    for hard in (False, True):  # the standard method first: it wins a tie
        sets = clip_feature_sets(all_pos, all_neg, model_name, hard_clips=hard)
        score = cross_validate(all_pos, all_neg, model_name, folder / "_trial_verifier.pkl",
                               lambda path: WakeWordDetector(model_name, threshold, gain, path, margin), feature_sets=sets)
        print(f"  {'learning from hard clips too' if hard else 'standard'}: woke for {score[0]} of {score[1]}, "
              f"{_wakes(score[2])}")
        results.append((score, hard, sets))
    # most wakes, a false wake costing two of them; a tie keeps the standard method
    (caught, tried, false_wakes), hard, sets = max(results, key=lambda r: (r[0][0] - 2 * r[0][2], not r[1]))
    out = verifier_path(data_dir, model_name)
    print(f"Training on everything ({'learning from hard clips too' if hard else 'standard'})...")
    try:
        stats = fit_verifier(*sets, out)
    except ValueError as exc:
        print(f"Couldn't train: {exc}. Try again closer to the mic, saying it clearly.")
        return 1
    print(f"Done: learned from {stats['clips_used']} of {len(all_pos)} recordings. Saved {out.name}.")
    if tried:
        print(f"On recordings it hadn't heard: woke for {caught} of {tried} \"hey jarvis\" "
              f"({100 * caught // tried}%), {_wakes(false_wakes)} in normal speech.")
    print('Next: start JARVIS and say "hey jarvis" a few times; every attempt is logged in logs/app.log.')
    print("To go back to the stock model: delete that file, or set wake_word.personal_verifier: false.")
    return 0
