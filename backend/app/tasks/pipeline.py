import json
import shutil
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from .celery_app import celery
from ..database import SessionLocal
from ..models import Job
from ..config import settings
from .transcribe import transcribe
from .ass_generator import build as build_ass
from .ffmpeg_burn import get_video_dimensions, burn_subtitles


def _update_job(db: Session, job_id: str, **kwargs) -> None:
    db.query(Job).filter(Job.id == job_id).update(kwargs)
    db.commit()


def _format_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds % 1) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _serializable_segments(segments: list) -> list:
    """Keep only the fields the preview overlay and renderer need from Whisper
    output, so the transcript persists as compact, JSON-safe data."""
    out = []
    for seg in segments:
        out.append(
            {
                "start": seg["start"],
                "end": seg["end"],
                "text": seg["text"].strip(),
                "words": [
                    {"word": w["word"], "start": w["start"], "end": w["end"]}
                    for w in seg.get("words", [])
                ],
            }
        )
    return out


@celery.task(name="transcribe_video")
def transcribe_video(job_id: str) -> None:
    """Phase 1 (on upload): transcribe and persist the transcript so the frontend
    can render a live caption preview. Does NOT burn or schedule cleanup — the
    upload must survive until the user saves a render."""
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return

        video_path = job.filename
        output_dir = settings.output_path / job_id
        output_dir.mkdir(parents=True, exist_ok=True)

        _update_job(db, job_id, status="transcribing", step="transcribing", progress=10)
        result = transcribe(video_path, job.language)
        segments = _serializable_segments(result["segments"])

        # Persist transcript artifacts: SRT + TXT for download, JSON for preview/render.
        srt_lines = []
        for i, seg in enumerate(segments, 1):
            srt_lines.extend([
                str(i),
                f"{_format_srt_time(seg['start'])} --> {_format_srt_time(seg['end'])}",
                seg["text"],
                "",
            ])
        (output_dir / "transcript.srt").write_text("\n".join(srt_lines), encoding="utf-8")
        (output_dir / "transcript.txt").write_text(
            " ".join(seg["text"] for seg in segments),
            encoding="utf-8",
        )
        (output_dir / "transcript.json").write_text(
            json.dumps({"segments": segments}), encoding="utf-8"
        )

        _update_job(db, job_id, status="ready", step="preview_ready", progress=100)

    except Exception as exc:
        _update_job(db, job_id, status="failed", error=str(exc))
        raise
    finally:
        db.close()


@celery.task(name="render_video")
def render_video(job_id: str) -> None:
    """Phase 2 (on save): build the ASS file from the chosen style + placement and
    burn it into the source video, then schedule cleanup."""
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return

        video_path = job.filename
        output_dir = settings.output_path / job_id
        output_dir.mkdir(parents=True, exist_ok=True)

        transcript_path = output_dir / "transcript.json"
        segments = json.loads(transcript_path.read_text(encoding="utf-8"))["segments"]

        _update_job(db, job_id, status="rendering", step="styling", progress=55)

        width, height = get_video_dimensions(video_path)
        ass_content = build_ass(
            segments,
            job.style,
            width,
            height,
            position=(job.position_x, job.position_y),
            scale=job.scale,
        )
        ass_path = output_dir / "captions.ass"
        ass_path.write_text(ass_content, encoding="utf-8")

        _update_job(db, job_id, step="burning", progress=70)
        output_video = str(output_dir / "output.mp4")
        burn_subtitles(video_path, str(ass_path), output_video)

        _update_job(
            db,
            job_id,
            status="complete",
            step="done",
            progress=100,
            completed_at=datetime.now(timezone.utc),
        )

        cleanup_job.apply_async(args=[job_id], countdown=settings.cleanup_delay_seconds)

    except Exception as exc:
        _update_job(db, job_id, status="failed", error=str(exc))
        raise
    finally:
        db.close()


@celery.task(name="cleanup_job")
def cleanup_job(job_id: str) -> None:
    for base in (settings.upload_path, settings.output_path):
        shutil.rmtree(base / job_id, ignore_errors=True)
    db = SessionLocal()
    try:
        _update_job(db, job_id, status="expired", step="deleted", progress=100)
    finally:
        db.close()


@celery.task(name="sweep_expired_jobs")
def sweep_expired_jobs() -> None:
    """Durable backstop for cleanup_job.

    Runs on a Celery Beat schedule and reaps two kinds of stale jobs:
      1. Completed jobs whose completed_at is older than the cleanup delay
         (the durable backstop for the per-job countdown cleanup).
      2. Abandoned or failed pre-render jobs (uploaded/transcribed but never
         saved, or terminally failed) whose created_at is older than a longer
         grace window — without this their source uploads would leak forever.
         The window is generous so it never deletes a video while the user is
         still in the preview editor. Only terminal/idle statuses are reaped
         here; an in-progress "rendering" job is left alone so a slow burn is
         never deleted out from under itself.

    Because the work is derived from the database on each tick (not held in worker
    memory), pending cleanups survive worker/beat restarts. Idempotent with the
    countdown task: cleanup_job flips status to "expired", which these queries exclude.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    complete_cutoff = now - timedelta(seconds=settings.cleanup_delay_seconds)
    abandoned_cutoff = now - timedelta(
        seconds=max(settings.cleanup_delay_seconds, 3600)
    )
    db = SessionLocal()
    try:
        stale_ids = [
            row.id
            for row in db.query(Job.id)
            .filter(
                Job.status == "complete",
                Job.completed_at.isnot(None),
                Job.completed_at < complete_cutoff,
            )
            .all()
        ]
        stale_ids += [
            row.id
            for row in db.query(Job.id)
            .filter(
                Job.status.in_(("transcribing", "ready", "failed")),
                Job.created_at < abandoned_cutoff,
            )
            .all()
        ]
    finally:
        db.close()

    for job_id in stale_ids:
        cleanup_job(job_id)
