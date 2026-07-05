import json
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import update
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
logger = logging.getLogger(__name__)


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
    if job.status == "expired":
        # Cleanup deleted this job's files; "not ready yet" would be misleading.
        raise HTTPException(status_code=410, detail="This job has expired and its files were deleted.")

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
    deleted = 0
    for stored_seg, edit in zip(stored, req.segments):
        if edit.delete:
            deleted += 1
            continue
        # Collapse all interior whitespace (including newlines) to single
        # spaces: a blank line inside a cue would terminate the block early in
        # the SRT file, and the even word-timing split below assumes
        # space-separated words anyway.
        new_text = " ".join(edit.text.split())
        if new_text == stored_seg["text"]:
            updated.append(stored_seg)
            continue
        if not new_text:
            # An empty segment would survive the "≥1 must remain" deletion
            # guard below while rendering as an invisible caption.
            raise HTTPException(
                status_code=400,
                detail="Segment text cannot be empty — delete the segment instead.",
            )
        # Word-level timing can't be reliably re-derived from a free-text edit,
        # so split the edited text into evenly-spaced word slots across the
        # segment's original [start, end] span. Real per-word entries (rather
        # than one giant fake "word") keep ass_generator's per-segment
        # word-group chunking (_iter_word_groups) working sensibly on the
        # edited segment, and let pick_emphasis_word POS-tag real single words
        # instead of a whole sentence.
        seg_start = stored_seg["start"]
        seg_end = stored_seg["end"]
        new_word_strs = new_text.split()
        new_words = []
        if new_word_strs:
            step = max(seg_end - seg_start, 0.01) / len(new_word_strs)
            for i, w in enumerate(new_word_strs):
                new_words.append(
                    {
                        "word": w,
                        "start": seg_start + i * step,
                        "end": seg_start + (i + 1) * step,
                    }
                )
        updated.append(
            {"start": seg_start, "end": seg_end, "text": new_text, "words": new_words}
        )

    if not updated:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete every segment — at least one caption must remain.",
        )

    write_transcript_files(output_dir, updated)

    logger.info(
        "transcript edited: job=%s segments=%d deleted=%d", job_id, len(updated), deleted
    )

    return TranscriptResponse(segments=updated)


@router.post("/jobs/{job_id}/render", response_model=JobStatus, status_code=202)
def render_job(job_id: str, req: RenderRequest, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if req.style not in STYLES:
        raise HTTPException(status_code=400, detail=f"Unknown style: {req.style}")
    if job.status == "expired":
        raise HTTPException(status_code=410, detail="This job has expired and its files were deleted.")

    transcript_path = settings.output_path / job_id / "transcript.json"
    if not transcript_path.exists():
        raise HTTPException(status_code=409, detail="Transcript not ready yet")

    # Atomic compare-and-swap: only start a render from a state that has a
    # finished, final transcript. A plain read-then-write (check job.status,
    # then set it) has a race window where two near-simultaneous requests
    # (e.g. a double-click) can both pass the check and both enqueue
    # render_video for the same job_id, corrupting the shared
    # captions.ass/output.mp4 files. The allowed-set (rather than just
    # != "rendering") also closes the phase-1 window: transcript.json is
    # persisted before silence removal runs, so a still-"transcribing" job
    # would otherwise burn the un-trimmed video with un-remapped timings.
    result = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status.in_(("ready", "complete", "failed")))
        .values(
            style=req.style,
            position_x=req.position_x,
            position_y=req.position_y,
            scale=req.scale,
            status="rendering",
            step="styling",
            progress=55,
            error=None,
            render_started_at=datetime.now(timezone.utc),
            completed_at=None,
        )
    )
    db.commit()
    if result.rowcount == 0:
        db.refresh(job)
        detail = (
            "Job is already rendering"
            if job.status == "rendering"
            else f"Job cannot start a render in its current state ({job.status})"
        )
        raise HTTPException(status_code=409, detail=detail)

    db.refresh(job)
    celery.send_task("render_video", args=[job_id])

    logger.info(
        "render requested: job=%s style=%s pos=(%.2f,%.2f) scale=%.2f",
        job_id, req.style, req.position_x, req.position_y, req.scale,
    )

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
