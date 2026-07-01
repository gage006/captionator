import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import Job
from ..config import settings
from ..schemas import (
    JobStatus,
    StyleInfo,
    RenderRequest,
    TranscriptResponse,
    TranscriptEditRequest,
)
from ..styles.definitions import STYLES, base_font_size
from ..tasks.celery_app import celery
from ..tasks.pipeline import write_transcript_files

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
        remove_silences=job.remove_silences,
        silence_removed_seconds=job.silence_removed_seconds,
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


@router.put("/jobs/{job_id}/transcript", response_model=TranscriptResponse)
def update_transcript(job_id: str, req: TranscriptEditRequest, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "ready":
        raise HTTPException(
            status_code=409,
            detail="Transcript can only be edited while the job is ready for preview",
        )

    output_dir = settings.output_path / job_id
    transcript_path = output_dir / "transcript.json"
    if not transcript_path.exists():
        raise HTTPException(status_code=409, detail="Transcript not ready yet")

    stored = json.loads(transcript_path.read_text(encoding="utf-8"))["segments"]
    if len(req.segments) != len(stored):
        raise HTTPException(
            status_code=400,
            detail=f"Expected {len(stored)} segments, got {len(req.segments)}",
        )

    updated = []
    for stored_seg, edit in zip(stored, req.segments):
        new_text = edit.text.strip()
        if new_text == stored_seg["text"]:
            updated.append(stored_seg)
            continue
        # Word-level timing can't be reliably re-derived from a free-text edit,
        # so collapse the edited segment to a single span covering its whole
        # duration. This only loses per-word highlight precision (karaoke /
        # compound styles) on segments that were actually edited — segments
        # left untouched keep their original word-level timing.
        updated.append(
            {
                "start": stored_seg["start"],
                "end": stored_seg["end"],
                "text": new_text,
                "words": (
                    [{"word": new_text, "start": stored_seg["start"], "end": stored_seg["end"]}]
                    if new_text
                    else []
                ),
            }
        )

    write_transcript_files(output_dir, updated)

    return TranscriptResponse(segments=updated)


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
        remove_silences=job.remove_silences,
        silence_removed_seconds=job.silence_removed_seconds,
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
