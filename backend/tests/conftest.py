import sys
import time
from pathlib import Path

import httpx
import pytest

BASE_URL = "http://localhost"
FIXTURE = Path(__file__).parent / "fixtures" / "sample.mp4"
PIPELINE_TIMEOUT = 600  # seconds — single worker processes ~4 queued jobs before reaching ours


def pytest_configure(config):
    if not FIXTURE.exists():
        print(
            f"\nWARNING: Test fixture missing: {FIXTURE}\n"
            "Run:  bash scripts/create_test_fixture.sh\n"
            "Pipeline tests will be skipped.\n",
            file=sys.stderr,
        )


@pytest.fixture(scope="session")
def sample_video() -> Path:
    if not FIXTURE.exists():
        pytest.skip(
            f"Test fixture not found: {FIXTURE}. Run: bash scripts/create_test_fixture.sh"
        )
    return FIXTURE


@pytest.fixture(scope="session")
def completed_job(sample_video: Path) -> str:
    """Upload sample.mp4 with 'classic' style and wait for the job to complete.
    Cached for the whole session so the pipeline only runs once for this fixture."""
    with open(sample_video, "rb") as f:
        r = httpx.post(
            f"{BASE_URL}/api/upload",
            files={"file": ("sample.mp4", f, "video/mp4")},
            data={"style": "classic", "language": "auto"},
            timeout=60,
        )
    r.raise_for_status()
    job_id = r.json()["job_id"]

    deadline = time.time() + PIPELINE_TIMEOUT
    while time.time() < deadline:
        r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}", timeout=10)
        r.raise_for_status()
        status = r.json()
        if status["status"] == "complete":
            return job_id
        if status["status"] == "failed":
            pytest.fail(f"Job {job_id} failed during fixture setup: {status.get('error')}")
        time.sleep(2)

    pytest.fail(f"Job {job_id} did not complete within {PIPELINE_TIMEOUT}s")
