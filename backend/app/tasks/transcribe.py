import whisper
from ..config import settings

_model = None


def get_model() -> whisper.Whisper:
    global _model
    if _model is None:
        _model = whisper.load_model(settings.whisper_model)
    return _model


def transcribe(video_path: str, language: str = "auto") -> dict:
    model = get_model()
    kwargs: dict = {"word_timestamps": True, "verbose": False}
    if language != "auto":
        kwargs["language"] = language
    return model.transcribe(video_path, **kwargs)
