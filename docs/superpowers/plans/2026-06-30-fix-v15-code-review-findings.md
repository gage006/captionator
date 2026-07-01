# Fix v1.5 Code Review Findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 10 findings from the `/code-review` run on captionator's v1.5 commit (`15789f9`), without regressing any currently-passing behavior.

**Architecture:** Nine independently-committable tasks, each touching one cohesive area (ASS rendering, transcript editing, pipeline persistence ordering, render concurrency, frontend gating, DB migration safety, config validation, silence-trim progress math, docs). Backend fixes are verified the way this repo already tests everything — httpx against the live Docker stack (`backend/tests/`) or Playwright against the built frontend (`e2e/tests/`) — never by importing backend internals into a unit test, which would break the project's established "no unit tests, e2e only" convention (see `CLAUDE.md` and `backend/tests/conftest.py`'s own comment on this).

**Tech Stack:** FastAPI + SQLAlchemy + Celery + faster-whisper (backend), React + TypeScript + Vite (frontend), pytest + httpx (backend e2e tests), Playwright (browser e2e tests), Docker Compose (runtime).

## Global Constraints

- Every backend test must drive the live stack over HTTP (httpx against `http://localhost`), matching `backend/tests/conftest.py`'s existing helpers (`upload_sample`, `wait_for_status`, `render_job`, etc.). Do not import `app.*` modules into a test file.
- Every frontend behavior test must use Playwright against the built app (`e2e/tests/upload-flow.spec.ts`'s existing patterns). Do not add a new frontend unit-test framework.
- Preserve the exact JSON shape of `JobStatus`, `TranscriptResponse`, and `RenderRequest` — the frontend depends on these field names as-is.
- Preserve all 10 caption style IDs. Preserve current visual behavior for content that doesn't hit the bug being fixed, **except** where the fix's own mechanism necessarily changes it: Task 1's per-segment word-group chunking is the root-cause fix for the edit-ripple bug, and by construction it also pins chunk boundaries at every segment edge for compound/keyword-emphasis styles on **any** multi-segment video, edited or not (confirmed during Task 1's review: this is an accepted, intentional side effect, not a regression — a caption line can no longer glue together words spoken before and after a natural pause between segments, which is arguably more correct than the old global-flatten behavior).
- Follow existing code style: minimal comments, only explaining non-obvious *why*, matching the tone already used in each file.
- Run `cd frontend && npx tsc --noEmit` after any `.tsx`/`.ts` change and confirm it's clean before committing.
- The Docker stack must be running (`docker compose up -d`, rebuilt with `docker compose up -d --build backend nginx` after backend/frontend code changes) before running any test in `backend/tests/` or `e2e/tests/`.

## File Structure

| File | Change |
|---|---|
| `backend/app/tasks/ass_generator.py` | Modify — replace `_flatten_words` + two independent chunking loops with one shared `_iter_word_groups` helper that chunks per-segment |
| `backend/app/routers/jobs.py` | Modify — `update_transcript` splits edited text into real per-word entries; `render_job` becomes an atomic compare-and-swap on `status` |
| `backend/app/tasks/pipeline.py` | Modify — persist the transcript immediately after transcription (and again after a successful trim); `write_transcript_files` writes atomically via temp-file + rename |
| `backend/tests/test_transcript_editing.py` | Create — regression test proving an edit can't shift word-group chunking in other segments |
| `backend/tests/conftest.py` | Modify — add a `fully_silent_video` fixture (generated on the fly via ffmpeg, not committed as a binary) |
| `backend/tests/test_silence_removal.py` | Modify — add the "transcript persists even if silence removal fails" test (Task 3). Task 8's fix has no automated test — see Task 8 for why |
| `backend/tests/test_e2e_api.py` | Modify — add the "second render request while one is in flight gets 409" test |
| `frontend/src/components/TranscriptEditor.tsx` | Modify — report busy/dirty state up via a new `onBusyChange` prop |
| `frontend/src/components/PreviewEditor.tsx` | Modify — disable "Render with this style" while the transcript editor reports busy |
| `e2e/tests/upload-flow.spec.ts` | Modify — add a test asserting the Render button disables while a transcript edit is unsaved |
| `backend/app/database.py` | Modify — add a SQL `DEFAULT` clause (and backfill for already-NULL rows) to the additive migration; raise the SQLite connection lock `timeout` |
| `backend/app/config.py` | Modify — validate `silence_max_segments >= 1` |
| `backend/app/tasks/silence.py` | Modify — `trim_silences` computes its progress denominator from `kept_ranges`, not a re-probe of the original file |
| `CLAUDE.md` | Modify — fix the stale `90..99%` progress claim and stale `DownloadPanel.tsx` module-map row |

---

### Task 1: `ass_generator.py` — shared per-segment word-group chunking

**Files:**
- Modify: `backend/app/tasks/ass_generator.py:42-158`

**Interfaces:**
- Produces: `_iter_word_groups(segments: list, wpg: int) -> Iterator[list[dict]]` — used by Task 2's test and by both `_build_compound_events` and `_build_keyword_emphasis_events` below.
- Removes: `_flatten_words` (no longer used anywhere after this task).

This is the root-cause fix for review finding #1 (Keyword Pop/Duo Tone/Mixed Weight desync after any transcript edit) and finding #10 (duplicated chunking logic between the two `_build_*_events` functions). Chunking words *within* each segment independently — instead of flattening every segment's words into one global list first — means an edit that changes one segment's word count can only ever change that segment's own line count. It can no longer shift chunk boundaries anywhere else in the video.

- [ ] **Step 1: Replace `_flatten_words` with `_iter_word_groups`**

In `backend/app/tasks/ass_generator.py`, delete the `_flatten_words` function (lines 42-50) and replace it with:

```python
def _iter_word_groups(segments: list, wpg: int):
    """Yield each segment's words chunked into fixed-size groups of `wpg`.

    Chunking happens independently per segment (never spanning a segment
    boundary) so editing one segment's text can only ever change that
    segment's own group count — it can never shift chunk boundaries anywhere
    else in the transcript.
    """
    for seg in segments:
        words = [
            {"word": w["word"].strip(), "start": w["start"], "end": w["end"]}
            for w in seg.get("words", [])
            if w["word"].strip()
        ]
        for i in range(0, len(words), wpg):
            chunk = words[i : i + wpg]
            if chunk:
                yield chunk
```

- [ ] **Step 2: Rewrite `_build_compound_events` to consume the shared iterator**

Replace the whole function (currently lines 74-117) with:

```python
def _build_compound_events(
    segments: list, style: dict, style_name: str, pos_prefix: str = ""
) -> list:
    """Render a compound style: group words into fixed-size chunks and apply
    inline override tags to the trailing 'accent' words of each chunk."""
    wpg = style.get("words_per_group", 4)
    split_after = style.get("split_after", wpg // 2)
    accent = style.get("accent", {})
    two_line = style.get("two_line", True)

    open_tags = _build_accent_open_tags(accent)
    close_tags = "{\\r}" if open_tags else ""
    # \\N in Python source → \N in the string → ASS hard line break in the file
    separator = "\\N" if two_line else " "

    events = []
    for chunk in _iter_word_groups(segments, wpg):
        start = _format_ass_time(chunk[0]["start"])
        end = _format_ass_time(chunk[-1]["end"])

        base_part = " ".join(w["word"] for w in chunk[:split_after])
        accent_part = " ".join(w["word"] for w in chunk[split_after:])

        if base_part and accent_part:
            text = f"{base_part}{separator}{open_tags}{accent_part}{close_tags}"
        elif accent_part:
            text = f"{open_tags}{accent_part}{close_tags}"
        else:
            text = base_part

        if text:
            events.append(
                f"Dialogue: 0,{start},{end},{style_name},,0,0,0,,{pos_prefix}{text}"
            )

    return events
```

- [ ] **Step 3: Rewrite `_build_keyword_emphasis_events` to consume the shared iterator**

Replace the whole function (currently lines 120-158) with:

```python
def _build_keyword_emphasis_events(
    segments: list, style: dict, style_name: str, pos_prefix: str = ""
) -> list:
    """Render each fixed-size word group as a single line, popping the size of
    one semantically meaningful word (noun/verb/adjective, picked by
    emphasis.pick_emphasis_word) instead of a fixed trailing block. Falls back
    to no emphasis for a group that has no content word."""
    wpg = style.get("words_per_group", 4)
    accent = style.get("accent", {})
    open_tags = _build_accent_open_tags(accent)
    close_tags = "{\\r}" if open_tags else ""

    events = []
    for chunk in _iter_word_groups(segments, wpg):
        start = _format_ass_time(chunk[0]["start"])
        end = _format_ass_time(chunk[-1]["end"])

        emphasis_idx = pick_emphasis_word([w["word"] for w in chunk])
        parts = []
        for idx, w in enumerate(chunk):
            if idx == emphasis_idx:
                parts.append(f"{open_tags}{w['word']}{close_tags}")
            else:
                parts.append(w["word"])
        text = " ".join(parts)

        events.append(
            f"Dialogue: 0,{start},{end},{style_name},,0,0,0,,{pos_prefix}{text}"
        )

    return events
```

- [ ] **Step 4: Rebuild the backend image and confirm existing compound-style pipeline tests still pass**

```bash
docker compose up -d --build backend worker
bash scripts/create_test_fixture.sh   # only if backend/tests/fixtures/sample.mp4 is missing
pytest backend/tests/test_e2e_api.py -k "compound_style" -v
```

Expected: `3 passed` (`duo_tone`, `mixed_weight`, `keyword_pop` all still complete and produce a downloadable `.ass` file — `sample.mp4` has no real speech, so this only proves no regression on the empty-transcript path already covered by finding no words to chunk).

- [ ] **Step 5: Commit**

```bash
git add backend/app/tasks/ass_generator.py
git commit -m "fix: chunk caption word-groups per-segment, not globally

Fixes a bug where editing one transcript segment's word count would
silently shift word-group chunk boundaries for every segment after it
in Duo Tone / Mixed Weight / Keyword Pop styles. Also removes the
duplicated chunking loop between _build_compound_events and
_build_keyword_emphasis_events by extracting _iter_word_groups."
```

---

### Task 2: `jobs.py` — real per-word transcript edits

**Files:**
- Modify: `backend/app/routers/jobs.py:79-101`
- Create: `backend/tests/test_transcript_editing.py`

