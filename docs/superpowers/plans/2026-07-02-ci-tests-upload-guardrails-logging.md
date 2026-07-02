# Hardening: CI Test Gate, Upload Guardrails, Application Logging — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the top 3 project-review findings: CI runs the full e2e test suite and gates image publishing on it; uploads are size-capped, content-validated (ffprobe), and concurrency-capped; the backend logs every meaningful event so production failures are diagnosable.

**Architecture:** Logging lands first (a tiny `logging_setup.py` shared by the API process and Celery via the `setup_logging` signal) so the guardrail tasks can log their rejections. The three upload guards are independent behaviors in `routers/upload.py` + one middleware in `main.py`, each with its own e2e test; test-reachable limits are pinned in `docker-compose.dev.yml` (the same decoupling pattern already used for `WHISPER_MODEL`). CI comes last so its first green run exercises everything: a `test` job runs `scripts/run-e2e.sh` on the runner, and the existing `build` job gains `needs: test` so untested images can never publish.

**Tech Stack:** Python stdlib `logging` + Celery `setup_logging` signal, FastAPI HTTP middleware, `ffprobe` (already in the backend image and on test hosts), GitHub Actions (`actions/cache`, existing docker build matrix), pytest + httpx e2e tests.

## Global Constraints

- **No unit tests.** All automated tests drive the live Docker stack over HTTP (project convention, per CLAUDE.md). Where behavior isn't HTTP-observable (log output), verification is manual with exact commands — the same pattern used for prior config/DB tasks.
- **Test stack = dev override.** Tests run against `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build`. The dev override pins test-reachable limits (`MAX_UPLOAD_MB=50`, `MAX_ACTIVE_JOBS=3`) exactly like it already pins `WHISPER_MODEL=base.en`.
- **Backend code changes require an image rebuild** before testing: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build backend worker`.
- **Every new env var appears in all four places:** `backend/app/config.py`, `docker-compose.yml` `x-app-env` (as `${VAR:-default}`), `.env.example`, and CLAUDE.md's Environment section.
- **Every new upload rejection returns JSON `{"detail": "<human-readable>"}`** (via `HTTPException` or `JSONResponse`) — `frontend/src/api/client.ts` already surfaces `detail` for any non-202 upload response, so no frontend changes are needed.
- **Do not change nginx's `client_max_body_size` (2000m)** — it stays as the hard edge cap; the app-level limit must be ≤ it.
- **Commit style:** conventional commits (`feat:` / `fix:` / `test:` / `ci:` / `docs:`), matching repo history.

## File Structure

| File | Change |
|------|--------|
| `backend/app/logging_setup.py` | **Create** — one `configure_logging()` shared by API + Celery |
| `backend/app/config.py` | Add `log_level`, `max_upload_mb` (+ `max_upload_bytes` property), `max_active_jobs` |
| `backend/app/main.py` | Call `configure_logging()`, log health degradation, add upload-size middleware |
| `backend/app/tasks/celery_app.py` | `setup_logging` signal hook |
| `backend/app/tasks/pipeline.py` | Task start/finish/failure logs with durations |
| `backend/app/routers/upload.py` | Accept/reject logs; size enforcement; ffprobe validation; active-job cap |
| `backend/app/routers/jobs.py` | Render-requested / transcript-edited logs |
| `backend/tests/test_upload_validation.py` | **Create** — 413 / 415×2 / 429 e2e tests |
| `docker-compose.yml` | `x-app-env`: `LOG_LEVEL`, `MAX_UPLOAD_MB`, `MAX_ACTIVE_JOBS` |
| `docker-compose.dev.yml` | Pin `MAX_UPLOAD_MB=50`, `MAX_ACTIVE_JOBS=3` on backend |
| `.env.example`, `CLAUDE.md` | Document the three new vars; note CI test gate; update upload.py module-map row |
| `e2e/tests/upload-flow.spec.ts` | Fix stale `STYLE_COUNT = 9` → `10` (blocks green CI) |
| `scripts/run-e2e.sh` | `docker-compose` → `docker compose` (v1 binary absent on GH runners) |
| `scripts/create_real_speech_fixtures.sh` | `SKIP_GEHRIG=1` guard (archive.org download is CI-hostile) |
| `.github/workflows/docker-publish.yml` | New `test` job running `run-e2e.sh`; `build` gains `needs: test` |

---

### Task 1: Application logging

Today `backend/app/` contains zero `logging` calls; the only failure signal is the one-line `error` string on the job row. Add a shared logging config and instrument the API + pipeline. Log output isn't HTTP-observable, so this task is verified manually with exact commands (established convention for non-per-request-testable behavior).

**Files:**
- Create: `backend/app/logging_setup.py`
- Modify: `backend/app/config.py`, `backend/app/main.py`, `backend/app/tasks/celery_app.py`, `backend/app/tasks/pipeline.py`, `backend/app/routers/upload.py`, `backend/app/routers/jobs.py`, `docker-compose.yml`, `.env.example`, `CLAUDE.md`

**Interfaces:**
- Produces: `app.logging_setup.configure_logging() -> None` (idempotent; reads `settings.log_level`). Tasks 2–4 use `logger = logging.getLogger(__name__)` module loggers, which this task's root-logger config formats/levels — they don't import anything from this task.

- [ ] **Step 1: Add the `log_level` setting**

In `backend/app/config.py`, after the `cors_origins` field (line 27), add:

```python
    # Root log level for the API and Celery worker/beat ("DEBUG", "INFO",
    # "WARNING", ...). Applied by app.logging_setup.configure_logging(); note
    # the worker's --loglevel CLI flag is superseded by this (the celery
    # setup_logging hook takes over logging config entirely).
    log_level: str = "INFO"
