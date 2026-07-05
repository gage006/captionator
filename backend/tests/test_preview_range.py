"""
Integration tests for GET /api/preview/{job_id}/source Range handling — require
the Docker stack running on http://localhost (see test_e2e_api.py's docstring).

The preview scrubber's <video> element depends on correct 206 semantics to
seek; the uploaded fixture's bytes are known locally, so every partial
response can be checked byte-for-byte against the real file.
"""
from pathlib import Path

import httpx

try:
    from .conftest import upload_sample
except ImportError:  # pragma: no cover
    from conftest import upload_sample

BASE_URL = "http://localhost"


def _source_url(job_id: str) -> str:
    return f"{BASE_URL}/api/preview/{job_id}/source"


def test_range_requests_serve_the_exact_requested_bytes(sample_video: Path):
    # The preview route only needs the job row + stored file, both in place as
    # soon as the upload returns — no need to wait for transcription.
    job_id = upload_sample(sample_video)
    data = sample_video.read_bytes()
    size = len(data)
    url = _source_url(job_id)

    # No Range: full body, with range support advertised.
    r = httpx.get(url, timeout=30)
    assert r.status_code == 200
    assert r.headers["accept-ranges"] == "bytes"
    assert r.content == data

    # Bounded range.
    r = httpx.get(url, headers={"Range": "bytes=0-99"}, timeout=30)
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 0-99/{size}"
    assert r.content == data[:100]

    # Open-ended range ("from offset to EOF") — what Chrome/Firefox send.
    r = httpx.get(url, headers={"Range": "bytes=100-"}, timeout=30)
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 100-{size - 1}/{size}"
    assert r.content == data[100:]

    # Suffix range ("the final N bytes") — used to fetch a trailing moov atom.
    r = httpx.get(url, headers={"Range": "bytes=-100"}, timeout=30)
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes {size - 100}-{size - 1}/{size}"
    assert r.content == data[-100:]

    # A suffix longer than the file clamps to the whole file.
    r = httpx.get(url, headers={"Range": f"bytes=-{size + 1000}"}, timeout=30)
    assert r.status_code == 206
    assert r.content == data

    # Start beyond EOF is unsatisfiable.
    r = httpx.get(url, headers={"Range": f"bytes={size + 1}-"}, timeout=30)
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{size}"
