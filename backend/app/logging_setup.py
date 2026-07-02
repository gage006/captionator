import logging

from .config import settings

_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure_logging() -> None:
    """Configure the root logger once per process.

    Shared by the FastAPI app (called at import time in main.py) and the
    Celery worker/beat (via the setup_logging signal in tasks/celery_app.py)
    so every container emits the same line format to stdout, where
    `docker compose logs` picks it up. basicConfig is a no-op when the root
    logger already has handlers, so repeated calls are harmless.
    """
    logging.basicConfig(level=settings.log_level.upper(), format=_FORMAT)
