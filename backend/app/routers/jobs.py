import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import Job
from ..config import settings
from ..schemas import JobStatus, StyleInfo, RenderRequest, TranscriptResponse
from ..styles.definitions import STYLES, base_font_size
from ..tasks.celery_app import celery

router = APIRouter()


@router.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatus(
        job_id=job.id,
        status=job.status,
        step=job.step,
        progress=job.progress,
        style=job.style,
        position_x=job.position_x,
        position_y=job.position_y,
        scale=job.scale,
        error=job.error,
        created_at=job.created_at,
        completed_at=job.completed_at,
    )


@router.get("/jobs/{job_id}/transcript", response_model=TranscriptResponse)
def get_transcript(job_id: str, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    transcript_path = settings.output_path / job_id / "transcript.json"
    if not transcript_path.exists():
        raise HTTPException(status_code=409, detail="Transcript not ready yet")

    return TranscriptResponse(**json.loads(transcript_path.read_text(encoding="utf-8")))


@router.post("/jobs/{job_id}/render", response_model=JobStatus, status_code=202)
def render_job(job_id: str, req: RenderRequest, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if req.style not in STYLES:
        raise HTTPException(status_code=400, detail=f"Unknown style: {req.style}")

    transcript_path = settings.output_path / job_id / "transcript.json"
    if not transcript_path.exists():
        raise HTTPException(status_code=409, detail="Transcript not ready yet")

    job.style = req.style
    job.position_x = req.position_x
    job.position_y = req.position_y
    job.scale = req.scale
    job.status = "rendering"
    job.step = "styling"
    job.progress = 55
    job.error = None
    job.completed_at = None
    db.commit()

    celery.send_task("render_video", args=[job_id])

    return JobStatus(
        job_id=job.id,
        status=job.status,
        step=job.step,
        progress=job.progress,
        style=job.style,
        position_x=job.position_x,
        position_y=job.position_y,
        scale=job.scale,
        error=job.error,
        created_at=job.created_at,
        completed_at=job.completed_at,
    )


@router.get("/styles", response_model=list[StyleInfo])
def get_styles():
    return [
        StyleInfo(
            id=k,
            label=v["label"],
            description=v["description"],
            preview_color=v["preview_color"],
            base_font_size=base_font_size(v),
        )
        for k, v in STYLES.items()
    ]