```

- [ ] **Step 2: Create `backend/app/logging_setup.py`**

```python
import logging

from .config import settings

_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure_logging() -> None:
    """Configure the root logger once per process.

    Shared by the FastAPI app (called at import time in main.py) and the
    Celery worker/beat (via the setup_logging signal in tasks/celery_app.py)
    so every container emits the same line format to stdout, where
    `docker compose logs` picks it up. basicConfig is a no-op when the root
    logger already has handlers, so repeated calls are harmless.
    """
    logging.basicConfig(level=settings.log_level.upper(), format=_FORMAT)
```

- [ ] **Step 3: Wire it into the API process (`backend/app/main.py`)**

Replace the import block (lines 1–10) with:

```python
import logging
import os
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from .config import settings
from .database import engine, Base, ensure_schema
from .logging_setup import configure_logging
from .routers import upload, jobs, download, styles, preview

configure_logging()
logger = logging.getLogger(__name__)
```

In the `lifespan` function, add a startup line before `yield`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_schema()
    logger.info(
        "API started: model=%s storage=%s", settings.whisper_model, settings.storage_path
    )
    yield
```

In `health()`, log each failing probe. Replace the `except` branch:

```python
        except Exception as exc:
            checks[name] = f"error: {exc}"
            logger.warning("health check failed: %s: %s", name, exc)
```

- [ ] **Step 4: Wire it into Celery (`backend/app/tasks/celery_app.py`)**

Add to the imports (after `from celery import Celery`):

```python
from celery.signals import setup_logging
```

At the end of the file, after the `init_db` signal handler, add:

```python
@setup_logging.connect
def _configure_logging(**_kwargs):
    # Connecting this signal makes Celery skip its own logger hijacking, so
    # worker/beat lines match the API's format and honor LOG_LEVEL (the
    # --loglevel CLI flag is superseded). Task/module loggers propagate to the
    # root handler configured here.
    from ..logging_setup import configure_logging
    configure_logging()
```

- [ ] **Step 5: Instrument the pipeline (`backend/app/tasks/pipeline.py`)**

Add to the imports at the top:

```python
import logging
import time
```

After the imports, before `_update_job`:

```python
logger = logging.getLogger(__name__)
```

In `transcribe_video`, right after the `if not job: return` guard:

```python
        logger.info("transcribe_video started: job=%s file=%s", job_id, job.filename)
        started = time.monotonic()
```

Right after `write_transcript_files(output_dir, segments)` (the first call, before the silence block):

```python
        logger.info(
            "transcription complete: job=%s segments=%d took=%.1fs",
            job_id, len(segments), time.monotonic() - started,
        )
```

Inside the silence-removal branch, right after the second `write_transcript_files(output_dir, segments)`:

```python
                logger.info(
                    "silence removal: job=%s removed=%.2fs kept_ranges=%d",
                    job_id, silence_removed_seconds, len(kept_ranges),
                )
```

After the final `_update_job(... status="ready" ...)` call:

```python
        logger.info("job ready for preview: job=%s", job_id)
```

Replace the `except` block:

