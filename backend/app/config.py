from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    storage_path: Path = Path("/storage")
    # base.en is the recommended starting point: noticeably more accurate than
    # tiny.en (which mis-hears words on real speech) while still fast on CPU via
    # faster-whisper. Bump to small.en/medium.en for better accuracy if you have
    # the CPU/RAM budget. Override via env.
    whisper_model: str = "base.en"
    # CTranslate2 compute type. "int8_float32" quantizes weights to int8 while
    # keeping activations in float32 — near-lossless quality, ~half the RAM, and
    # AVX2-accelerated on CPUs like the Intel N150. Use "int8" for less RAM still,
    # or "float32" to disable quantization entirely.
    whisper_compute_type: str = "int8_float32"
    # CPU threads for transcription. 0 lets CTranslate2 auto-detect physical cores.
    whisper_cpu_threads: int = 0
    cleanup_delay_seconds: int = 600
    # Extra browser origins allowed via CORS, comma-separated. Empty by default:
    # the app is same-origin behind nginx, so no CORS headers are needed. Set this
    # only when the API is called from a different origin.
    cors_origins: str = ""
    # Root log level for the API and Celery worker/beat ("DEBUG", "INFO",
    # "WARNING", ...). Applied by app.logging_setup.configure_logging(); note
    # the worker's --loglevel CLI flag is superseded by this (the celery
    # setup_logging hook takes over logging config entirely).
    log_level: str = "INFO"

    # Maximum accepted upload size in megabytes. Enforced twice by the backend:
    # from the request's Content-Length in middleware (rejects honest oversized
    # uploads before the body is read) and while streaming the file to disk
    # (covers chunked/lying clients). Keep <= nginx's client_max_body_size
    # (2000m), which remains the hard edge cap.
    max_upload_mb: int = Field(default=2000, ge=1)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    # Silence detection threshold in dB: audio quieter than this (relative to
    # 0dBFS) counts as silence. -30dB is moderate — quiet enough to not trigger
    # on room tone/light breathing, loud enough to catch real gaps between speech.
    # Lower (more negative) = stricter/less removed; higher = more aggressive.
    silence_threshold_db: float = -30.0
    # Minimum duration (seconds) a quiet stretch must last to count as removable
    # silence. Below this, brief pauses between words are left alone — removing
    # them would make speech sound unnaturally clipped/rushed.
    silence_min_duration: float = 0.5
    # Padding (seconds) kept on each side of a detected silence gap. Prevents
    # clipping word onsets/decays that silencedetect's hard boundary slightly
    # underestimates.
    silence_padding: float = 0.15
    # Safety cap on the number of kept (speech) segments after silence removal.
    # Pathologically chatty/noisy audio can produce hundreds of silence gaps; an
    # ffmpeg filter_complex graph with that many trim/concat branches is slow to
    # build and risks hitting ffmpeg's internal graph limits. Beyond this cap the
    # shortest silences are progressively re-merged into their neighboring kept
    # segments, trading some silence-removal completeness for a bounded graph.
    # Must be >= 1: compute_kept_ranges' safety-merge loop assumes there's
    # always at least one gap left to re-merge across while shrinking toward
    # this cap, and indexes into an empty range otherwise.
    silence_max_segments: int = Field(default=60, ge=1)

    @property
    def upload_path(self) -> Path:
        p = self.storage_path / "uploads"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def output_path(self) -> Path:
        p = self.storage_path / "outputs"
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
