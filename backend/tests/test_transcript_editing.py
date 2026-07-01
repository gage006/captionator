"""
Integration tests for PUT /api/jobs/{job_id}/transcript — require the Docker
stack running on http://localhost (see test_e2e_api.py's docstring).
"""
import math
from pathlib import Path

import httpx
import pytest

try:
    from .conftest import upload_sample, wait_for_status, render_job
except ImportError:  # pragma: no cover
    from conftest import upload_sample, wait_for_status, render_job

BASE_URL = "http://localhost"
SPEECH_FIXTURE = Path(__file__).parent / "fixtures" / "real_speech_synthetic.mp4"
KEYWORD_POP_WORDS_PER_GROUP = 4  # backend/app/styles/definitions.py "keyword_pop"


@pytest.fixture(scope="module")
def speech_video() -> Path:
    if not SPEECH_FIXTURE.exists():
        pytest.skip(
            f"Fixture not found: {SPEECH_FIXTURE}. "
            "Run: bash scripts/create_real_speech_fixtures.sh"
        )
    return SPEECH_FIXTURE


def test_editing_a_segment_gives_it_real_per_word_timing(speech_video: Path):
    job_id = upload_sample(speech_video)
    wait_for_status(job_id, "ready")

    r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}/transcript", timeout=15)
    assert r.status_code == 200
    segments = r.json()["segments"]
    assert segments, "expected at least one transcribed segment"

    texts = [s["text"] for s in segments]
    texts[0] = "Hi there friend"  # 3 words, almost certainly not the original count

    r = httpx.put(
        f"{BASE_URL}/api/jobs/{job_id}/transcript",
        json={"segments": [{"text": t} for t in texts]},
        timeout=15,
    )
    assert r.status_code == 200
    updated = r.json()["segments"]

    # The bug this guards against: editing used to collapse the whole edited
    # text into ONE fake "word" spanning the segment. Real per-word entries
    # are required for sensible chunking/emphasis on the edited segment.
    assert len(updated[0]["words"]) == 3
    assert [w["word"] for w in updated[0]["words"]] == ["Hi", "there", "friend"]
    # Each word gets its own non-overlapping, monotonically increasing slice
    # of the segment's original [start, end] span.
    for w1, w2 in zip(updated[0]["words"], updated[0]["words"][1:]):
        assert w1["end"] == w2["start"]
    assert updated[0]["words"][0]["start"] == segments[0]["start"]
    assert updated[0]["words"][-1]["end"] == pytest.approx(segments[0]["end"])


def test_editing_one_segment_does_not_shift_chunking_in_other_segments(
    speech_video: Path,
):
    job_id = upload_sample(speech_video)
    wait_for_status(job_id, "ready")

    r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}/transcript", timeout=15)
    segments = r.json()["segments"]
    if len(segments) < 2:
        pytest.skip("fixture produced only one segment this run; nothing to isolate")

    texts = [s["text"] for s in segments]
    original_word_count = len(segments[0]["words"])
    # Pick an edit whose word count differs from the original segment's word
    # count, so a global (bug) chunking scheme and a per-segment (fixed)
    # chunking scheme would disagree on where later segments' groups start.
    texts[0] = " ".join(["word"] * (original_word_count + 3))

    r = httpx.put(
        f"{BASE_URL}/api/jobs/{job_id}/transcript",
        json={"segments": [{"text": t} for t in texts]},
        timeout=15,
    )
    assert r.status_code == 200
    updated = r.json()["segments"]

    r = render_job(job_id, style="keyword_pop")
    assert r.status_code == 202
    wait_for_status(job_id, "complete")

    ass_text = httpx.get(f"{BASE_URL}/api/download/{job_id}/ass", timeout=15).text
    dialogue_lines = [l for l in ass_text.splitlines() if l.startswith("Dialogue:")]

    # Expected line count derived straight from the ACTUAL (post-edit)
    # transcript, assuming each segment's words are chunked independently.
    # Under the pre-fix global-flatten behavior, this count would be wrong
    # for every segment after the edited one.
    expected_lines = sum(
        math.ceil(len(seg["words"]) / KEYWORD_POP_WORDS_PER_GROUP)
        for seg in updated
        if seg["words"]
    )
    assert len(dialogue_lines) == expected_lines