```python
    except Exception as exc:
        logger.exception("transcribe_video failed: job=%s", job_id)
        _update_job(db, job_id, status="failed", error=str(exc))
        raise
```

In `render_video`, right after the `if not job: return` guard:

```python
        logger.info("render_video started: job=%s style=%s", job_id, job.style)
        started = time.monotonic()
```

After the `_update_job(... status="complete" ...)` call (before `cleanup_job.apply_async`):

```python
        logger.info("render complete: job=%s took=%.1fs", job_id, time.monotonic() - started)
```

Replace its `except` block:

```python
    except Exception as exc:
        logger.exception("render_video failed: job=%s", job_id)
        _update_job(db, job_id, status="failed", error=str(exc))
        raise
```

In `cleanup_job`, after the `for base in ...` loop:

```python
    logger.info("cleaned up job files: job=%s", job_id)
```

In `sweep_expired_jobs`, before the final `for job_id in stale_ids:` loop:

```python
    if stale_ids:
        logger.info("sweeping %d expired job(s)", len(stale_ids))
```

- [ ] **Step 6: Instrument the upload router (`backend/app/routers/upload.py`)**

Add `import logging` to the imports and `logger = logging.getLogger(__name__)` after `router = APIRouter()`. Replace the file-writing loop with a byte-counting version, and log acceptance after `celery.send_task`:

```python
    size_bytes = 0
    async with aiofiles.open(dest, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size_bytes += len(chunk)
            await out.write(chunk)
```

```python
    celery.send_task("transcribe_video", args=[job_id])

    logger.info(
        "upload accepted: job=%s file=%s size=%dB language=%s remove_silences=%s",
        job_id, safe_filename, size_bytes, language, remove_silences,
    )
```

- [ ] **Step 7: Instrument the jobs router (`backend/app/routers/jobs.py`)**

Add `import logging` to the imports and `logger = logging.getLogger(__name__)` after `router = APIRouter()`.

In `update_transcript`, right after `write_transcript_files(output_dir, updated)`:

```python
    logger.info("transcript edited: job=%s segments=%d", job_id, len(updated))
```

In `render_job`, right after `celery.send_task("render_video", args=[job_id])`:

```python
    logger.info(
        "render requested: job=%s style=%s pos=(%.2f,%.2f) scale=%.2f",
        job_id, req.style, req.position_x, req.position_y, req.scale,
    )
```

- [ ] **Step 8: Plumb `LOG_LEVEL` through compose and docs**

`docker-compose.yml` — add to the `x-app-env` block after `CORS_ORIGINS`:

```yaml
  LOG_LEVEL: ${LOG_LEVEL:-INFO}
```

`.env.example` — add after the `CORS_ORIGINS` block:

```
# Log level for the backend API and Celery worker/beat (DEBUG, INFO, WARNING).
LOG_LEVEL=INFO
```

`CLAUDE.md` — in the Environment section's env-var code block, add after `CORS_ORIGINS=`:

```
LOG_LEVEL=INFO                # backend + worker log verbosity (DEBUG/INFO/WARNING)
```

- [ ] **Step 9: Rebuild and verify by observation**

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build backend worker beat
curl -sf http://localhost/api/health   # wait until healthy
curl -s -X POST http://localhost/api/upload \
  -F "file=@backend/tests/fixtures/sample.mp4;type=video/mp4" -F "language=auto"
sleep 30
docker compose logs backend | grep "upload accepted"
docker compose logs worker | grep -E "transcribe_video started|transcription complete|job ready"
```

Expected: each grep prints at least one matching, timestamp-formatted line (`... INFO [app.routers.upload] upload accepted: job=... size=...B ...`).

- [ ] **Step 10: Run the existing suite to confirm no regression**

```bash
pytest backend/tests/ -v
```

Expected: all pass (skips only for missing optional fixtures).

- [ ] **Step 11: Commit**

```bash
git add backend/app/ docker-compose.yml .env.example CLAUDE.md
git commit -m "feat: add structured application logging across API and pipeline"
```

---

### Task 2: Upload size limit

nginx's 2 GB `client_max_body_size` is currently the *only* size guard. Add a configurable app-level cap. Critical subtlety: **FastAPI parses the entire multipart body (spooling it to disk) before the endpoint's code runs**, so the Content-Length check must live in middleware to reject before any body byte is read; the endpoint's byte-count check backstops chunked/lying clients (whom nginx still caps at 2 GB).

**Files:**
- Modify: `backend/app/config.py`, `backend/app/main.py`, `backend/app/routers/upload.py`, `docker-compose.yml`, `docker-compose.dev.yml`, `.env.example`, `CLAUDE.md`
- Create: `backend/tests/test_upload_validation.py`

**Interfaces:**
- Consumes: module loggers formatted by Task 1's root config.
- Produces: `settings.max_upload_mb: int` and `settings.max_upload_bytes: int` (property); `POST /api/upload` → `413 {"detail": "File too large. Limit is <N> MB."}`. Task 3 adds its check to the same endpoint *after* this task's stream loop.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_upload_validation.py`:

