from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class JobStatus(BaseModel):
    model_config = {"from_attributes": True}

    job_id: str
    status: str
    step: str
    progress: int
    style: Optional[str] = None
    position_x: float = 0.5
    position_y: float = 0.85
    scale: float = 1.0
    remove_silences: bool = False
    silence_removed_seconds: Optional[float] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class UploadResponse(BaseModel):
    job_id: str
    status: str
    message: str


class RenderRequest(BaseModel):
    style: str
    position_x: float = Field(0.5, ge=0.0, le=1.0)
    position_y: float = Field(0.85, ge=0.0, le=1.0)
    scale: float = Field(1.0, ge=0.1, le=5.0)


class Word(BaseModel):
    word: str
    start: float
    end: float


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str
    words: list[Word] = []


class TranscriptResponse(BaseModel):
    segments: list[TranscriptSegment]


class TranscriptEditSegment(BaseModel):
    text: str
    # Deletions ride on the full positional list rather than shrinking it: the
    # client flags rows instead of omitting them, so the endpoint's count check
    # still catches a payload built against a stale baseline (which would
    # otherwise delete the wrong segments by shifted position).
    delete: bool = False


class TranscriptEditRequest(BaseModel):
    # Matched to the stored segments by position, so the count must agree with
    # whatever GET /jobs/{job_id}/transcript last returned to the client. Only
    # text is editable here — timing edits aren't part of this feature.
    segments: list[TranscriptEditSegment]


class StyleInfo(BaseModel):
    id: str
    label: str
    description: str
    preview_color: str
    base_font_size: int
