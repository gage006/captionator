import mimetypes
import re
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, Response
from sqlalchemy.orm import Session
from ..config import settings
from ..database import get_db
from ..tasks.transcode import PREVIEW_FILENAME
from .common import load_job

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
    # Expired jobs 410 like the other file-serving routes (their source is deleted).
    job = load_job(job_id, db)

    # When the source codec isn't browser-decodable (HEVC phone footage),
    # phase 1 leaves an H.264 sidecar next to the upload — stream that instead.
    # The original stays the render source; this file exists only for the
    # <video> element.
    path = settings.upload_path / job_id / PREVIEW_FILENAME
    if not path.exists():
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
    # "bytes=-" specifies nothing; per RFC 9110 an unsatisfiable/invalid Range
    # is ignored and the full file served.
    if match and not (match.group(1) or match.group(2)):
        match = None

    if match:
        if match.group(1):
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else file_size - 1
        else:
            # Suffix form "bytes=-N": the final N bytes (e.g. a player fetching
            # a trailing moov atom), not bytes 0..N.
            start = max(0, file_size - int(match.group(2)))
            end = file_size - 1
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