```python
"""
Integration tests for the upload guardrails (size limit, content validation,
active-job cap) — require the Docker stack running with the dev override
(docker-compose.dev.yml pins MAX_UPLOAD_MB=50 and MAX_ACTIVE_JOBS=3 so the
limits are reachable with small fixtures; production defaults are far higher).
"""
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
```

- [ ] **Step 2: Pin the test limit in the dev override and run the test to verify it fails**

In `docker-compose.dev.yml`, add to the **backend** service's environment list:

```yaml
      # Guardrail limits pinned low so the test suite can actually reach them
      # with small fixtures (same decoupling rationale as WHISPER_MODEL above).
      - MAX_UPLOAD_MB=50
```

Then:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d backend
pytest backend/tests/test_upload_validation.py -v
```

Expected: FAIL — the upload is accepted (202), since no limit exists yet.

- [ ] **Step 3: Add the settings**

In `backend/app/config.py`, after the `log_level` field:

```python
    # Maximum accepted upload size in megabytes. Enforced twice by the backend:
    # from the request's Content-Length in middleware (rejects honest oversized
    # uploads before the body is read) and while streaming the file to disk
    # (covers chunked/lying clients). Keep <= nginx's client_max_body_size
    # (2000m), which remains the hard edge cap.
    max_upload_mb: int = Field(default=2000, ge=1)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024
```

(`Field` is already imported in this file.)

- [ ] **Step 4: Add the Content-Length middleware**

In `backend/app/main.py`, add `Request` to the fastapi import and add a JSONResponse import:

```python
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
```

After the CORS block (below `app.add_middleware(...)`/the `if _cors_origins:` block), add:

```python
@app.middleware("http")
async def enforce_upload_size(request: Request, call_next):
    # FastAPI parses the whole multipart body (spooling it to disk) before the
    # upload endpoint's code runs, so a size check inside the endpoint can't
    # stop an oversized body from landing on disk first. Rejecting here, off
    # the declared Content-Length, refuses honest oversized uploads before a
    # single body byte is read. Chunked/lying clients are caught by the
    # byte-count check in the upload endpoint, and nginx's client_max_body_size
    # (2000m) caps everyone regardless.
    if request.method == "POST" and request.url.path == "/api/upload":
        try:
            content_length = int(request.headers.get("content-length") or 0)
        except ValueError:
            content_length = 0
        if content_length > settings.max_upload_bytes:
            logger.warning(
                "upload rejected (too large): content_length=%d limit=%dMB",
                content_length, settings.max_upload_mb,
            )
            return JSONResponse(
                status_code=413,
                content={"detail": f"File too large. Limit is {settings.max_upload_mb} MB."},
            )
    return await call_next(request)
```

- [ ] **Step 5: Enforce during streaming in the endpoint**

In `backend/app/routers/upload.py`, add `import shutil` and `HTTPException` to the fastapi import:

```python
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Depends
```

Replace the byte-counting write loop from Task 1 with:

```python
    size_bytes = 0
    async with aiofiles.open(dest, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size_bytes += len(chunk)
            if size_bytes > settings.max_upload_bytes:
                shutil.rmtree(dest_dir, ignore_errors=True)
                logger.warning(
                    "upload rejected (too large mid-stream): file=%s limit=%dMB",
                    safe_filename, settings.max_upload_mb,
                )
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Limit is {settings.max_upload_mb} MB.",
                )
            await out.write(chunk)
```

- [ ] **Step 6: Plumb `MAX_UPLOAD_MB` through compose and docs**

`docker-compose.yml` `x-app-env`, after `LOG_LEVEL`:

```yaml
  MAX_UPLOAD_MB: ${MAX_UPLOAD_MB:-2000}
