"""
Integration tests for the opt-in "remove silences" pipeline step — require the
Docker stack running on http://localhost (see test_e2e_api.py's docstring).

The fixture (backend/tests/fixtures/sample.mp4) alternates tone/silence segments
with three real silence gaps (~0.8s, ~1.0s, ~0.6s) so silencedetect reliably
fires; see scripts/create_test_fixture.sh.
"""
from pathlib import Path

import httpx
import pytest

try:
    from .conftest import (
        upload_sample,
        upload_sample_with_silence_removal,
        wait_for_status,
        render_job,
        ffprobe_duration,
        download_to_tempfile,
    )
except ImportError:  # pragma: no cover
    from conftest import (
        upload_sample,
        upload_sample_with_silence_removal,
        wait_for_status,
        render_job,
        ffprobe_duration,
        download_to_tempfile,
    )

BASE_URL = "http://localhost"


class TestSilenceRemoval:
    def test_remove_silences_false_by_default(self, sample_video: Path):
        """Backward compatibility: jobs that don't opt in are untouched."""
        job_id = upload_sample(sample_video)
        status = wait_for_status(job_id, "ready")
        assert status["remove_silences"] is False
        assert status["silence_removed_seconds"] is None

    def test_upload_with_remove_silences_trims_video_and_remaps_transcript(
        self, sample_video: Path
    ):
        original_duration = ffprobe_duration(sample_video)

        job_id = upload_sample_with_silence_removal(sample_video)
        status = wait_for_status(job_id, "ready")

        assert status["remove_silences"] is True
        assert status["silence_removed_seconds"] is not None
        assert status["silence_removed_seconds"] > 0
        # The fixture has ~2.4s of total silence (0.8 + 1.0 + 0.6); padding trims
        # a bit back from that, so just assert a meaningful chunk was removed.
        assert status["silence_removed_seconds"] > 1.0

        trimmed_duration = original_duration - status["silence_removed_seconds"]

        # Transcript timings (if any — the fixture has no real speech) must fit
        # within the new, shorter timeline.
        r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}/transcript", timeout=15)
        assert r.status_code == 200
        for seg in r.json()["segments"]:
            assert seg["end"] <= trimmed_duration + 0.5

        # Render and confirm the final burned output is actually shorter than the
        # original — proves the trim survived into the burn step, not just the
        # preview-stage transcript.
        r = render_job(job_id, style="classic")
        assert r.status_code == 202
        wait_for_status(job_id, "complete")

        out_path = download_to_tempfile(job_id)
        try:
            output_duration = ffprobe_duration(out_path)
        finally:
            out_path.unlink(missing_ok=True)

        assert output_duration < original_duration - 1.0

    def test_job_status_includes_silence_fields(self, sample_video: Path):
        job_id = upload_sample(sample_video)
        r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}")
        assert r.status_code == 200
        body = r.json()
        assert "remove_silences" in body
        assert "silence_removed_seconds" in body

    def test_transcript_persists_even_if_silence_removal_fails(
        self, fully_silent_video: Path
    ):
        job_id = upload_sample_with_silence_removal(fully_silent_video)
        status = wait_for_status(job_id, "failed")
        assert "entire video" in (status.get("error") or "").lower()

        # Whisper's transcription succeeded before the silence-removal step
        # failed — that work must not be thrown away.
        r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}/transcript", timeout=15)
        assert r.status_code == 200
