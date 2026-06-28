from typing import Callable, Optional

from faster_whisper import WhisperModel
from ..config import settings

_model: Optional[WhisperModel] = None


def get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(
            settings.whisper_model,
            device="cpu",
            compute_type=settings.whisper_compute_type,
            cpu_threads=settings.whisper_cpu_threads,
        )
    return _model


def transcribe(
    video_path: str,
    language: str = "auto",
    progress_callback: Optional[Callable[[float], None]] = None,
) -> dict:
    """Transcribe with faster-whisper (CTranslate2). Returns the same
    {"segments": [...]} shape the pipeline expects from the old openai-whisper
    path, with word-level timing preserved for the preview overlay and karaoke.

    faster-whisper's transcribe() is lazy: the returned `segments` is a generator
    that performs the actual decoding as we iterate. We drive real progress off
    each segment's end time vs. the total audio duration — no tqdm shim needed.
    """
    model = get_model()
    segments_iter, info = model.transcribe(
        video_path,
        word_timestamps=True,
        vad_filter=True,  # skip silence: faster, and avoids silence hallucinations
        language=None if language == "auto" else language,
    )

    duration = info.duration or 0.0
    segments: list[dict] = []
    for seg in segments_iter:
        words = [
            {"word": w.word, "start": w.start, "end": w.end}
            for w in (seg.words or [])
            if w.start is not None and w.end is not None
        ]
        segments.append(
            {"start": seg.start, "end": seg.end, "text": seg.text, "words": words}
        )
        if progress_callback and duration:
            progress_callback(min(1.0, seg.end / duration))

    return {"segments": segments}
