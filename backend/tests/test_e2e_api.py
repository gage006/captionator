"""
API integration tests — require the Docker stack to be running on http://localhost.

Start the stack:
    docker-compose -f docker-compose.yml -f docker-compose.dev.yml up -d

Run these tests:
    pytest backend/tests/ -v
"""
import time
from pathlib import Path

import httpx
import pytest

BASE_URL = "http://localhost"
PIPELINE_TIMEOUT = 600

EXPECTED_STYLE_IDS = {
    "classic", "tiktok_bold", "karaoke", "clean_box",
    "neon", "minimal", "cinematic", "duo_tone", "mixed_weight",
}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_backend_is_reachable(self):
        r = httpx.get(f"{BASE_URL}/api/health", timeout=10)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Styles endpoint
# ---------------------------------------------------------------------------

class TestStyles:
    def test_returns_all_styles(self):
        r = httpx.get(f"{BASE_URL}/api/styles")
        assert r.status_code == 200
        styles = r.json()
        assert isinstance(styles, list)
        assert len(styles) == len(EXPECTED_STYLE_IDS)

    def test_style_ids_match_known_set(self):
        r = httpx.get(f"{BASE_URL}/api/styles")
        ids = {s["id"] for s in r.json()}
        assert ids == EXPECTED_STYLE_IDS

    def test_each_style_has_required_fields(self):
        r = httpx.get(f"{BASE_URL}/api/styles")
        for style in r.json():
            for field in ("id", "label", "description", "preview_color"):
                assert field in style, f"Field '{field}' missing from style {style.get('id')}"

    def test_preview_color_is_hex(self):
        r = httpx.get(f"{BASE_URL}/api/styles")
        for style in r.json():
            color = style["preview_color"]
            assert color.startswith("#"), f"preview_color '{color}' is not hex"
            assert len(color) in (4, 7), f"preview_color '{color}' has unexpected length"


# ---------------------------------------------------------------------------
# Upload validation
# ---------------------------------------------------------------------------

class TestUploadValidation:
    def test_rejects_unknown_style(self, sample_video: Path):
        with open(sample_video, "rb") as f:
            r = httpx.post(
                f"{BASE_URL}/api/upload",
                files={"file": ("test.mp4", f, "video/mp4")},
                data={"style": "does_not_exist"},
                timeout=30,
            )
        assert r.status_code == 400

    def test_accepts_valid_upload(self, sample_video: Path):
        with open(sample_video, "rb") as f:
            r = httpx.post(
                f"{BASE_URL}/api/upload",
                files={"file": ("test.mp4", f, "video/mp4")},
                data={"style": "classic"},
                timeout=30,
            )
        assert r.status_code == 202
        body = r.json()
        assert "job_id" in body
        assert len(body["job_id"]) == 8
        assert body["status"] == "queued"
        assert "message" in body

    def test_accepts_explicit_language(self, sample_video: Path):
        with open(sample_video, "rb") as f:
            r = httpx.post(
                f"{BASE_URL}/api/upload",
                files={"file": ("test.mp4", f, "video/mp4")},
                data={"style": "classic", "language": "en"},
                timeout=30,
            )
        assert r.status_code == 202

    def test_accepts_auto_language(self, sample_video: Path):
        with open(sample_video, "rb") as f:
            r = httpx.post(
                f"{BASE_URL}/api/upload",
                files={"file": ("test.mp4", f, "video/mp4")},
                data={"style": "minimal", "language": "auto"},
                timeout=30,
            )
        assert r.status_code == 202


# ---------------------------------------------------------------------------
# Job polling
# ---------------------------------------------------------------------------

class TestJobPolling:
    def test_404_for_unknown_job(self):
        r = httpx.get(f"{BASE_URL}/api/jobs/doesnotexist")
        assert r.status_code == 404

    def test_job_status_has_required_fields(self, sample_video: Path):
        with open(sample_video, "rb") as f:
            r = httpx.post(
                f"{BASE_URL}/api/upload",
                files={"file": ("test.mp4", f, "video/mp4")},
                data={"style": "classic"},
                timeout=30,
            )
        job_id = r.json()["job_id"]
        r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["job_id"] == job_id
        assert body["status"] in ("queued", "processing", "complete", "failed")
        assert isinstance(body["progress"], int)
        assert 0 <= body["progress"] <= 100
        assert "step" in body
        assert "style" in body

    def test_complete_job_has_timestamps(self, completed_job: str):
        r = httpx.get(f"{BASE_URL}/api/jobs/{completed_job}")
        body = r.json()
        assert body["created_at"] is not None
        assert body["completed_at"] is not None

    def test_complete_job_has_100_progress(self, completed_job: str):
        r = httpx.get(f"{BASE_URL}/api/jobs/{completed_job}")
        body = r.json()
        assert body["status"] == "complete"
        assert body["progress"] == 100


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