**Interfaces:**
- Consumes: `_iter_word_groups` from Task 1 (indirectly, via `ass_generator.build()` — this task's test renders through the full pipeline to prove the two fixes work together).
- Consumes: `write_transcript_files(output_dir: Path, segments: list) -> None` (unchanged signature, from `backend/app/tasks/pipeline.py`).

This completes the fix for review finding #1: instead of collapsing an edited segment's text into a single fake "word" spanning the whole segment (which forced `pick_emphasis_word` to POS-tag a whole sentence, and gave Duo Tone/Mixed Weight one giant accent blob), split the edited text into real per-word entries, evenly spaced across the segment's original time span.

- [ ] **Step 1: Write the failing (pre-fix) regression test**

Create `backend/tests/test_transcript_editing.py`:

```python
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
    assert updated[0]["words"][-1]["end"] == segments[0]["end"]


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
```

- [ ] **Step 2: Run the new tests to verify they fail against the current code**

```bash
docker compose up -d
bash scripts/create_real_speech_fixtures.sh   # only if the fixtures are missing
pytest backend/tests/test_transcript_editing.py -v
```

Expected: `test_editing_a_segment_gives_it_real_per_word_timing` FAILS with `assert 1 == 3` (the current code produces exactly one fake word). `test_editing_one_segment_does_not_shift_chunking_in_other_segments` may pass or fail depending on the fixture's exact segmentation this run — either way, proceed to the fix.

- [ ] **Step 3: Fix `update_transcript` in `backend/app/routers/jobs.py`**

Replace lines 85-101 (the `# Word-level timing can't be reliably re-derived...` comment through the end of the `updated.append(...)` call) with:

```python
        # Word-level timing can't be reliably re-derived from a free-text edit,
        # so split the edited text into evenly-spaced word slots across the
        # segment's original [start, end] span. Real per-word entries (rather
        # than one giant fake "word") keep ass_generator's per-segment
        # word-group chunking (_iter_word_groups) working sensibly on the
        # edited segment, and let pick_emphasis_word POS-tag real single words
        # instead of a whole sentence.
        seg_start = stored_seg["start"]
        seg_end = stored_seg["end"]
        new_word_strs = new_text.split()
        new_words = []
        if new_word_strs:
            step = max(seg_end - seg_start, 0.01) / len(new_word_strs)
            for i, w in enumerate(new_word_strs):
                new_words.append(
                    {
                        "word": w,
                        "start": seg_start + i * step,
                        "end": seg_start + (i + 1) * step,
                    }
                )
        updated.append(
            {"start": seg_start, "end": seg_end, "text": new_text, "words": new_words}
        )
```

- [ ] **Step 4: Rebuild and re-run the tests to verify they pass**

```bash
docker compose up -d --build backend worker
pytest backend/tests/test_transcript_editing.py -v
```

Expected: `2 passed`.

- [ ] **Step 5: Run the full backend suite to confirm no regressions**

```bash
pytest backend/tests/ -v
```

Expected: all tests pass (same pass count as before this task, plus the 2 new ones).

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/jobs.py backend/tests/test_transcript_editing.py
git commit -m "fix: give edited transcript segments real per-word timing

A transcript edit used to collapse the whole edited sentence into one
fake 'word' spanning the segment. Combined with the previous commit's
per-segment chunking, edits now produce real per-word entries so
compound/Keyword Pop rendering on the edited segment stays sensible."
```

---

### Task 3: `pipeline.py` — persist the transcript before the risky step, and write it atomically

**Files:**
- Modify: `backend/app/tasks/pipeline.py:50-152`
- Modify: `backend/tests/conftest.py`
- Modify: `backend/tests/test_silence_removal.py`
- Modify: `CLAUDE.md:97-102`

**Interfaces:**
- Produces: `fully_silent_video` pytest fixture (session-scoped, generated via ffmpeg into a temp dir) — usable by any test in `backend/tests/`.

Fixes review finding #5 (a silence-removal failure discarded an already-completed transcription, with no artifact ever persisted) and half of finding #4 (a non-atomic write could let `render_video` read a torn file). Also fixes the CLAUDE.md doc text this reordering affects.

- [ ] **Step 1: Add a `fully_silent_video` fixture**

In `backend/tests/conftest.py`, add near the other fixtures (after `sample_video`, before `ready_job`):

```python
@pytest.fixture(scope="session")
def fully_silent_video(tmp_path_factory) -> Path:
    """A short, entirely-silent clip — silencedetect should flag the whole
    duration, which the pipeline must treat as a hard failure rather than
    producing a zero-length output. Generated on the fly (not committed as a
    binary) since it's trivial and deterministic to produce."""
    out = tmp_path_factory.mktemp("fixtures") / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-f", "lavfi", "-i", "color=c=black:size=320x240:rate=24",
            "-t", "2",
            "-c:v", "libx264", "-c:a", "aac",
            str(out),
        ],
        capture_output=True,
        check=True,
    )
    return out
```

- [ ] **Step 2: Write the failing (pre-fix) regression test**

In `backend/tests/test_silence_removal.py`, add to the `TestSilenceRemoval` class:

```python
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
```

- [ ] **Step 3: Run it to verify it fails**

```bash
docker compose up -d
pytest backend/tests/test_silence_removal.py -k transcript_persists -v
```

Expected: FAILS with `assert 409 == 200` (current code raises before `write_transcript_files` ever runs, so `GET .../transcript` 409s with "Transcript not ready yet").

- [ ] **Step 4: Reorder persistence and make writes atomic in `backend/app/tasks/pipeline.py`**

Replace the `write_transcript_files` function (lines 50-70) with an atomic version:

```python
def _atomic_write_text(path: Path, content: str) -> None:
    """Write via a temp file + rename so a concurrent reader (e.g. render_video
    running in a different worker process) never observes a partially-written
    file — os.replace()/Path.replace() is an atomic rename on POSIX."""
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def write_transcript_files(output_dir: Path, segments: list) -> None:
    """Persist transcript artifacts: SRT + TXT for download, JSON for preview/
    render. Shared by the initial transcription (transcribe_video) and the
    transcript-edit endpoint, so an edit saved before render keeps the
    downloadable SRT/TXT consistent with whatever the eventual burn will show."""
    srt_lines = []
    for i, seg in enumerate(segments, 1):
        srt_lines.extend([
            str(i),
            f"{_format_srt_time(seg['start'])} --> {_format_srt_time(seg['end'])}",
            seg["text"],
            "",
        ])
    _atomic_write_text(output_dir / "transcript.srt", "\n".join(srt_lines))
    _atomic_write_text(
        output_dir / "transcript.txt", " ".join(seg["text"] for seg in segments)
    )
    _atomic_write_text(output_dir / "transcript.json", json.dumps({"segments": segments}))