```

`.env.example`, after the `LOG_LEVEL` block:

```
# Maximum accepted upload size in MB. Keep <= nginx's client_max_body_size
# (2000m in nginx/nginx.conf), which is the hard edge cap.
MAX_UPLOAD_MB=2000
```

`CLAUDE.md` env block, after `LOG_LEVEL=INFO`:

```
MAX_UPLOAD_MB=2000            # app-level upload size cap; keep <= nginx's 2000m edge cap
```

- [ ] **Step 7: Rebuild and run the test to verify it passes**

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build backend worker
pytest backend/tests/test_upload_validation.py -v
```

Expected: PASS.

- [ ] **Step 8: Run the full backend suite (fixtures must all stay under 50 MB — they do, sample.mp4 is ~50 KB and the speech fixtures are single-digit MB)**

```bash
pytest backend/tests/ -v
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add backend/app/ backend/tests/test_upload_validation.py docker-compose.yml docker-compose.dev.yml .env.example CLAUDE.md
git commit -m "feat: enforce a configurable upload size limit (MAX_UPLOAD_MB)"
```

---

### Task 3: Upload content validation (ffprobe)

Untrusted bytes currently flow straight into ffmpeg/ffprobe/faster-whisper's decoders mid-pipeline, where failures surface as cryptic job errors. Validate at upload time: the file must be a decodable media file with **both** a video stream (the burn step re-encodes video) and an audio stream (Whisper transcribes audio). ffprobe reads only headers, so this is fast even for huge files.

**Files:**
- Modify: `backend/app/routers/upload.py`, `backend/tests/test_upload_validation.py`, `CLAUDE.md`

**Interfaces:**
- Consumes: Task 2's stream loop (this check runs after the file is fully written to `dest`).
- Produces: `POST /api/upload` → `415 {"detail": "Upload must be a video file with an audio track."}` for undecodable files or files missing a video/audio stream.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_upload_validation.py`, add `import subprocess` above the `import httpx` line, then append:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest backend/tests/test_upload_validation.py -v
```

Expected: the two new tests FAIL (uploads accepted with 202); the 413 test still passes.

- [ ] **Step 3: Add the probe helper and the endpoint check**

In `backend/app/routers/upload.py`, add to the imports:

```python
import asyncio
import json
import subprocess
```

After `logger = logging.getLogger(__name__)`, add:

```python
def _probe_has_video_and_audio(path: str) -> bool:
    """True iff ffprobe can decode the file and finds at least one video and
    one audio stream — the minimum the pipeline needs (Whisper transcribes the
    audio track; the burn step re-encodes the video track). ffprobe reads only
    container headers, so this is fast regardless of file size."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False
    if result.returncode != 0:
        return False
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError:
        return False
    codec_types = {s.get("codec_type") for s in streams}
    return "video" in codec_types and "audio" in codec_types
```

In `upload_video`, immediately after the write loop (before the `Job(...)` construction):

```python
    # Validate the bytes actually are a playable video before creating a job:
    # this turns what would be a cryptic mid-pipeline Whisper/FFmpeg failure
    # into an immediate, clear 415. Run in a thread so the (brief) subprocess
    # call doesn't block the event loop.
    if not await asyncio.to_thread(_probe_has_video_and_audio, str(dest)):
        shutil.rmtree(dest_dir, ignore_errors=True)
        logger.warning("upload rejected (not a playable video): file=%s", safe_filename)
        raise HTTPException(
            status_code=415,
            detail="Upload must be a video file with an audio track.",
        )
```

