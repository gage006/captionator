from fastapi import HTTPException
from sqlalchemy.orm import Session
from ..models import Job


def load_job(job_id: str, db: Session, *, allow_expired: bool = False) -> Job:
    """Look up a job or raise the right error for its absence.

    Every route that serves a job's files rejects expired jobs with the same
    410 — cleanup has deleted the files, so "not ready/complete yet" would be
    misleading. Routes that only *report* on the job (status; the transcript
    editor's own ready-state 409) pass allow_expired=True and handle the
    status themselves.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not allow_expired and job.status == "expired":
        raise HTTPException(
            status_code=410, detail="This job has expired and its files were deleted."
        )
    return job