```

Then in `transcribe_video`, move the persistence call to right after transcription succeeds, and add a second call after a successful trim. Replace lines 103-142 with:

```python
        result = transcribe(video_path, job.language, progress_callback=on_progress)
        segments = _serializable_segments(result["segments"])
        write_transcript_files(output_dir, segments)

        silence_removed_seconds = None
        if job.remove_silences:
            silence_removed_seconds = 0.0
            _update_job(db, job_id, step="removing_silences", progress=95)

            duration = get_video_duration(video_path)
            silences = detect_silences(
                video_path, settings.silence_threshold_db, settings.silence_min_duration
            )
            kept_ranges = compute_kept_ranges(
                silences, duration, settings.silence_padding, settings.silence_max_segments
            )

            if not kept_ranges:
                # Entire video flagged silent — refuse to produce a zero-duration
                # output. remove_silences was explicitly requested, so silently
                # ignoring it would be more surprising than failing loudly. The
                # pre-trim transcript written above is still on disk and
                # fetchable even though the job itself is marked failed.
                raise RuntimeError(
                    "Silence removal would remove the entire video; aborting."
                )

            if not (len(kept_ranges) == 1 and kept_ranges[0] == (0.0, duration)):
                trimmed_path = str(output_dir / "trimmed.mp4")

                def on_trim_progress(fraction: float) -> None:
                    _update_job(db, job_id, progress=95 + int(fraction * 4))

                trim_silences(
                    video_path, kept_ranges, trimmed_path, progress_callback=on_trim_progress
                )

                kept_total = sum(e - s for s, e in kept_ranges)
                silence_removed_seconds = max(0.0, duration - kept_total)
                segments = remap_segments(segments, kept_ranges)
                video_path = trimmed_path
                # Overwrite with the final, trimmed-timeline transcript now
                # that the trim actually succeeded.
                write_transcript_files(output_dir, segments)
```

(The `_update_job(...)` call that follows, setting `status="ready"`, is unchanged.)

- [ ] **Step 5: Rebuild and re-run the test to verify it passes**

```bash
docker compose up -d --build backend worker
pytest backend/tests/test_silence_removal.py -k transcript_persists -v
```

Expected: `1 passed`.

- [ ] **Step 6: Update the stale `CLAUDE.md` pipeline description**

In `CLAUDE.md`, replace lines 97-102:

```
transcribe_video task (phase 1):
  1. Transcribe (Whisper)
  2. If remove_silences: detect + cut silent ranges, remap transcript timings onto
     the trimmed video, and swap job.filename to point at it  →  90..99%
     (failure here fails the whole job; the original is never silently kept)
  3. Write transcript.srt + .txt + transcript.json  →  status=ready
```

with:

```
transcribe_video task (phase 1):
  1. Transcribe (Whisper), write transcript.srt + .txt + transcript.json  →  5..95%
  2. If remove_silences: detect + cut silent ranges, remap transcript timings onto
     the trimmed video, swap job.filename to point at it, and re-write the
     transcript files with the remapped timings  →  95..99%  →  status=ready
     (failure here fails the whole job and leaves the pre-trim transcript already
     persisted from step 1; the original video is never silently kept as a
     fallback — the job is reported failed, not quietly downgraded)
```

- [ ] **Step 7: Run the full backend suite to confirm no regressions**

```bash
pytest backend/tests/ -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add backend/app/tasks/pipeline.py backend/tests/conftest.py backend/tests/test_silence_removal.py CLAUDE.md
git commit -m "fix: persist transcript before the fallible silence-removal step

Whisper's transcript is now written to disk right after transcription
succeeds, and re-written after a successful trim — so a silence-removal
failure (e.g. the whole clip flagged silent) no longer discards a
completed transcription with no artifact ever saved. Writes are also
now atomic (temp file + rename) so a concurrent reader can't observe a
partially-written transcript.json."
```

---

### Task 4: `jobs.py` — atomic render-start guard

**Files:**
- Modify: `backend/app/routers/jobs.py:1-18, 108-147`
- Modify: `backend/tests/test_e2e_api.py`

**Interfaces:**
- Consumes: `sqlalchemy.update` (new import).

Fixes review finding #3: two overlapping `POST /jobs/{job_id}/render` calls (e.g. a double-click) could both pass validation and both enqueue a `render_video` Celery task for the same job, corrupting the shared output files. The fix is an atomic compare-and-swap on `status` at the database layer, closing the read-then-write race window entirely.

- [ ] **Step 1: Write the failing (pre-fix) regression test**

In `backend/tests/test_e2e_api.py`, add near the other render-endpoint tests:

```python
def test_render_returns_409_when_already_rendering(sample_video: Path):
    job_id = upload_sample(sample_video)
    wait_for_status(job_id, "ready")

    r1 = render_job(job_id, style="classic")
    assert r1.status_code == 202

    r2 = render_job(job_id, style="classic")
    assert r2.status_code == 409

    wait_for_status(job_id, "complete")
```

- [ ] **Step 2: Run it to verify it fails**

```bash
docker compose up -d
pytest backend/tests/test_e2e_api.py -k render_returns_409 -v
```

Expected: FAILS with `assert 202 == 409` (the current code happily accepts a second render request).

- [ ] **Step 3: Add the atomic guard in `backend/app/routers/jobs.py`**

Add `update` to the sqlalchemy import at the top of the file:

```python
from sqlalchemy import update
```

Replace `render_job` (currently lines 108-147) with:

```python
@router.post("/jobs/{job_id}/render", response_model=JobStatus, status_code=202)
def render_job(job_id: str, req: RenderRequest, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if req.style not in STYLES:
        raise HTTPException(status_code=400, detail=f"Unknown style: {req.style}")

    transcript_path = settings.output_path / job_id / "transcript.json"
    if not transcript_path.exists():
        raise HTTPException(status_code=409, detail="Transcript not ready yet")

    # Atomic compare-and-swap: only start a render if one isn't already in
    # flight. A plain read-then-write (check job.status, then set it) has a
    # race window where two near-simultaneous requests (e.g. a double-click)
    # can both pass the check and both enqueue render_video for the same
    # job_id, corrupting the shared captions.ass/output.mp4 files.
    result = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status != "rendering")
        .values(
            style=req.style,
            position_x=req.position_x,
            position_y=req.position_y,
            scale=req.scale,
            status="rendering",
            step="styling",
            progress=55,
            error=None,
            completed_at=None,
        )
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="Job is already rendering")

    db.refresh(job)
    celery.send_task("render_video", args=[job_id])

    return JobStatus(
        job_id=job.id,
        status=job.status,
        step=job.step,
        progress=job.progress,
        style=job.style,
        position_x=job.position_x,
        position_y=job.position_y,
        scale=job.scale,
        remove_silences=job.remove_silences,
        silence_removed_seconds=job.silence_removed_seconds,
        error=job.error,
        created_at=job.created_at,
        completed_at=job.completed_at,
    )
