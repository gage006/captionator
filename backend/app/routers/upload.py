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
    job_id = uuid.uuid4().hex[:8]
    dest_dir = settings.upload_path / job_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    safe_filename = file.filename or "video.mp4"
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
