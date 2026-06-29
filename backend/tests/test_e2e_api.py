"""
API integration tests — require the Docker stack to be running on http://localhost.

Start the stack:
    docker-compose -f docker-compose.yml -f docker-compose.dev.yml up -d

Run these tests:
    pytest backend/tests/ -v

The flow is two-phase: POST /api/upload only transcribes (status -> "ready"), then
POST /api/jobs/{id}/render burns the chosen style/placement (-> "complete").
"""
import time
from pathlib import Path

import httpx
import pytest

try:  # works whether pytest imports this as a package module or a top-level one
    from .conftest import upload_sample, wait_for_status, render_job
except ImportError:  # pragma: no cover
    from conftest import upload_sample, wait_for_status, render_job

BASE_URL = "http://localhost"
PIPELINE_TIMEOUT = 600

EXPECTED_STYLE_IDS = {
    "classic", "tiktok_bold", "karaoke", "clean_box",
    "neon", "minimal", "cinematic", "duo_tone", "mixed_weight",
}

# Every status the API can report across both phases.
VALID_STATUSES = {"transcribing", "ready", "rendering", "complete", "failed", "expired"}


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
            for field in ("id", "label", "description", "preview_color", "base_font_size"):
                assert field in style, f"Field '{field}' missing from style {style.get('id')}"

    def test_preview_color_is_hex(self):
        r = httpx.get(f"{BASE_URL}/api/styles")
        for style in r.json():
            color = style["preview_color"]
            assert color.startswith("#"), f"preview_color '{color}' is not hex"
            assert len(color) in (4, 7), f"preview_color '{color}' has unexpected length"


# ---------------------------------------------------------------------------
# Upload (phase 1) — no style here; just stores the video and starts transcription
# ---------------------------------------------------------------------------

class TestUpload:
    def test_accepts_valid_upload(self, sample_video: Path):
        with open(sample_video, "rb") as f:
            r = httpx.post(
                f"{BASE_URL}/api/upload",
                files={"file": ("test.mp4", f, "video/mp4")},
                timeout=30,
            )
        assert r.status_code == 202
        body = r.json()
        assert "job_id" in body
        assert len(body["job_id"]) == 32  # full uuid4 hex, not a truncated id
        assert body["status"] == "transcribing"
        assert "message" in body

    def test_accepts_explicit_language(self, sample_video: Path):
        with open(sample_video, "rb") as f:
            r = httpx.post(
                f"{BASE_URL}/api/upload",
                files={"file": ("test.mp4", f, "video/mp4")},
                data={"language": "en"},
                timeout=30,
            )
        assert r.status_code == 202

    def test_path_traversal_filename_is_neutralized(self, sample_video: Path):
        """A malicious filename must not escape the job directory — the upload
        should still succeed and produce a normal, downloadable job."""
        with open(sample_video, "rb") as f:
            r = httpx.post(
                f"{BASE_URL}/api/upload",
                files={"file": ("../../../../etc/pwned.mp4", f, "video/mp4")},
                timeout=30,
            )
        assert r.status_code == 202
        assert len(r.json()["job_id"]) == 32


# ---------------------------------------------------------------------------
# Render (phase 2) — style is validated and chosen here
# ---------------------------------------------------------------------------

class TestRender:
    def test_rejects_unknown_style(self, ready_job: str):
        r = render_job(ready_job, style="does_not_exist")
        assert r.status_code == 400

    def test_accepts_valid_style(self, ready_job: str):
        r = render_job(ready_job, style="classic")
        assert r.status_code == 202
        assert r.json()["status"] == "rendering"

    def test_404_for_unknown_job(self):
        r = render_job("doesnotexist", style="classic")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Job polling
# ---------------------------------------------------------------------------

class TestJobPolling:
    def test_404_for_unknown_job(self):
        r = httpx.get(f"{BASE_URL}/api/jobs/doesnotexist")
        assert r.status_code == 404

    def test_job_status_has_required_fields(self, sample_video: Path):
        job_id = upload_sample(sample_video)
        r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["job_id"] == job_id
        assert body["status"] in VALID_STATUSES
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
    def test_download_blocked_before_complete(self, sample_video: Path):
        # A freshly uploaded job is only transcribing/ready, never "complete".
        job_id = upload_sample(sample_video)
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
    job_id = upload_sample(sample_video)
    wait_for_status(job_id, "ready")

    r = render_job(job_id, style=style_id)
    assert r.status_code == 202, f"Render failed for style '{style_id}': {r.text}"

    wait_for_status(job_id, "complete")
    r_ass = httpx.get(f"{BASE_URL}/api/download/{job_id}/ass", timeout=15)
    assert r_ass.status_code == 200
