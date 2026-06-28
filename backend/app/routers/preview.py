import mimetypes
import re
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, Response
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import Job

router = APIRouter()

_CHUNK = 1024 * 1024
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


@router.api_route("/preview/{job_id}/source", methods=["GET", "HEAD"])
def preview_source(job_id: str, request: Request, db: Session = Depends(get_db)):
    """Stream the uploaded source video for the preview scrubber.

    Served inline (not as an attachment) with explicit HTTP Range support so the
    browser <video> element can seek without downloading the whole file. (This
    version of Starlette's FileResponse does not emit 206 on its own, so we handle
    Range here.) Distinct from download.py, which serves only finished outputs.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    path = Path(job.filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Source video not found")

    file_size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    base_headers = {"accept-ranges": "bytes", "content-type": media_type}

    # HEAD: advertise size + range support, no body.
    if request.method == "HEAD":
        return Response(
            status_code=200,
            headers={**base_headers, "content-length": str(file_size)},
        )

    range_header = request.headers.get("range")
    match = _RANGE_RE.fullmatch(range_header.strip()) if range_header else None

    if match:
        start = int(match.group(1)) if match.group(1) else 0
        end = int(match.group(2)) if match.group(2) else file_size - 1
        end = min(end, file_size - 1)
        if start > end or start >= file_size:
            return Response(
                status_code=416,
                headers={"content-range": f"bytes */{file_size}", "accept-ranges": "bytes"},
            )

        length = end - start + 1

        def stream_range():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(_CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            stream_range(),
            status_code=206,
            headers={
                **base_headers,
                "content-length": str(length),
                "content-range": f"bytes {start}-{end}/{file_size}",
            },
        )

    # No Range header: full file, but advertise range support for the browser.
    return FileResponse(path=str(path), media_type=media_type, headers={"accept-ranges": "bytes"})
