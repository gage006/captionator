"""
Integration tests for the upload guardrails (size limit, content validation,
active-job cap) — require the Docker stack running with the dev override
(docker-compose.dev.yml pins MAX_UPLOAD_MB=50 and MAX_ACTIVE_JOBS=3 so the
limits are reachable with small fixtures; production defaults are far higher).
"""
import subprocess
import time

import httpx

try:
    from .conftest import upload_sample, wait_for_status
except ImportError:  # pragma: no cover
    from conftest import upload_sample, wait_for_status

BASE_URL = "http://localhost"


def _post_upload_tolerating_cap(
    file_bytes: bytes, filename: str, language: str = "auto"
) -> httpx.Response:
    """Direct POST (no conftest retry helper) that only retries a transient 429.

    The active-job cap check runs before content validation, so on a busy
    stack (e.g. the cold-start pile-up) a content-rejection test could see
    429 instead of its 415. These tests assert content rejections, not the
    cap, so wait out a brief 429 burst; any other status returns as-is.
    """
    deadline = time.time() + 60
    while True:
        r = httpx.post(
            f"{BASE_URL}/api/upload",
            files={"file": (filename, file_bytes, "video/mp4")},
            data={"language": language},
            timeout=30,
        )
        if r.status_code == 429 and time.time() < deadline:
            time.sleep(2)
            continue
        return r


def test_oversized_upload_is_rejected_with_413():
    # 51 MB of zeros: over the dev override's 50 MB cap but far under nginx's
    # 2 GB edge cap, so the rejection observed here is the backend's own
    # Content-Length middleware, not nginx.
    blob = b"\0" * (51 * 1024 * 1024)
    r = httpx.post(
        f"{BASE_URL}/api/upload",
        files={"file": ("big.mp4", blob, "video/mp4")},
        data={"language": "auto"},
        timeout=120,
    )
    assert r.status_code == 413
    assert "too large" in r.json()["detail"].lower()


def test_non_video_upload_is_rejected_with_415():
    r = _post_upload_tolerating_cap(b"this is not a video at all", "fake.mp4")
    assert r.status_code == 415
    assert "video" in r.json()["detail"].lower()


def test_unknown_language_code_is_rejected_with_400(sample_video):
    """An invalid language must fail at upload with a clear 400 — not minutes
    later as a cryptic Whisper ValueError on a job the user already waited on."""
    r = _post_upload_tolerating_cap(
        sample_video.read_bytes(), "sample.mp4", language="klingon"
    )
    assert r.status_code == 400
    assert "language" in r.json()["detail"].lower()


def test_video_without_audio_track_is_rejected_with_415(tmp_path):
    # A real, decodable mp4 — but with no audio stream, so transcription would
    # be impossible. Generated on the fly (trivial and deterministic).
    silent = tmp_path / "no_audio.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:size=320x240:rate=24",
            "-t", "1", "-c:v", "libx264",
            str(silent),
        ],
        capture_output=True,
        check=True,
    )
    r = _post_upload_tolerating_cap(silent.read_bytes(), "no_audio.mp4")
    assert r.status_code == 415


def test_uploads_beyond_active_job_cap_get_429(speech_video):
    # Drain first: upload one job and wait for "ready". The single worker is
    # serial, so once ours is ready, everything queued before it is terminal
    # too — guaranteeing zero active jobs regardless of what earlier tests left.
    drain_id = upload_sample(speech_video)
    wait_for_status(drain_id, "ready")

    # Fill the cap (dev override pins MAX_ACTIVE_JOBS=3). Each upload is
    # sub-second while transcribing ~20s of real speech takes several seconds
    # even with a warm model. (The tone-only sample.mp4 is NOT slow enough:
    # VAD skips it almost instantly, so jobs would drain between uploads.)
    job_ids = [upload_sample(speech_video) for _ in range(3)]

    with open(speech_video, "rb") as f:
        r = httpx.post(
            f"{BASE_URL}/api/upload",
            files={"file": ("speech.mp4", f, "video/mp4")},
            data={"language": "auto"},
            timeout=60,
        )
    assert r.status_code == 429
    assert "busy" in r.json()["detail"].lower()

    # Drain the queue so later tests never see leftover active jobs.
    for job_id in job_ids:
        wait_for_status(job_id, "ready")