class TestDownload:
    def test_download_blocked_while_queued(self, sample_video: Path):
        with open(sample_video, "rb") as f:
            r = httpx.post(
                f"{BASE_URL}/api/upload",
                files={"file": ("test.mp4", f, "video/mp4")},
                data={"style": "classic"},
                timeout=30,
            )
        job_id = r.json()["job_id"]
        r = httpx.get(f"{BASE_URL}/api/download/{job_id}/video")
        assert r.status_code == 400

    def test_download_404_for_unknown_job(self):
        r = httpx.get(f"{BASE_URL}/api/download/doesnotexist/video")
        assert r.status_code == 404

    def test_invalid_file_type_returns_400(self, completed_job: str):
        r = httpx.get(f"{BASE_URL}/api/download/{completed_job}/badtype")
        assert r.status_code == 400

    def test_download_video(self, completed_job: str):
        r = httpx.get(f"{BASE_URL}/api/download/{completed_job}/video", timeout=60)
        assert r.status_code == 200
        assert r.headers["content-type"] == "video/mp4"
        assert len(r.content) > 1000  # non-empty MP4

    def test_download_srt(self, completed_job: str):
        r = httpx.get(f"{BASE_URL}/api/download/{completed_job}/srt", timeout=15)
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]

    def test_download_txt(self, completed_job: str):
        r = httpx.get(f"{BASE_URL}/api/download/{completed_job}/txt", timeout=15)
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]

    def test_download_ass(self, completed_job: str):
        r = httpx.get(f"{BASE_URL}/api/download/{completed_job}/ass", timeout=15)
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]

    def test_head_request_works(self, completed_job: str):
        r = httpx.head(f"{BASE_URL}/api/download/{completed_job}/video", timeout=15)
        assert r.status_code == 200

    def test_srt_format(self, completed_job: str):
        r = httpx.get(f"{BASE_URL}/api/download/{completed_job}/srt", timeout=15)
        # SRT files are either empty (silent video produces no speech) or valid SRT
        # Either way the response must be 200 with text content
        assert r.status_code == 200
        text = r.text
        if text.strip():
            # If non-empty, must look like SRT (has --> timing markers or starts with a digit)
            assert "-->" in text or text.strip()[0].isdigit()

    def test_ass_has_script_info(self, completed_job: str):
        r = httpx.get(f"{BASE_URL}/api/download/{completed_job}/ass", timeout=15)
        assert "[Script Info]" in r.text
        assert "[V4+ Styles]" in r.text
        assert "[Events]" in r.text


# ---------------------------------------------------------------------------
# Compound style pipeline
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("style_id", ["duo_tone", "mixed_weight"])
def test_compound_style_pipeline_completes(sample_video: Path, style_id: str):
    """Compound styles have unique ASS-generation codepaths — each deserves a full run."""
    with open(sample_video, "rb") as f:
        r = httpx.post(
            f"{BASE_URL}/api/upload",
            files={"file": ("test.mp4", f, "video/mp4")},
            data={"style": style_id},
            timeout=60,
        )
    assert r.status_code == 202, f"Upload failed for style '{style_id}': {r.text}"
    job_id = r.json()["job_id"]

    deadline = time.time() + PIPELINE_TIMEOUT
    while time.time() < deadline:
        r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}", timeout=10)
        status = r.json()
        if status["status"] == "complete":
            assert status["progress"] == 100
            r_ass = httpx.get(f"{BASE_URL}/api/download/{job_id}/ass", timeout=15)
            assert r_ass.status_code == 200
            return
        if status["status"] == "failed":
            pytest.fail(f"Style '{style_id}' pipeline failed: {status.get('error')}")
        time.sleep(2)

    pytest.fail(f"Style '{style_id}' job {job_id} timed out after {PIPELINE_TIMEOUT}s")
