"""
Integration tests for GET /api/preview/{job_id}/source Range handling — require
the Docker stack running on http://localhost (see test_e2e_api.py's docstring).

The preview scrubber's <video> element depends on correct 206 semantics to
seek. The served file is no longer byte-identical to the upload (phase 1
faststart-remuxes browser-safe sources in place, and swaps in a preview.mp4
sidecar for browser-unsafe codecs), so the test waits for "ready" — after
which the served file is stable — and checks every partial response
byte-for-byte against the full served body.
"""
from pathlib import Path

import httpx

try:
    from .conftest import upload_sample, wait_for_status
except ImportError:  # pragma: no cover
    from conftest import upload_sample, wait_for_status

BASE_URL = "http://localhost"


def _source_url(job_id: str) -> str:
    return f"{BASE_URL}/api/preview/{job_id}/source"


def test_range_requests_serve_the_exact_requested_bytes(sample_video: Path):
    # Wait for phase 1: until "ready" the worker may still rewrite the source
    # (in-place faststart remux), which would change the file mid-test.
    job_id = upload_sample(sample_video)
    wait_for_status(job_id, "ready")
    url = _source_url(job_id)

    # No Range: full body, with range support advertised. This body is the
    # reference for every partial response below — the ftyp box at offset 4
    # confirms it's still a real MP4 container.
    r = httpx.get(url, timeout=30)
    assert r.status_code == 200
    assert r.headers["accept-ranges"] == "bytes"
    data = r.content
    size = len(data)
    assert size > 0
    assert data[4:8] == b"ftyp"

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

    # The served preview must be moov-first (faststart) so browsers get
    # metadata without fetching the tail — the whole point of the remux.
    head = httpx.get(url, headers={"Range": "bytes=0-63"}, timeout=30).content
    assert b"moov" in head, "served preview should have a leading moov box"
