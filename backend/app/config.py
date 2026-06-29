from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    storage_path: Path = Path("/storage")
    # small.en is the default: faster-whisper makes it land near the old base.en
    # wall-clock on CPU while being noticeably more accurate. Override via env.
    whisper_model: str = "small.en"
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
