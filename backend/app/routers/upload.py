import os
import uuid
import aiofiles
from fastapi import APIRouter, File, Form, UploadFile, Depends
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import Job
from ..config import settings
from ..schemas import UploadResponse
from ..tasks.celery_app import celery

router = APIRouter()


@router.post("/upload", response_model=UploadResponse, status_code=202)
async def upload_video(
    file: UploadFile = File(...),
    language: str = Form("auto"),
    db: Session = Depends(get_db),
):
    # Style and placement are chosen later in the preview editor; upload only
    # stores the video and kicks off transcription.
    # Full 128-bit hex id: unguessable (the download/preview/transcript routes are
    # unauthenticated, so a short id would be enumerable) and collision-free.
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

    async with aiofiles.open(dest, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            await out.write(chunk)

    job = Job(
        id=job_id,
        filename=str(dest),
        language=language,
        status="transcribing",
        step="transcribing",
        progress=0,
    )
    db.add(job)
    db.commit()

    celery.send_task("transcribe_video", args=[job_id])

    return UploadResponse(
        job_id=job_id,
        status="transcribing",
        message="Upload received. Transcribing…",
    )
