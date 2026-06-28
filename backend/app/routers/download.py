from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import Job
from ..config import settings

router = APIRouter()


@router.api_route("/download/{job_id}/{file_type}", methods=["GET", "HEAD"])
def download_file(job_id: str, file_type: str, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "complete":
        raise HTTPException(status_code=400, detail="Job not complete yet")

    output_dir = settings.output_path / job_id

    if file_type in ("video", "mp4"):
        path = output_dir / "output.mp4"
        media_type = "video/mp4"
        filename = "captionated.mp4"
    elif file_type == "srt":
        path = output_dir / "transcript.srt"
        media_type = "text/plain"
        filename = "transcript.srt"
    elif file_type == "txt":
        path = output_dir / "transcript.txt"
        media_type = "text/plain"
        filename = "transcript.txt"
    elif file_type == "ass":
        path = output_dir / "captions.ass"
        media_type = "text/plain"
        filename = "captions.ass"
    else:
        raise HTTPException(status_code=400, detail="Invalid file type. Use video, srt, txt, or ass.")

    if not path.exists():
        raise HTTPException(status_code=404, detail="Output file not found")

    return FileResponse(
        path=str(path),
        media_type=media_type,
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
