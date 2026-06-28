import os
from celery import Celery

celery = Celery(
    "captionator",
    broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    backend=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    include=["app.tasks.pipeline"],
)

celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    worker_prefetch_multiplier=1,
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
    from ..database import Base, engine
    Base.metadata.create_all(bind=engine)
