from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    storage_path: Path = Path("/storage")
    whisper_model: str = "base.en"
    cleanup_delay_seconds: int = 600

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
