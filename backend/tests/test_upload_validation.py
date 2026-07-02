"""
Integration tests for the upload guardrails (size limit, content validation,
active-job cap) — require the Docker stack running with the dev override
(docker-compose.dev.yml pins MAX_UPLOAD_MB=50 and MAX_ACTIVE_JOBS=3 so the
limits are reachable with small fixtures; production defaults are far higher).
"""
import subprocess

import httpx

BASE_URL = "http://localhost"


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
    r = httpx.post(
        f"{BASE_URL}/api/upload",
        files={"file": ("fake.mp4", b"this is not a video at all", "video/mp4")},
        data={"language": "auto"},
        timeout=30,
    )
    assert r.status_code == 415
    assert "video" in r.json()["detail"].lower()


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
    with open(silent, "rb") as f:
        r = httpx.post(
            f"{BASE_URL}/api/upload",
            files={"file": ("no_audio.mp4", f, "video/mp4")},
            data={"language": "auto"},
            timeout=30,
        )
    assert r.status_code == 415