- [ ] **Step 4: Rebuild and run the tests to verify they pass**

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build backend worker
pytest backend/tests/test_upload_validation.py -v
```

Expected: all 3 PASS.

- [ ] **Step 5: Run the full backend suite (positive control: every existing fixture upload must still be accepted)**

```bash
pytest backend/tests/ -v
```

Expected: all pass. Note `conftest.py`'s `fully_silent_video` has an audio track (anullsrc) — it passes validation by design; its silence is a *pipeline* failure case, not an upload-validation case.

- [ ] **Step 6: Update CLAUDE.md's module map**

In the backend module-map table, replace the `routers/upload.py` row's description with:

```
| `routers/upload.py` | `POST /api/upload` — guards first (size limit via middleware + stream count, ffprobe must find video+audio streams, active-job cap), then saves file (filename sanitized to its basename; job id is a full uuid4 hex), creates Job, enqueues `transcribe_video` (no style at upload) |
```

(The active-job cap lands in Task 4 — writing the row once here avoids two consecutive edits to the same line; Task 4 adds no further CLAUDE.md module-map change.)

- [ ] **Step 7: Commit**

```bash
git add backend/app/routers/upload.py backend/tests/test_upload_validation.py CLAUDE.md
git commit -m "feat: reject uploads that are not playable videos with audio (415)"
```

---

### Task 4: Active-job cap

Nothing currently stops a burst of parallel uploads from filling the disk and building an unbounded queue in front of the single Celery worker. Add a soft cap on concurrently *processing* jobs (`transcribing` or `rendering`; `ready` jobs idling in the editor don't count — they consume no worker time and are reaped by `sweep_expired_jobs`).

**Files:**
- Modify: `backend/app/config.py`, `backend/app/routers/upload.py`, `backend/tests/test_upload_validation.py`, `docker-compose.yml`, `docker-compose.dev.yml`, `.env.example`, `CLAUDE.md`

**Interfaces:**
- Consumes: `Job` model statuses; Task 2's dev-override pattern.
- Produces: `settings.max_active_jobs: int`; `POST /api/upload` → `429 {"detail": "Server is busy processing other videos. Try again in a few minutes."}`.

- [ ] **Step 1: Write the failing test**

> **Correction (2026-07-02, during execution):** the original version of this step
> filled the cap with `sample.mp4`. That fixture is tone-only, so VAD lets a warm
> model transcribe it near-instantly (~0.1s) — three uploads never stay active long
> enough to fill the cap, and the test fails deterministically for the wrong
> reason. Corrected to fill the cap with the ~20 s real-speech fixture (several
> seconds of actual transcription each), which `test_transcript_editing.py`
> already depends on the same way (skip-if-missing).

In `backend/tests/conftest.py`, add a session-scoped speech fixture (after the
`sample_video` fixture):

```python
SPEECH_FIXTURE = Path(__file__).parent / "fixtures" / "real_speech_synthetic.mp4"


@pytest.fixture(scope="session")
def speech_video() -> Path:
    if not SPEECH_FIXTURE.exists():
        pytest.skip(
            f"Fixture not found: {SPEECH_FIXTURE}. "
            "Run: bash scripts/create_real_speech_fixtures.sh"
        )
    return SPEECH_FIXTURE
```

In `backend/tests/test_upload_validation.py`, add the conftest helper import after the `import httpx` line:

```python
try:
    from .conftest import upload_sample, wait_for_status
except ImportError:  # pragma: no cover
    from conftest import upload_sample, wait_for_status
```

Then append:

```python
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
```

(The `speech_video` fixture is injected by pytest automatically — only the helper functions need the explicit import. `test_transcript_editing.py` keeps its own module-scoped fixture of the same name, which harmlessly shadows the conftest one.)

- [ ] **Step 2: Pin the test cap in the dev override and run the test to verify it fails**

In `docker-compose.dev.yml`, add to the backend service's environment list, after `MAX_UPLOAD_MB=50`:

```yaml
      - MAX_ACTIVE_JOBS=3
```

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d backend
pytest backend/tests/test_upload_validation.py::test_uploads_beyond_active_job_cap_get_429 -v
```

Expected: FAIL — the fourth upload returns 202.

- [ ] **Step 3: Add the setting**

In `backend/app/config.py`, after the `max_upload_bytes` property:

```python
    # Maximum number of jobs allowed to be actively processing (status
    # "transcribing" or "rendering") at once; uploads beyond it get 429. With a
    # single Celery worker, an unbounded queue means unbounded disk usage and a
    # starved worker. Soft cap: checked before the job row is created, so two
    # truly simultaneous uploads can overshoot by one — fine for its purpose
    # (backlog/abuse protection, not a hard scheduler).
    max_active_jobs: int = Field(default=10, ge=1)
```

- [ ] **Step 4: Add the check to the endpoint**

In `backend/app/routers/upload.py`, at the very top of `upload_video`'s body (before the `job_id = uuid.uuid4().hex` line):

```python
    active = (
        db.query(Job)
        .filter(Job.status.in_(("transcribing", "rendering")))
        .count()
    )
    if active >= settings.max_active_jobs:
        logger.warning("upload rejected (server busy): active_jobs=%d", active)
        raise HTTPException(
            status_code=429,
            detail="Server is busy processing other videos. Try again in a few minutes.",
        )
```

