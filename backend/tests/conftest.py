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


# ---------------------------------------------------------------------------
# Helpers. The flow is two-phase now: upload only transcribes (status -> "ready");
# a separate render call burns the captions (-> "complete").
# ---------------------------------------------------------------------------

def upload_sample(sample_video: Path, language: str = "auto") -> str:
    """Upload the fixture and return its job_id. Style is NOT chosen here anymore."""
    with open(sample_video, "rb") as f:
        r = httpx.post(
            f"{BASE_URL}/api/upload",
            files={"file": ("sample.mp4", f, "video/mp4")},
            data={"language": language},
            timeout=60,
        )
    r.raise_for_status()
    return r.json()["job_id"]


def wait_for_status(job_id: str, target: str, timeout: int = PIPELINE_TIMEOUT) -> dict:
    """Poll a job until it reaches `target` (or a terminal failure) and return its body."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}", timeout=10)
        r.raise_for_status()
        status = r.json()
        if status["status"] == target:
            return status
        if status["status"] in ("failed", "expired"):
            pytest.fail(
                f"Job {job_id} reached {status['status']} while waiting for "
                f"{target}: {status.get('error')}"
            )
        time.sleep(2)
    pytest.fail(f"Job {job_id} did not reach {target} within {timeout}s")


def render_job(job_id: str, style: str = "classic") -> httpx.Response:
    """Kick off phase 2 (burn) for an already-transcribed job."""
    return httpx.post(
        f"{BASE_URL}/api/jobs/{job_id}/render",
        json={"style": style, "position_x": 0.5, "position_y": 0.85, "scale": 1.0},
        timeout=30,
    )


@pytest.fixture(scope="session")
def sample_video() -> Path:
    if not FIXTURE.exists():
        pytest.skip(
            f"Test fixture not found: {FIXTURE}. Run: bash scripts/create_test_fixture.sh"
        )
    return FIXTURE


@pytest.fixture
def ready_job(sample_video: Path) -> str:
    """A freshly uploaded + transcribed job (status 'ready'), not yet rendered.
    Function-scoped so render-endpoint tests each get an untouched job to mutate."""
    job_id = upload_sample(sample_video)
    wait_for_status(job_id, "ready")
    return job_id


@pytest.fixture(scope="session")
def completed_job(sample_video: Path) -> str:
    """Drive one upload through BOTH phases (transcribe -> render) and return its
    job_id. Session-scoped so the (slow) pipeline runs only once for the suite."""
    job_id = upload_sample(sample_video)
    wait_for_status(job_id, "ready")
    r = render_job(job_id, style="classic")
    if r.status_code != 202:
        pytest.fail(f"Render request failed for {job_id}: {r.status_code} {r.text}")
    wait_for_status(job_id, "complete")
    return job_id
