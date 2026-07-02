import asyncio
import json
import logging
import os
import shutil
import subprocess
import uuid
import aiofiles
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Depends
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import Job
from ..config import settings
from ..schemas import UploadResponse
from ..tasks.celery_app import celery

router = APIRouter()
logger = logging.getLogger(__name__)


def _probe_has_video_and_audio(path: str) -> bool:
    """True iff ffprobe can decode the file and finds at least one video and
    one audio stream — the minimum the pipeline needs (Whisper transcribes the
    audio track; the burn step re-encodes the video track). ffprobe reads only
    container headers, so this is fast regardless of file size."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False
    if result.returncode != 0:
        return False
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError:
        return False
    codec_types = {s.get("codec_type") for s in streams}
    return "video" in codec_types and "audio" in codec_types


@router.post("/upload", response_model=UploadResponse, status_code=202)
async def upload_video(
    file: UploadFile = File(...),
    language: str = Form("auto"),
    remove_silences: bool = Form(False),
    db: Session = Depends(get_db),
):
    # Style and placement are chosen later in the preview editor; upload only
    # stores the video and kicks off transcription.
    # Full 128-bit hex id: unguessable (the download/preview/transcript routes are
    # unauthenticated, so a short id would be enumerable) and collision-free.
    active = (
        db.query(Job)
        .filter(Job.status.in_(("transcribing", "rendering")))
        .count()
    )
    if active >= settings.max_active_jobs:
        logger.warning("upload rejected (server busy): active_jobs=%d", active)
        raise HTTPException(
            status_code=429,
            detail="Server is busy processing other videos. Try again in a few minutes.",
        )

    job_id = uuid.uuid4().hex
    dest_dir = settings.upload_path / job_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Never trust the client-supplied filename for path building: strip any
    # directory components (handling both "/" and "\" separators) and reject the
    # traversal names so the write can only ever land inside dest_dir.
    safe_filename = os.path.basename((file.filename or "").replace("\\", "/"))
    if safe_filename in ("", ".", ".."):
        safe_filename = "video.mp4"
    dest = dest_dir / safe_filename

    size_bytes = 0
    async with aiofiles.open(dest, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size_bytes += len(chunk)
            if size_bytes > settings.max_upload_bytes:
                shutil.rmtree(dest_dir, ignore_errors=True)
                logger.warning(
                    "upload rejected (too large mid-stream): file=%s limit=%dMB",
                    safe_filename, settings.max_upload_mb,
                )
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Limit is {settings.max_upload_mb} MB.",
                )
            await out.write(chunk)

    # Validate the bytes actually are a playable video before creating a job:
    # this turns what would be a cryptic mid-pipeline Whisper/FFmpeg failure
    # into an immediate, clear 415. Run in a thread so the (brief) subprocess
    # call doesn't block the event loop.
    if not await asyncio.to_thread(_probe_has_video_and_audio, str(dest)):
        shutil.rmtree(dest_dir, ignore_errors=True)
        logger.warning("upload rejected (not a playable video): file=%s", safe_filename)
        raise HTTPException(
            status_code=415,
            detail="Upload must be a video file with an audio track.",
        )

    job = Job(
        id=job_id,
        filename=str(dest),
        language=language,
        remove_silences=remove_silences,
        status="transcribing",
        step="transcribing",
        progress=0,
    )
    db.add(job)
    db.commit()

    celery.send_task("transcribe_video", args=[job_id])

    logger.info(
        "upload accepted: job=%s file=%s size=%dB language=%s remove_silences=%s",
        job_id, safe_filename, size_bytes, language, remove_silences,
    )

    return UploadResponse(
        job_id=job_id,
        status="transcribing",
        message="Upload received. Transcribing…",
    )
