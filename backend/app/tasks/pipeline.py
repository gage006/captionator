import json
import logging
import shutil
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from .celery_app import celery
from ..database import SessionLocal
from ..models import Job
from ..config import settings
from .transcribe import transcribe
from .ass_generator import build as build_ass
from .ffmpeg_burn import get_video_dimensions, get_video_duration, burn_subtitles
from .silence import detect_silences, compute_kept_ranges, trim_silences, remap_segments

logger = logging.getLogger(__name__)


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


def _atomic_write_text(path: Path, content: str) -> None:
    """Write via a temp file + rename so a concurrent reader (e.g. render_video
    running in a different worker process) never observes a partially-written
    file — os.replace()/Path.replace() is an atomic rename on POSIX."""
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def write_transcript_files(output_dir: Path, segments: list) -> None:
    """Persist transcript artifacts: SRT + TXT for download, JSON for preview/
    render. Shared by the initial transcription (transcribe_video) and the
    transcript-edit endpoint, so an edit saved before render keeps the
    downloadable SRT/TXT consistent with whatever the eventual burn will show."""
    srt_lines = []
    for i, seg in enumerate(segments, 1):
        srt_lines.extend([
            str(i),
            f"{_format_srt_time(seg['start'])} --> {_format_srt_time(seg['end'])}",
            seg["text"],
            "",
        ])
    _atomic_write_text(output_dir / "transcript.srt", "\n".join(srt_lines))
    _atomic_write_text(
        output_dir / "transcript.txt", " ".join(seg["text"] for seg in segments)
    )
    _atomic_write_text(output_dir / "transcript.json", json.dumps({"segments": segments}))


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

        logger.info("transcribe_video started: job=%s file=%s", job_id, job.filename)
        started = time.monotonic()

        video_path = job.filename
        output_dir = settings.output_path / job_id
        output_dir.mkdir(parents=True, exist_ok=True)

        _update_job(db, job_id, status="transcribing", step="transcribing", progress=5)

        # Map Whisper's 0..1 decode progress onto 5..95% and persist it as it
        # climbs, so the frontend's poll sees a real, advancing bar instead of a
        # stall at a fixed value. Throttle to whole-percent increases to avoid a
        # DB commit on every tqdm tick.
        last_pct = 5

        def on_progress(fraction: float) -> None:
            nonlocal last_pct
            pct = 5 + int(fraction * 90)
            if pct > last_pct:
                last_pct = pct
                _update_job(db, job_id, progress=pct)

        result = transcribe(video_path, job.language, progress_callback=on_progress)
        segments = _serializable_segments(result["segments"])
        write_transcript_files(output_dir, segments)

        logger.info(
            "transcription complete: job=%s segments=%d took=%.1fs",
            job_id, len(segments), time.monotonic() - started,
        )

        silence_removed_seconds = None
        if job.remove_silences:
            silence_removed_seconds = 0.0
            _update_job(db, job_id, step="removing_silences", progress=95)

            duration = get_video_duration(video_path)
            silences = detect_silences(
                video_path, settings.silence_threshold_db, settings.silence_min_duration
            )
            kept_ranges = compute_kept_ranges(
                silences, duration, settings.silence_padding, settings.silence_max_segments
            )

            if not kept_ranges:
                # Entire video flagged silent — refuse to produce a zero-duration
                # output. remove_silences was explicitly requested, so silently
                # ignoring it would be more surprising than failing loudly. The
                # pre-trim transcript written above is still on disk and
                # fetchable even though the job itself is marked failed.
                raise RuntimeError(
                    "Silence removal would remove the entire video; aborting."
                )

            if not (len(kept_ranges) == 1 and kept_ranges[0] == (0.0, duration)):
                trimmed_path = str(output_dir / "trimmed.mp4")

                def on_trim_progress(fraction: float) -> None:
                    _update_job(db, job_id, progress=95 + int(fraction * 4))

                trim_silences(
                    video_path, kept_ranges, trimmed_path, progress_callback=on_trim_progress
                )

                kept_total = sum(e - s for s, e in kept_ranges)
                silence_removed_seconds = max(0.0, duration - kept_total)
                segments = remap_segments(segments, kept_ranges)
                video_path = trimmed_path
                # Overwrite with the final, trimmed-timeline transcript now
                # that the trim actually succeeded.
                write_transcript_files(output_dir, segments)

                logger.info(
                    "silence removal: job=%s removed=%.2fs kept_ranges=%d",
                    job_id, silence_removed_seconds, len(kept_ranges),
                )

        _update_job(
            db,
            job_id,
            status="ready",
            step="preview_ready",
            progress=100,
            filename=video_path,
            silence_removed_seconds=silence_removed_seconds,
        )

        logger.info("job ready for preview: job=%s", job_id)

    except Exception as exc:
        logger.exception("transcribe_video failed: job=%s", job_id)
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

        logger.info("render_video started: job=%s style=%s", job_id, job.style)
        started = time.monotonic()

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

        _update_job(db, job_id, step="burning", progress=60)

        # Map FFmpeg's 0..1 encode progress onto 60..99% and persist it as it
        # climbs, so the (longest) burn step shows a live bar instead of stalling
        # at a fixed value. Throttle to whole-percent increases to avoid a DB
        # commit on every FFmpeg progress tick.
        last_pct = 60

        def on_burn_progress(fraction: float) -> None:
            nonlocal last_pct
            pct = 60 + int(fraction * 39)
            if pct > last_pct:
                last_pct = pct
                _update_job(db, job_id, progress=pct)

        output_video = str(output_dir / "output.mp4")
        burn_subtitles(
            video_path, str(ass_path), output_video, progress_callback=on_burn_progress
        )

        _update_job(
            db,
            job_id,
            status="complete",
            step="done",
            progress=100,
            completed_at=datetime.now(timezone.utc),
        )

        logger.info("render complete: job=%s took=%.1fs", job_id, time.monotonic() - started)

        cleanup_job.apply_async(args=[job_id], countdown=settings.cleanup_delay_seconds)

    except Exception as exc:
        logger.exception("render_video failed: job=%s", job_id)
        _update_job(db, job_id, status="failed", error=str(exc))
        raise
    finally:
        db.close()