```

- [ ] **Step 4: Rebuild and re-run the test to verify it passes**

```bash
docker compose up -d --build backend
pytest backend/tests/test_e2e_api.py -k render_returns_409 -v
```

Expected: `1 passed`.

- [ ] **Step 5: Run the full backend suite to confirm no regressions**

```bash
pytest backend/tests/ -v
```

Expected: all tests pass — in particular, every existing single-render test (`completed_job` fixture, compound-style tests) must still get a 202 on its one-and-only render call.

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/jobs.py backend/tests/test_e2e_api.py
git commit -m "fix: make render-start an atomic compare-and-swap on job status

Two overlapping POST /jobs/{id}/render requests (e.g. a double-click)
could both pass validation and both enqueue a render_video task for
the same job, racing to write the same output files. The UPDATE ...
WHERE status != 'rendering' now closes that race at the DB layer: the
loser gets a 409 instead of a corrupted output."
```

---

### Task 5: Frontend — gate rendering on unsaved transcript edits

**Files:**
- Modify: `frontend/src/components/TranscriptEditor.tsx:5-9, 17-35`
- Modify: `frontend/src/components/PreviewEditor.tsx:41-48, 255-281`
- Modify: `e2e/tests/upload-flow.spec.ts`

**Interfaces:**
- Produces: `TranscriptEditor`'s new `onBusyChange?: (busy: boolean) => void` prop, fired whenever the editor has an unsaved edit or a save in flight.
- Consumes: `TranscriptEditor`'s existing `dirty` (local) and `saveState` (local) — unchanged, just also reported upward.

Completes the fix for review finding #4: nothing previously stopped a user from clicking "Render with this style" while a transcript edit was unsaved or still saving, letting `render_video` (a separate Celery worker process) read a stale or in-flight `transcript.json`. Combined with Task 3's atomic writes, this closes the realistic (UI-driven) version of the race; an API client bypassing the UI entirely is outside this fix's scope, same as the rest of this API's trust model.

- [ ] **Step 1: Write the failing (pre-fix) Playwright test**

In `e2e/tests/upload-flow.spec.ts`, add inside the existing `test.describe('Transcript editing', ...)` block (after the `'editing a segment and saving persists the new text'` test):

```typescript
  test('Render button is disabled while a transcript edit is unsaved', async ({ page }) => {
    await page.goto('/')
    await page.locator('input[type="file"]').setInputFiles(SPEECH_FIXTURE)

    const renderBtn = page.getByRole('button', { name: /Render with this style/i })
    await expect(renderBtn).toBeVisible({ timeout: PIPELINE_TIMEOUT })
    await expect(renderBtn).toBeEnabled()

    const firstTextarea = page.locator('.transcript-textarea').first()
    await firstTextarea.fill('Edited but not saved yet.')
    await expect(renderBtn).toBeDisabled()

    const saveBtn = page.getByRole('button', { name: /Save Transcript/i })
    await saveBtn.click()
    await expect(page.getByText('Saved')).toBeVisible()
    await expect(renderBtn).toBeEnabled()
  })
```

- [ ] **Step 2: Run it to verify it fails**

```bash
docker compose up -d
cd e2e && npx playwright test -g "Render button is disabled while a transcript edit is unsaved"
```

Expected: FAILS — `renderBtn` stays enabled after the edit (`toBeDisabled()` times out).

- [ ] **Step 3: Have `TranscriptEditor` report busy state upward**

In `frontend/src/components/TranscriptEditor.tsx`, update the `Props` interface (lines 5-9):

```typescript
interface Props {
  jobId: string
  segments: TranscriptSegment[]
  onSegmentsChange: (segments: TranscriptSegment[]) => void
  onBusyChange?: (busy: boolean) => void
}
```

Update the function signature and add a reporting effect (replace lines 17-30):

```typescript
export function TranscriptEditor({ jobId, segments, onSegmentsChange, onBusyChange }: Props) {
  const [texts, setTexts] = useState<string[]>(() => segments.map((s) => s.text))
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [errorMessage, setErrorMessage] = useState('')

  // A new job means a fresh transcript — resync the editable copy. Keyed on
  // jobId only (not segments) so saving our own edits doesn't get clobbered
  // by the very segments update it just triggered.
  useEffect(() => {
    setTexts(segments.map((s) => s.text))
    setSaveState('idle')
  }, [jobId]) // eslint-disable-line react-hooks/exhaustive-deps

  const dirty = texts.some((t, i) => t !== segments[i]?.text)

  // Let the parent (PreviewEditor) know whether it's safe to render right
  // now — rendering while an edit is unsaved or mid-save can race the
  // transcript.json write that render_video reads from.
  useEffect(() => {
    onBusyChange?.(dirty || saveState === 'saving')
  }, [dirty, saveState, onBusyChange])
```

- [ ] **Step 4: Have `PreviewEditor` gate the Render button on that busy state**