- [ ] **Step 5: Plumb `MAX_ACTIVE_JOBS` through compose and docs**

`docker-compose.yml` `x-app-env`, after `MAX_UPLOAD_MB`:

```yaml
  MAX_ACTIVE_JOBS: ${MAX_ACTIVE_JOBS:-10}
```

`.env.example`, after the `MAX_UPLOAD_MB` block:

```
# Max jobs actively processing (transcribing/rendering) at once; further
# uploads get 429 until the queue drains.
MAX_ACTIVE_JOBS=10
```

`CLAUDE.md` env block, after `MAX_UPLOAD_MB=2000`:

```
MAX_ACTIVE_JOBS=10            # concurrent processing cap; extra uploads get 429
```

- [ ] **Step 6: Rebuild and run the test to verify it passes**

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build backend worker
pytest backend/tests/test_upload_validation.py -v
```

Expected: all 4 PASS.

- [ ] **Step 7: Run the full backend suite**

```bash
pytest backend/tests/ -v
```

Expected: all pass. (Every other test uploads at most 1–2 jobs before waiting for a terminal state, so ambient active count never reaches 3.)

> **Correction (2026-07-02, during Task 5's cold-stack verification):** the
> parenthetical above holds only on a *warm* stack. On a cold stack (CI's
> reality), the worker's first transcription includes the Whisper model load,
> so sequential tests that upload without waiting pile jobs up in
> `transcribing` and trip the dev cap — 8 failures/12 errors, reproduced 2/2.
> Fix (committed with Task 5): `conftest.py`'s upload helpers
> (`upload_sample`, `upload_sample_with_silence_removal`) now treat 429 as
> the documented backpressure signal and poll-retry for up to ~120 s before
> failing. The cap test is unaffected — it asserts the 429 with its own raw
> `httpx.post`, bypassing the helpers.

- [ ] **Step 8: Commit**

```bash
git add backend/app/ backend/tests/test_upload_validation.py docker-compose.yml docker-compose.dev.yml .env.example CLAUDE.md
git commit -m "feat: cap concurrent active jobs (MAX_ACTIVE_JOBS, 429 when busy)"
```

---

### Task 5: CI test gate

The workflow currently only builds/publishes images — a functionality-breaking change can merge and ship to ghcr.io untested. Add a `test` job that runs the full e2e suite (`scripts/run-e2e.sh`, the exact command developers run locally) and make `build` depend on it. Three prerequisites keep CI green and portable: the stale `STYLE_COUNT = 9` Playwright constant must become 10 (there are 10 styles — this is a known pre-existing failure), `run-e2e.sh` must use `docker compose` v2 (the `docker-compose` v1 binary is absent on GitHub runners), and the Gehrig fixture's archive.org download must be skippable (its tests already `pytest.skip` on a missing fixture).

**Files:**
- Modify: `e2e/tests/upload-flow.spec.ts:13`, `scripts/run-e2e.sh`, `scripts/create_real_speech_fixtures.sh`, `.github/workflows/docker-publish.yml`, `CLAUDE.md`

**Interfaces:**
- Consumes: Tasks 1–4's tests and dev-override pins (CI runs them all via `run-e2e.sh`).
- Produces: a required `test` job; `build` runs only after `test` succeeds.

- [ ] **Step 1: Fix the stale style count**

In `e2e/tests/upload-flow.spec.ts` line 13, change:

```typescript
const STYLE_COUNT = 9
```

to:

```typescript
const STYLE_COUNT = 10
```

(`backend/app/styles/definitions.py` defines 10 styles; the constant predates Keyword Pop.)

- [ ] **Step 2: Modernize `scripts/run-e2e.sh` to compose v2**

Replace every `docker-compose` invocation with `docker compose` (three occurrences):

- Line 13: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build`
- Line 22 (inside the health-wait failure branch): `docker compose logs --tail=50`
- Line 44 (final echo): `echo "Tear down with: docker compose down"`

- [ ] **Step 3: Add the `SKIP_GEHRIG` guard to `scripts/create_real_speech_fixtures.sh`**

Replace the Gehrig block's opening condition:

```bash
if [ -f "$GEHRIG_OUT" ]; then
    echo "Fixture already exists: $GEHRIG_OUT"
else
```

with:

```bash
if [ "${SKIP_GEHRIG:-0}" = "1" ]; then
    echo "SKIP_GEHRIG=1 — skipping the Gehrig fixture (its tests will pytest.skip)."
elif [ -f "$GEHRIG_OUT" ]; then
    echo "Fixture already exists: $GEHRIG_OUT"
else
```

