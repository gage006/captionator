"""Benchmark WHISPER_MODEL candidates on the Gehrig public-domain fixture.

Runs inside the production backend/worker image so the environment matches
deployment exactly (CTranslate2 CPU kernels, int8_float32, VAD). For each
model it measures:

  - first_init_s: first WhisperModel() call (includes HF download on a cold
    cache — reported separately so it doesn't pollute load time)
  - load_s: re-instantiation with a warm disk cache (the real cold-start cost
    a worker pays once per process)
  - transcribe runs 1 and 2 (run 2 is the steady-state number; run 1 can
    include lazy one-time init inside CT2)
  - accuracy: WER against the verified reference transcript of the clip
    (Rev.com / SABR-derived, same source as GEHRIG_KNOWN_PHRASES in
    backend/tests/test_real_speech_silence_removal.py) plus the test suite's
    five known-phrase checks
  - timed_words / total_words: words that came back with usable start/end
    timestamps — the app's karaoke styles and preview overlay depend on these

Usage (from repo root):
  docker run --rm \
    -v captionator_whisper_cache:/home/app/.cache/huggingface \
    -v $PWD/backend/tests/fixtures:/bench/fixtures:ro \
    -v $PWD/scripts:/bench/scripts:ro \
    ghcr.io/gage006/captionator-backend:latest \
    python /bench/scripts/bench_whisper_models.py
"""
import gc
import json
import re
import time

from faster_whisper import WhisperModel

AUDIO = "/bench/fixtures/real_speech_gehrig.mp4"
MODELS = ["base.en", "distil-small.en", "distil-medium.en"]
COMPUTE_TYPE = "int8_float32"  # production default (WHISPER_COMPUTE_TYPE)

# Verified transcript of this exact ~21s clip (closing lines of the speech),
# per the reference cited in test_real_speech_silence_removal.py.
REFERENCE = (
    "I consider myself the luckiest man on the face of the earth. "
    "And I might've been given a bad break, but I've got an awful lot "
    "to live for. Thank you."
)

KNOWN_PHRASES = [
    "luckiest man",
    "face of the earth",
    "bad break",
    "awful lot to live for",
    "thank you",
]

CONTRACTIONS = {
    "might've": "might have",
    "i've": "i have",
    "i'm": "i am",
    "it's": "it is",
    "that's": "that is",
}


def normalize(text: str) -> list[str]:
    text = text.lower()
    for c, full in CONTRACTIONS.items():
        text = text.replace(c, full)
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return text.split()


def wer(ref: list[str], hyp: list[str]) -> float:
    """Word error rate via standard edit distance."""
    d = list(range(len(hyp) + 1))
    for i, rw in enumerate(ref, 1):
        prev_diag, d[0] = d[0], i
        for j, hw in enumerate(hyp, 1):
            cur = min(
                d[j] + 1,          # deletion
                d[j - 1] + 1,      # insertion
                prev_diag + (rw != hw),  # substitution/match
            )
            prev_diag, d[j] = d[j], cur
    return d[-1] / max(1, len(ref))


def transcribe_once(model: WhisperModel) -> tuple[float, dict]:
    """Mirror app/tasks/transcribe.py: VAD on, word timestamps, auto language.
    The segments iterator is lazy — iterating IS the decode, so time the loop."""
    t0 = time.perf_counter()
    segments_iter, info = model.transcribe(
        AUDIO,
        word_timestamps=True,
        vad_filter=True,
        language=None,
    )
    segments = []
    timed_words = 0
    total_words = 0
    for seg in segments_iter:
        for w in seg.words or []:
            total_words += 1
            if w.start is not None and w.end is not None:
                timed_words += 1
        segments.append({"start": seg.start, "end": seg.end, "text": seg.text})
    elapsed = time.perf_counter() - t0
    return elapsed, {
        "duration": info.duration,
        "segments": segments,
        "timed_words": timed_words,
        "total_words": total_words,
    }


def bench_model(name: str) -> dict:
    t0 = time.perf_counter()
    model = WhisperModel(name, device="cpu", compute_type=COMPUTE_TYPE, cpu_threads=0)
    first_init_s = time.perf_counter() - t0

    del model
    gc.collect()
    t0 = time.perf_counter()
    model = WhisperModel(name, device="cpu", compute_type=COMPUTE_TYPE, cpu_threads=0)
    load_s = time.perf_counter() - t0

    run1_s, result = transcribe_once(model)
    run2_s, result2 = transcribe_once(model)

    full_text = " ".join(s["text"].strip() for s in result2["segments"])
    hyp = normalize(full_text)
    ref = normalize(REFERENCE)
    lower = full_text.lower()
    missing = [p for p in KNOWN_PHRASES if p not in lower]

    del model
    gc.collect()

    return {
        "model": name,
        "first_init_s": round(first_init_s, 2),
        "load_s": round(load_s, 2),
        "transcribe_run1_s": round(run1_s, 2),
        "transcribe_run2_s": round(run2_s, 2),
        "audio_duration_s": round(result2["duration"], 2),
        "realtime_factor_run2": round(result2["duration"] / run2_s, 2),
        "wer_pct": round(100 * wer(ref, hyp), 1),
        "phrases_found": len(KNOWN_PHRASES) - len(missing),
        "phrases_missing": missing,
        "timed_words": result2["timed_words"],
        "total_words": result2["total_words"],
        "transcript": full_text,
        "run1_transcript_matches_run2": (
            [s["text"] for s in result["segments"]]
            == [s["text"] for s in result2["segments"]]
        ),
    }


def main() -> None:
    results = []
    for name in MODELS:
        print(f"=== {name} ===", flush=True)
        r = bench_model(name)
        results.append(r)
        for k, v in r.items():
            if k != "transcript":
                print(f"  {k}: {v}")
        print(f"  transcript: {r['transcript']!r}", flush=True)
    print("\nRESULTS_JSON_BEGIN")
    print(json.dumps(results, indent=2))
    print("RESULTS_JSON_END")


if __name__ == "__main__":
    main()
