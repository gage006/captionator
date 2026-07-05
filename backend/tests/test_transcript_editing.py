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


def test_deleting_a_segment_drops_it_and_rejects_stale_resubmission(
    speech_video: Path,
):
    job_id = upload_sample(speech_video)
    wait_for_status(job_id, "ready")

    r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}/transcript", timeout=15)
    segments = r.json()["segments"]
    if len(segments) < 2:
        pytest.skip("fixture produced only one segment this run; nothing to delete")

    # Deleting every segment must be rejected — an empty transcript would
    # break downloads and produce a captionless render.
    r = httpx.put(
        f"{BASE_URL}/api/jobs/{job_id}/transcript",
        json={"segments": [{"text": s["text"], "delete": True} for s in segments]},
        timeout=15,
    )
    assert r.status_code == 400

    stale_payload = {"segments": [{"text": s["text"]} for s in segments]}

    # Delete just the first segment; survivors must come back byte-identical
    # (deletion must never re-time or otherwise disturb the segments it keeps).
    edits = [{"text": s["text"]} for s in segments]
    edits[0]["delete"] = True
    r = httpx.put(
        f"{BASE_URL}/api/jobs/{job_id}/transcript",
        json={"segments": edits},
        timeout=15,
    )
    assert r.status_code == 200
    updated = r.json()["segments"]
    assert len(updated) == len(segments) - 1
    assert updated == segments[1:]

    r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}/transcript", timeout=15)
    assert r.json()["segments"] == segments[1:]

    # A resubmission built against the pre-delete baseline now has the wrong
    # count — it must 400 rather than silently deleting by shifted position.
    r = httpx.put(
        f"{BASE_URL}/api/jobs/{job_id}/transcript", json=stale_payload, timeout=15
    )
    assert r.status_code == 400


def test_blanking_a_segment_text_is_rejected(speech_video: Path):
    """Whitespace-only text must 400 rather than creating an invisible caption:
    an empty segment would survive the "≥1 must remain" deletion guard while
    contributing nothing to the burn. Deletion has an explicit flag instead."""
    job_id = upload_sample(speech_video)
    wait_for_status(job_id, "ready")

    r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}/transcript", timeout=15)
    segments = r.json()["segments"]
    assert segments, "expected at least one transcribed segment"

    edits = [{"text": s["text"]} for s in segments]
    edits[0]["text"] = "   \n  "
    r = httpx.put(
        f"{BASE_URL}/api/jobs/{job_id}/transcript",
        json={"segments": edits},
        timeout=15,
    )
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()

    # The rejected edit must not have touched the stored transcript.
    r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}/transcript", timeout=15)
    assert r.json()["segments"] == segments


def test_braces_and_newlines_in_edited_text_survive_render_intact(speech_video: Path):
    """Edited text containing `{`, `}` or newlines must not corrupt the ASS file.

    Inside an ASS Dialogue line, `{...}` is a live override-tag block (a typed
    `{\\an1}` would really re-position the caption) and a raw newline
    terminates the line, silently dropping the rest of the text from the burn.
    Both are typable in the editor textareas today.
    """
    job_id = upload_sample(speech_video)
    wait_for_status(job_id, "ready")

    r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}/transcript", timeout=15)
    segments = r.json()["segments"]
    assert segments, "expected at least one transcribed segment"

    edits = [{"text": s["text"]} for s in segments]
    edits[0]["text"] = "hello {\\an1}brace\nsecond line"

    r = httpx.put(
        f"{BASE_URL}/api/jobs/{job_id}/transcript",
        json={"segments": edits},
        timeout=15,
    )
    assert r.status_code == 200

    r = render_job(job_id, style="classic")
    assert r.status_code == 202
    wait_for_status(job_id, "complete")

    ass_text = httpx.get(f"{BASE_URL}/api/download/{job_id}/ass", timeout=15).text
    events = ass_text.split("[Events]", 1)[1]

    # A raw newline in the text would split its Dialogue line, leaving the
    # remainder as a stray non-Dialogue line that libass silently drops.
    stray = [
        line
        for line in events.splitlines()
        if line.strip() and not line.startswith(("Format:", "Dialogue:"))
    ]
    assert stray == [], f"caption text leaked onto its own line(s): {stray}"

    dialogue_lines = [l for l in events.splitlines() if l.startswith("Dialogue:")]
    # classic emits one Dialogue line per segment.
    assert len(dialogue_lines) == len(segments)
    joined = "\n".join(dialogue_lines)
    # The typed brace tag must be neutralized (escaped), not live ASS syntax.
    assert "{\\an1}" not in joined
    # And none of the words the user typed may be lost.
    for word in ("hello", "brace", "second", "line"):
        assert word in joined


def test_deleted_segment_is_absent_from_rendered_captions(speech_video: Path):
    job_id = upload_sample(speech_video)
    wait_for_status(job_id, "ready")

    r = httpx.get(f"{BASE_URL}/api/jobs/{job_id}/transcript", timeout=15)
    segments = r.json()["segments"]
    if len(segments) < 2:
        pytest.skip("fixture produced only one segment this run; nothing to delete")

    edits = [{"text": s["text"]} for s in segments]
    edits[0]["delete"] = True
    r = httpx.put(
        f"{BASE_URL}/api/jobs/{job_id}/transcript",
        json={"segments": edits},
        timeout=15,
    )
    assert r.status_code == 200

    r = render_job(job_id, style="classic")
    assert r.status_code == 202
    wait_for_status(job_id, "complete")

    ass_text = httpx.get(f"{BASE_URL}/api/download/{job_id}/ass", timeout=15).text
    dialogue_lines = [l for l in ass_text.splitlines() if l.startswith("Dialogue:")]

    # classic emits exactly one Dialogue line per segment, so the burn must
    # contain one line per SURVIVING segment and none with the deleted text.
    assert len(dialogue_lines) == len(segments) - 1
    deleted_text = segments[0]["text"]
    assert all(deleted_text not in line for line in dialogue_lines)