(The rest of the block is unchanged; `elif` chains onto the existing `else`.)

- [ ] **Step 4: Add the `test` job and gate `build` on it**

In `.github/workflows/docker-publish.yml`, change the workflow name to reflect its widened role:

```yaml
name: CI
```

Insert a `test` job before `build`, and add `needs: test` to `build`:

```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - name: Checkout
        uses: actions/checkout@v7

      # The fixture scripts need ffmpeg on the host (the runner image doesn't
      # ship it).
      - name: Install ffmpeg
        run: sudo apt-get update && sudo apt-get install -y --no-install-recommends ffmpeg

      # Fixture generation is slow (the speech fixture apt-installs espeak-ng
      # inside a throwaway container); cache the outputs keyed on the scripts.
      - name: Cache generated test fixtures
        uses: actions/cache@v4
        with:
          path: backend/tests/fixtures
          key: fixtures-${{ hashFiles('scripts/create_test_fixture.sh', 'scripts/create_real_speech_fixtures.sh') }}

      # Gehrig is skipped: it downloads from archive.org, which is too flaky a
      # dependency for CI. Its tests pytest.skip when the fixture is absent.
      - name: Generate speech fixture
        run: SKIP_GEHRIG=1 bash scripts/create_real_speech_fixtures.sh

      # Builds the stack with the dev override, waits for health, then runs
      # pytest + Playwright — the exact suite developers run locally.
      - name: Run e2e suite
        run: bash scripts/run-e2e.sh

      - name: Dump stack logs on failure
        if: failure()
        run: docker compose -f docker-compose.yml -f docker-compose.dev.yml logs --tail=300

  build:
    needs: test
    runs-on: ubuntu-latest
    ...
```

(`build`'s existing content — permissions, strategy matrix, and all steps — is unchanged apart from the added `needs: test` line.)

- [ ] **Step 5: Validate the workflow YAML parses**

```bash
python3 -c "import yaml, sys; yaml.safe_load(open('.github/workflows/docker-publish.yml')); print('OK')"
```

Expected: `OK`. (If PyYAML is missing on the host: `pip install pyyaml` or run inside the backend image.)

- [ ] **Step 6: Run the full suite locally via the modified script — this is the same command CI will run**

```bash
docker compose down
bash scripts/run-e2e.sh
```

Expected: fixture creation, stack build, pytest all-pass (including the 4 new guardrail tests), Playwright all-pass (including the style-count assertion now expecting 10).

- [ ] **Step 7: Update CLAUDE.md**

In the Docker commands section, replace the sentence:

```
Images are published by `.github/workflows/docker-publish.yml` on push to `main` and `v*` tags.
```

with:

```
CI (`.github/workflows/docker-publish.yml`) runs the full e2e suite (`scripts/run-e2e.sh`) on every push/PR, and images publish to GHCR on push to `main` and `v*` tags only after tests pass. The Gehrig speech fixture is skipped in CI (`SKIP_GEHRIG=1`); its tests skip gracefully.
```

- [ ] **Step 8: Commit**

```bash
git add e2e/tests/upload-flow.spec.ts scripts/run-e2e.sh scripts/create_real_speech_fixtures.sh .github/workflows/docker-publish.yml CLAUDE.md
git commit -m "ci: run the full e2e suite and gate image publishing on it"
```

- [ ] **Step 9: Post-push verification (after the branch-finishing step, once the user approves a push)**

```bash
gh run list --limit 3
gh run watch
```

Expected: the `test` job passes; `build` starts only afterward. This is the only step that can't be verified pre-push; if the run fails on runner-environment differences, treat it as a bug to fix, not a reason to remove the gate.

---

## Deferred (considered, intentionally out of scope)

- **Per-IP rate limiting (nginx `limit_req`)** — the active-job cap already bounds the actual risks (disk fill, worker starvation) for this single-user/LAN app; per-IP fairness adds test-flakiness risk (the whole suite uploads from one IP) for little benefit. Revisit if the app is ever exposed publicly.
- **Request-ID / structured JSON logging** — plain formatted lines are enough for `docker compose logs` triage; add structure only when there's an aggregator to consume it.
- **Caching Docker build layers / the whisper model volume in CI** — first-run CI time is acceptable (~10–15 min); optimize only if it becomes a bottleneck.
