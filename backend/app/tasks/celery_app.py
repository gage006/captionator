import os
import tempfile
from celery import Celery
from celery.signals import setup_logging

celery = Celery(
    "captionator",
    broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    backend=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    include=["app.tasks.pipeline"],
)

# Beat persists its schedule to a shelve/gdbm file. Defaulting to the bare
# "celerybeat-schedule" writes it to the cwd (/app in the image), which is
# root-owned — beat runs as the unprivileged "app" user, so creating/recreating
# the file there fails with "[Errno 13] Permission denied". Pin it to the OS temp
# dir (world-writable /tmp in the container, the OS tmp dir when run standalone)
# so any invocation is writable regardless of the launch command. The shelf is
# just last-run bookkeeping, so an ephemeral per-container path is fine.
beat_schedule_filename = os.path.join(tempfile.gettempdir(), "celerybeat-schedule")

celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    worker_prefetch_multiplier=1,
    beat_schedule_filename=beat_schedule_filename,
)

# Durable cleanup: re-derive expired jobs from the DB every minute so pending
# deletions survive worker/beat restarts (see pipeline.sweep_expired_jobs).
celery.conf.beat_schedule = {
    "sweep-expired-jobs": {
        "task": "sweep_expired_jobs",
        "schedule": 60.0,
    },
}


@celery.on_after_configure.connect
def init_db(sender, **kwargs):
    from ..database import Base, engine, ensure_schema
    Base.metadata.create_all(bind=engine)
    ensure_schema()


@setup_logging.connect
def _configure_logging(**_kwargs):
    # Connecting this signal makes Celery skip its own logger hijacking, so
    # worker/beat lines match the API's format and honor LOG_LEVEL (the
    # --loglevel CLI flag is superseded). Task/module loggers propagate to the
    # root handler configured here.
    from ..logging_setup import configure_logging
    configure_logging()