@celery.task(name="cleanup_job")
def cleanup_job(job_id: str) -> None:
    for base in (settings.upload_path, settings.output_path):
        shutil.rmtree(base / job_id, ignore_errors=True)
    logger.info("cleaned up job files: job=%s", job_id)
    db = SessionLocal()
    try:
        _update_job(db, job_id, status="expired", step="deleted", progress=100)
    finally:
        db.close()


@celery.task(name="sweep_expired_jobs")
def sweep_expired_jobs() -> None:
    """Durable backstop for cleanup_job.

    Runs on a Celery Beat schedule and reaps three kinds of stale jobs:
      1. Completed jobs whose completed_at is older than the cleanup delay
         (the durable backstop for the per-job countdown cleanup).
      2. Abandoned or failed pre-render jobs (uploaded/transcribed but never
         saved, or terminally failed) whose created_at is older than a longer
         grace window — without this their source uploads would leak forever.
         The window is generous so it never deletes a video while the user is
         still in the preview editor. Only terminal/idle statuses are reaped
         here; an in-progress "rendering" job is left alone so a slow burn is
         never deleted out from under itself.
      3. Stalled renders: jobs stuck in "rendering" far past render_started_at
         (a worker crash mid-burn leaves them there forever, and since the
         active-job cap counts "rendering", each one permanently eats a
         MAX_ACTIVE_JOBS slot). These are marked failed — not deleted — which
         frees the cap slot immediately; pass 2 reaps their files on a later
         tick. RENDER_STALL_SECONDS is generous (hours vs. the minutes a real
         burn takes) so a legitimately slow render is never declared dead. If
         a wedged render's task does eventually run to completion anyway
         (e.g. a worker comes back and drains the old queue), render_video
         simply marks the job complete again — the burn genuinely finished,
         so that's correct.

    Because the work is derived from the database on each tick (not held in worker
    memory), pending cleanups survive worker/beat restarts. Idempotent with the
    countdown task: cleanup_job flips status to "expired", which these queries exclude.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    complete_cutoff = now - timedelta(seconds=settings.cleanup_delay_seconds)
    abandoned_cutoff = now - timedelta(
        seconds=max(settings.cleanup_delay_seconds, 3600)
    )
    render_stall_cutoff = now - timedelta(seconds=settings.render_stall_seconds)
    db = SessionLocal()
    try:
        stalled_renders = (
            db.query(Job)
            .filter(
                Job.status == "rendering",
                # Rows predating the render_started_at column are left alone;
                # every render started since the column exists sets it.
                Job.render_started_at.isnot(None),
                Job.render_started_at < render_stall_cutoff,
            )
            .all()
        )
        for job in stalled_renders:
            job.status = "failed"
            job.error = (
                "Render stalled (no completion after "
                f"{settings.render_stall_seconds}s) — likely a worker crash "
                "mid-burn. Files will be cleaned up automatically."
            )
        if stalled_renders:
            db.commit()
            logger.warning(
                "failed %d stalled render(s): %s",
                len(stalled_renders),
                ", ".join(job.id for job in stalled_renders),
            )

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

    if stale_ids:
        logger.info("sweeping %d expired job(s)", len(stale_ids))

    for job_id in stale_ids:
        cleanup_job(job_id)