In `frontend/src/components/PreviewEditor.tsx`, add state near the other `useState` calls (after line 48's `transcriptOpen`):

```typescript
  const [transcriptBusy, setTranscriptBusy] = useState(false)
```

Update the `<TranscriptEditor>` usage (currently lines 260-264):

```typescript
        <TranscriptEditor
          jobId={jobId}
          segments={segments}
          onSegmentsChange={onSegmentsChange}
          onBusyChange={setTranscriptBusy}
        />
```

Update the Render button and add a hint (currently lines 267-280):

```typescript
      <button
        className="btn-primary"
        disabled={!selectedStyle || transcriptBusy}
        onClick={() =>
          onSave({
            style: selectedStyle,
            position_x: position.x,
            position_y: position.y,
            scale,
          })
        }
      >
        Render with this style
      </button>
      {transcriptBusy && (
        <p className="preview-hint">Save your transcript edits before rendering.</p>
      )}
```

- [ ] **Step 5: Type-check, rebuild, and re-run the test to verify it passes**

```bash
cd frontend && npx tsc --noEmit
cd .. && docker compose up -d --build nginx
cd e2e && npx playwright test -g "Render button is disabled while a transcript edit is unsaved"
```

Expected: `tsc` reports no errors; `1 passed`.

- [ ] **Step 6: Run the full Playwright suite to confirm no regressions**

```bash
cd e2e && npx playwright test
```

Expected: all tests pass, including the pre-existing `'editing a segment and saving persists the new text'` test.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/TranscriptEditor.tsx frontend/src/components/PreviewEditor.tsx e2e/tests/upload-flow.spec.ts
git commit -m "fix: disable Render while a transcript edit is unsaved

Nothing previously stopped clicking 'Render with this style' while a
transcript edit was dirty or mid-save, letting the render task read a
stale or in-flight transcript.json. TranscriptEditor now reports its
busy state up to PreviewEditor, which disables the Render button (with
a hint) until the edit is saved."
```

---

### Task 6: `database.py` — safe additive migration + SQLite lock timeout

**Files:**
- Modify: `backend/app/database.py`

Fixes review finding #2 (existing rows get `remove_silences=NULL` after the migration, which then fails `JobStatus`'s non-Optional `bool` field validation with a 500) and finding #6 (the migration's `except` clause only swallows "duplicate column" errors, so a concurrent "database is locked" from backend/worker/beat racing on startup would crash that process). This is a startup-time migration path, which — unlike the rest of this suite — cannot be exercised through a per-request httpx test without restarting containers against a deliberately-corrupted DB file. It's verified manually below with exact, reproducible commands instead of a pytest addition.

- [ ] **Step 1: Reproduce the bug against the current code**

```bash
docker compose up -d
# Simulate a pre-v1.5 row: a raw INSERT (bypassing the ORM, which is the only
# place the Python-side Column(default=False) would apply) leaves
# remove_silences NULL, exactly like the pre-fix ALTER TABLE ... ADD COLUMN
# (with no SQL DEFAULT) would for every row that existed before that migration ran.
docker compose exec backend python3 -c "
import sqlite3
conn = sqlite3.connect('/storage/captionator.db')
conn.execute(\"INSERT INTO jobs (id, filename, status, step, progress) VALUES ('legacytest', 'x.mp4', 'ready', 'preview_ready', 100)\")
conn.commit()
print(conn.execute(\"SELECT remove_silences FROM jobs WHERE id='legacytest'\").fetchone())
"
# Expected: (None,)

curl -s -o /dev/null -w "%{http_code}\n" http://localhost/api/jobs/legacytest
# Expected (bug): 500
```

- [ ] **Step 2: Fix the migration in `backend/app/database.py`**

Replace the whole file's engine construction and `ensure_schema` function with:

```python
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker
from .config import settings

SQLALCHEMY_DATABASE_URL = f"sqlite:///{settings.storage_path}/captionator.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    # check_same_thread=False: FastAPI/Celery share this engine across threads.
    # timeout=30: backend, worker, and beat all call ensure_schema() against
    # this same file on startup; sqlite3's default 5s lock-wait is tight
    # enough that a startup race can raise "database is locked" instead of
    # just waiting it out. These are sub-millisecond metadata/UPDATE
    # statements, so 30s is ample headroom without risking a real hang.
    connect_args={"check_same_thread": False, "timeout": 30},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _default_sql_literal(value) -> str:
    """Render a Python column default as a SQL literal for an ALTER TABLE
    DEFAULT clause or backfill UPDATE. Only the scalar types Job columns
    actually use today (bool, int, float, str) are supported."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def ensure_schema() -> None:
    """Idempotent additive migration. There's no Alembic here: create_all() only
    creates missing TABLES, not missing COLUMNS on a table that already exists on
    disk. Any column added to the Job model after a deployment's first run needs
    this, or existing installs would 500 on the new fields forever.

    Safe to call on every startup, and to call concurrently from the backend,
    worker, and beat processes racing against the same SQLite file on first
    deploy: a losing race just hits "duplicate column name", which is
    swallowed (the engine's `timeout` above makes that the rare outcome
    rather than a "database is locked" crash).
    """
    from .models import Job  # local import: avoids a circular import with Base

    inspector = inspect(engine)
    if "jobs" not in inspector.get_table_names():
        return  # create_all() will create it with every current column; nothing to add

    existing = {col["name"] for col in inspector.get_columns("jobs")}
    for column in Job.__table__.columns:
        has_scalar_default = (
            column.default is not None and getattr(column.default, "is_scalar", False)
        )
        if column.name not in existing:
            col_type = column.type.compile(engine.dialect)
            default_sql = ""
            if has_scalar_default:
                default_sql = f" DEFAULT {_default_sql_literal(column.default.arg)}"
            ddl = f'ALTER TABLE jobs ADD COLUMN "{column.name}" {col_type}{default_sql}'
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
            except OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        elif has_scalar_default:
            # Backfill any row left NULL by a pre-v1.5.1 deploy that ran this
            # migration before it added the DEFAULT clause above.
            default_sql = _default_sql_literal(column.default.arg)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f'UPDATE jobs SET "{column.name}" = {default_sql} '
                        f'WHERE "{column.name}" IS NULL'
                    )
                )
```

- [ ] **Step 3: Rebuild and verify the fix against the still-corrupted row from Step 1**

```bash
docker compose up -d --build backend worker beat
sleep 3  # let all three containers' startup ensure_schema() calls run

docker compose exec backend python3 -c "
import sqlite3
conn = sqlite3.connect('/storage/captionator.db')
print(conn.execute(\"SELECT remove_silences FROM jobs WHERE id='legacytest'\").fetchone())
"
# Expected: (0,)  <- backfilled, not NULL

curl -s http://localhost/api/jobs/legacytest
# Expected: 200 with "remove_silences": false, not a 500
```

- [ ] **Step 4: Verify all three containers still start cleanly (no lock-related crash loop)**

```bash
docker compose up -d
docker compose ps
```

Expected: `backend`, `worker`, `beat`, `redis`, `nginx` all `Up`/`healthy`, no restart loop.

- [ ] **Step 5: Clean up the manually-inserted test row and run the full backend suite**

```bash
docker compose exec backend python3 -c "
import sqlite3
conn = sqlite3.connect('/storage/captionator.db')
conn.execute(\"DELETE FROM jobs WHERE id='legacytest'\")
conn.commit()
"
pytest backend/tests/ -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/database.py
git commit -m "fix: give the additive migration a SQL DEFAULT + backfill, raise lock timeout

Job rows that existed before a new Boolean column was added were left
NULL (the Python-side Column default only applies on ORM inserts), and
JobStatus's non-Optional bool field then 500'd reading them. The
migration now sets a SQL-level DEFAULT for new columns and backfills
any already-NULL row. Also raises the SQLite connection timeout to 30s
so backend/worker/beat racing this migration on startup wait for the
lock instead of occasionally crashing with 'database is locked'."
```

---

### Task 7: `config.py` — validate `silence_max_segments`

**Files:**
- Modify: `backend/app/config.py:1-6, 47`

Fixes review finding #7: `SILENCE_MAX_SEGMENTS=0` (or any non-positive value) crashes every silence-removal job deep inside `compute_kept_ranges` with an unhandled `ValueError: min() arg is an empty sequence`. Validating at the settings boundary turns a confusing runtime crash into an immediate, clear startup failure — the settings object is constructed at import time (`settings = Settings()`), so both `backend` and `worker` refuse to start at all with a bad value, rather than accepting it and failing later on a real user's job.

- [ ] **Step 1: Reproduce the bug against the current code**

```bash
docker compose up -d
SILENCE_MAX_SEGMENTS=0 docker compose up -d worker
# Upload a video with enough silence gaps to exceed 1 kept range, e.g. sample.mp4
bash scripts/create_test_fixture.sh  # if missing
curl -s -X POST http://localhost/api/upload \
  -F "file=@backend/tests/fixtures/sample.mp4" \
  -F "remove_silences=true"
# Note the job_id, then:
sleep 5
curl -s http://localhost/api/jobs/<job_id>
# Expected (bug): "status":"failed", "error" containing "min() arg is an empty sequence"
docker compose up -d worker   # restore default SILENCE_MAX_SEGMENTS for the next step
```

- [ ] **Step 2: Add validation in `backend/app/config.py`**

Update the import line at the top of the file:

```python
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
```

Update the `silence_max_segments` field (currently line 47):

```python
    # Safety cap on the number of kept (speech) segments after silence removal.
    # Pathologically chatty/noisy audio can produce hundreds of silence gaps; an
    # ffmpeg filter_complex graph with that many trim/concat branches is slow to
    # build and risks hitting ffmpeg's internal graph limits. Beyond this cap the
    # shortest silences are progressively re-merged into their neighboring kept
    # segments, trading some silence-removal completeness for a bounded graph.
    # Must be >= 1: compute_kept_ranges' safety-merge loop assumes there's
    # always at least one gap left to re-merge across while shrinking toward
    # this cap, and indexes into an empty range otherwise.
    silence_max_segments: int = Field(default=60, ge=1)
```

- [ ] **Step 3: Verify the container now refuses to start with an invalid value**

```bash
SILENCE_MAX_SEGMENTS=0 docker compose up -d --build worker
sleep 3
docker compose ps worker
docker compose logs worker --tail 30
```

Expected: `worker` is not `Up`/is restarting, and the logs show a pydantic `ValidationError` for `silence_max_segments` ("Input should be greater than or equal to 1"), not a job-level crash.

```bash
docker compose up -d --build worker   # restore the default (60) for later tasks
```

- [ ] **Step 4: Run the full backend suite to confirm no regressions with the default value**

```bash
pytest backend/tests/ -v
```

Expected: all tests pass (the default `60` is unaffected by the `ge=1` constraint).

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py
git commit -m "fix: validate SILENCE_MAX_SEGMENTS >= 1 at startup

SILENCE_MAX_SEGMENTS=0 crashed every silence-removal job deep inside
compute_kept_ranges with an unhandled ValueError. Constraining the
setting at the pydantic boundary turns a misconfiguration into an
immediate, clear startup failure instead of a per-job runtime crash."
```

---

### Task 8: `silence.py` — fix `trim_silences`'s progress denominator

**Files:**
- Modify: `backend/app/tasks/silence.py:117-167`

**Interfaces:**
- No signature change to `trim_silences(video_path, kept_ranges, output_path, progress_callback=None)`.

Fixes review finding #8: `trim_silences` computed its progress fraction using `get_video_duration(video_path)` — the *original*, pre-trim source — as the denominator, while the numerator (`out_time_us` from ffmpeg's own `-progress` stream) tracks position in the *trimmed* (shorter) output. The fraction could never reach 1.0, so the live progress bar plateaued well below 100% even once the trim had actually finished. This also removes an unnecessary `ffprobe` subprocess call, since the correct denominator (`sum(e - s for s, e in kept_ranges)`) is already known from the function's own input.

**No automated regression test for this one** (unlike every other task in this plan): a live-polling e2e test against `sample.mp4` was attempted and abandoned. Two things made it impractical rather than merely inconvenient:
1. For `sample.mp4`, ffmpeg's real trim+concat output (~3.3s) is itself slightly shorter than the ideal `sum(kept_ranges)` (~3.47s) — its last kept range is only ~0.167s and isn't fully encoded, a frame-boundary artifact unrelated to this fix — so the fraction can never reach exactly 1.0 for this fixture regardless of the fix. The achievable ceiling is 98 (`95 + int(0.951*4)`), not 99/100, versus the old code's 97 (`95 + int(0.644*4)`) — a real, verified improvement, just not a clean round number.
2. The `removing_silences` step for this fixture completes in well under a second, and even at 10ms polling, 5 of 10 runs missed the window entirely (progress never observed above 95, the value it starts at) — a flake rate far too high to ship.

The fix is verified by the math above (reproducible independently — see Step 2) and by direct code inspection, the same way Tasks 6 and 7's migration/config-validation behaviors were verified without a per-request pytest test.

- [ ] **Step 1: Fix the denominator in `backend/app/tasks/silence.py`**

In `trim_silences` (currently lines 117-167), replace the line:

```python
    duration = get_video_duration(video_path) if progress_callback else 0.0
```

with:

```python
    # The output is the concatenation of kept_ranges, not the original
    # source — using get_video_duration(video_path) here (the pre-trim
    # source) would understate progress for the whole run, since ffmpeg's
    # out_time_us tracks position in the (shorter) trimmed output and would
    # never reach this larger denominator.
    target_duration = sum(e - s for s, e in kept_ranges)
```

And update the two places that reference the old `duration` variable inside this function:

```python
        for line in proc.stdout:
            if not (progress_callback and target_duration):
                continue
            line = line.strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                value = line.split("=", 1)[1]
                if value.isdigit():
                    progress_callback(min(1.0, int(value) / 1_000_000 / target_duration))
```

- [ ] **Step 2: Verify the fix's math against real numbers**

```bash
docker compose up -d --build backend worker
python3 -c "
old_duration, old_out = 5.125, 3.3   # original video duration, ffmpeg's real trim output
new_duration = 3.47                   # sum(kept_ranges) for sample.mp4's silence gaps
old_fraction = old_out / old_duration
new_fraction = old_out / new_duration
print('old ceiling:', 95 + int(old_fraction * 4))  # 97 — what the bug capped at
print('new ceiling:', 95 + int(new_fraction * 4))  # 98 — what the fix reaches
"
```

Expected: `old ceiling: 97` / `new ceiling: 98` — confirming the fix moves the achievable progress ceiling up, even though this specific fixture's ffmpeg output can't reach 100% for the frame-boundary reason above.

- [ ] **Step 3: Run the full backend suite to confirm no regressions**

```bash
pytest backend/tests/ -v
```

Expected: all tests pass, including `test_upload_with_remove_silences_trims_video_and_remaps_transcript` (final output duration/timing unaffected — only the *live* progress value changes).

- [ ] **Step 4: Commit**

```bash
git add backend/app/tasks/silence.py
git commit -m "fix: compute trim_silences progress against the trimmed duration

The progress denominator was the original (pre-trim) source duration
while the numerator tracked position in the (shorter) trimmed output,
so the live progress bar could never reach 100% during the
removing_silences step. Also drops an unnecessary ffprobe re-probe,
since the correct duration is already derivable from kept_ranges.

No dedicated regression test: a live-polling test against sample.mp4
was tried and dropped — this fixture's own ffmpeg output can't reach
100% of the ideal kept_ranges sum (a frame-boundary artifact on its
very short last kept range), and even at 10ms polling the step's
window was too narrow to observe reliably (5/10 runs missed it). The
fix is verified by direct math instead (see Step 2)."
```

---

### Task 9: `CLAUDE.md` — fix the stale `DownloadPanel.tsx` module-map row

**Files:**
- Modify: `CLAUDE.md`

Fixes the remainder of review finding #9: the frontend module-map table still describes `components/DownloadPanel.tsx` as "Links to download MP4, SRT, TXT", but the SRT/ASS download links were replaced by a "Copy AI Prompt" button.

- [ ] **Step 1: Find and fix the stale row**

```bash
grep -n "Links to download MP4, SRT, TXT" CLAUDE.md
```

Replace that line's cell content:

```
| `components/DownloadPanel.tsx` | Links to download MP4, SRT, TXT |
```

with:

```
| `components/DownloadPanel.tsx` | Download Video / Download Transcript links + a "Copy AI Prompt" button (fetches the transcript, interpolates it into an Instagram-caption prompt template, copies to clipboard) |
```

- [ ] **Step 2: Verify the fix**

```bash
grep -n "DownloadPanel.tsx" CLAUDE.md
```

Expected: the row now describes the current three actions, not the removed SRT link.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: fix stale DownloadPanel.tsx description in CLAUDE.md

The module map still described the old 4-download-link layout; it was
consolidated to Download Video / Download Transcript / Copy AI Prompt
in v1.5 but the doc was never updated."
```

---

## Self-Review

**Spec coverage** — mapping each of the 10 reported findings to its task:

1. Transcript edit collapses words, desyncing chunking → Tasks 1 + 2
2. Migration leaves `remove_silences` NULL on existing rows → Task 6
3. `render_job` allows a concurrent double-render → Task 4
4. Transcript save/render race → Tasks 3 (atomic writes) + 5 (UI gating)
5. Transcript discarded if silence removal fails → Task 3
6. `ensure_schema`'s narrow `except` vs. "database is locked" → Task 6
7. `SILENCE_MAX_SEGMENTS=0` crash → Task 7
8. `trim_silences` wrong progress denominator → Task 8
9. Stale `CLAUDE.md` docs (progress %, DownloadPanel row) → Tasks 3 + 9
10. Duplicated word-chunking logic in `ass_generator.py` → Task 1

All 10 covered. No gaps.

**Placeholder scan** — every step above contains complete, real code (no `TBD`/"add error handling"/"similar to Task N"). Confirmed clean.

**Type consistency** — `_iter_word_groups(segments, wpg)` (Task 1) is called identically in both `_build_compound_events` and `_build_keyword_emphasis_events`. `write_transcript_files(output_dir: Path, segments: list) -> None` (Task 3) keeps its existing signature, so Task 2's call site in `update_transcript` (unchanged by Task 3) still matches. `TranscriptEditor`'s new `onBusyChange?: (busy: boolean) => void` (Task 5) is optional, so any other future caller that doesn't pass it still type-checks. Confirmed consistent.
