from sqlalchemy import Column, String, Integer, Float, Text, DateTime
from sqlalchemy.sql import func
from .database import Base


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True)
    filename = Column(String, nullable=False)
    # Style is chosen at render time (after preview), not at upload, so it is nullable.
    style = Column(String, nullable=True)
    language = Column(String, default="auto")
    status = Column(String, default="queued")
    step = Column(String, default="uploading")
    progress = Column(Integer, default=0)
    # Caption placement chosen in the preview editor. position_* are the normalized
    # block-center anchor (fraction of video W/H); scale multiplies the style font size.
    position_x = Column(Float, default=0.5)
    position_y = Column(Float, default=0.85)
    scale = Column(Float, default=1.0)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())
    completed_at = Column(DateTime, nullable=True)
