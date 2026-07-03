# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Docker (primary dev workflow)
```bash
docker compose pull        # Fetch prebuilt images from GHCR (ghcr.io/gage006/captionator-*)
docker compose up          # Start all 5 services (redis, backend, worker, beat, nginx)
docker compose up --build  # Build images locally instead of using the prebuilt ones
docker compose logs -f worker   # Stream Celery worker logs
docker compose logs -f backend  # Stream FastAPI logs
```

The `backend` and `nginx` services declare both an `image:` (GHCR) and a `build:` context. `docker compose pull` + `up` uses the prebuilt images; `up --build` rebuilds from the Dockerfiles. The `nginx` image is multi-stage: it compiles the frontend (`./frontend`) and bakes the static build in alongside the edge proxy config, so its build context is the repo root. CI (`.github/workflows/docker-publish.yml`) runs the full e2e suite (`scripts/run-e2e.sh`) on every push/PR, and images publish to GHCR on push to `main` and `v*` tags only after tests pass. The speech fixtures are committed to the repo, so the Gehrig tests run in CI too; `SKIP_GEHRIG=1` only prevents the flaky archive.org re-download if the fixture is ever absent. Override the published tag at run time with `IMAGE_TAG` (default `latest`).

### Frontend (standalone)
```bash
cd frontend
npm install
npm run build    # TypeScript check + production build → dist/
```

There is no dev server. The frontend ships as a static build served by the `nginx` image, which compiles it in a build stage and bakes the artifact in (see `nginx/Dockerfile`), so the running app is always the exact production artifact. Iterate by rebuilding the image (`docker compose up --build`).

### Backend (standalone, requires Redis running)
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
celery -A app.tasks.celery_app worker --loglevel=info  # In a separate terminal
```

### Tests (require the Docker stack running)

Both suites drive the live stack over HTTP, so bring it up first (the dev override pins `WHISPER_MODEL=base.en` explicitly — same as the production default, but decoupled so the test suite's accuracy assertions don't silently break if the default ever changes):

```bash
bash scripts/run-e2e.sh   # one-shot: build fixture, start stack, run pytest + Playwright
```

Or run a suite directly against an already-running stack on `http://localhost`:
```bash
pytest backend/tests/ -v                       # API integration tests (httpx)
cd e2e && npm install && npx playwright test   # browser E2E (Playwright)
```

Both skip automatically if the fixture `backend/tests/fixtures/sample.mp4` is missing (`bash scripts/create_test_fixture.sh` generates it). There are no unit tests; coverage is end-to-end against the running services.

## Architecture

Five Docker containers, with Nginx as the edge (port 80):
- **nginx** — serves the React + TypeScript static build (Vite) directly and proxies `/api/*` → `backend:8000`; the frontend assets are baked into this image at build time; 2 GB upload limit, 600s timeouts
- **backend** — FastAPI, exposes REST API, writes jobs to SQLite, enqueues Celery tasks
- **worker** — Celery consumer, runs the video processing pipeline
- **beat** — Celery Beat scheduler; triggers the periodic `sweep_expired_jobs` cleanup task
- **redis** — Celery broker and result backend

Persistent storage is mounted at `./backend/storage` → `/storage` inside containers. SQLite DB lives at `/storage/captionator.db`.

The backend image (backend/worker/beat) runs its workload as the unprivileged `app` user: `backend/docker-entrypoint.sh` starts as root only to `chown` the mounted volumes, then drops privileges via `gosu`. A side effect of the non-root chown is that files under `./backend/storage` become owned by the container's `app` uid on the host — expected, and the app manages its own lifecycle (cleanup deletes them from inside the container).

Healthchecks: `redis` (`redis-cli ping`), `nginx` (HTTP `GET /` — it's the static layer too), `worker` (`celery inspect ping` over the broker — catches an alive-but-disconnected worker), and `backend`. The backend `GET /api/health` is a **readiness** probe, not a static literal: it checks the Redis broker, a DB `SELECT 1`, and storage writability, returning `200 {status: ok, checks: {...}}` or `503 {status: degraded, ...}` naming the failed dependency. Dependents wait for `condition: service_healthy`. `beat` has no healthcheck by design (it's PID 1, so a crash is handled by `restart`; a wedged-but-alive beat only delays cleanup, which other mechanisms backstop).

### Backend module map (`backend/app/`)

| Path | Role |
|------|------|
| `main.py` | FastAPI app, router registration, lifespan handler; CORS is opt-in (only enabled when `CORS_ORIGINS` is set — no wildcard) |
| `config.py` | Settings from env / `.env` (`REDIS_URL`, `STORAGE_PATH`, `WHISPER_MODEL`, `WHISPER_COMPUTE_TYPE`, `WHISPER_CPU_THREADS`, `CLEANUP_DELAY_SECONDS`, `CORS_ORIGINS`, `LOG_LEVEL`, `MAX_UPLOAD_MB`, `MAX_ACTIVE_JOBS`, `RENDER_STALL_SECONDS`) |
| `models.py` | SQLAlchemy `Job` model (id, filename, style (nullable), language, status, step, progress, caption placement `position_x/position_y/scale`, error, timestamps) |
| `schemas.py` | Pydantic models incl. `JobStatus`, `RenderRequest`, `TranscriptResponse`, `StyleInfo` |
| `database.py` | SQLite engine, `SessionLocal`, DB dependency for injection |
| `routers/upload.py` | `POST /api/upload` — guards first (size limit via middleware + stream count, ffprobe must find video+audio streams, active-job cap), then saves file (filename sanitized to its basename; job id is a full uuid4 hex), creates Job, enqueues `transcribe_video` (no style at upload) |
| `routers/jobs.py` | `GET /api/jobs/{job_id}` (status); `GET /api/jobs/{job_id}/transcript` (segments for preview); `PUT /api/jobs/{job_id}/transcript` (edit segment text and/or flag segments `delete: true` — positional against the stored list, count must match as a staleness guard, edited text gets evenly re-spaced word timing, ≥1 segment must survive); `POST /api/jobs/{job_id}/render` (start render with style+placement, atomic CAS on status) |
| `routers/preview.py` | `GET /api/preview/{job_id}/source` — streams the uploaded source video inline (Range-enabled) for the preview scrubber |
| `routers/download.py` | `GET /api/download/{job_id}/{file_type}` — streams output files |
| `routers/styles.py` | `GET /api/styles` — lists caption styles (incl. `base_font_size`) |
| `tasks/celery_app.py` | Celery app init, broker=Redis, initializes DB on startup |
| `tasks/pipeline.py` | `transcribe_video` (phase 1) + `render_video` (phase 2); `cleanup_job` deletes a job's files; `sweep_expired_jobs` is the Beat-driven durable cleanup backstop (also reaps abandoned pre-render uploads and marks stalled renders failed so they release their `MAX_ACTIVE_JOBS` slot) |
| `tasks/transcribe.py` | faster-whisper (CTranslate2) transcription (globally cached model), int8 CPU-quantized with VAD; returns segments with word-level timing. Progress driven off each segment's end time vs. audio duration |
| `tasks/silence.py` | Optional, opt-in (`Job.remove_silences`) step run inside phase 1: `detect_silences` (FFmpeg `silencedetect`), `compute_kept_ranges` (pure; pads/merges/caps), `trim_silences` (FFmpeg `trim`/`atrim`+`concat` re-encode — cuts aren't keyframe-aligned, so `-c:v copy` isn't possible), `remap_segments` (pure; re-times every segment/word onto the trimmed timeline) |
| `tasks/ass_generator.py` | Builds ASS subtitle file from segments + style; `build(..., position, scale)` applies a `{\an5\pos(x,y)}` placement override and scales the style font size; handles karaoke word-timing; `keyword_emphasis` styles pop one semantic word per fixed-size group instead of a trailing block |
| `tasks/emphasis.py` | `pick_emphasis_word` — NLTK POS-tags a word group and returns the index of the longest noun/verb/adjective (or `None`), used by the Keyword Pop style |
| `tasks/ffmpeg_burn.py` | Probes video dimensions + audio codec, burns ASS captions into video via FFmpeg (re-encodes video; stream-copies audio when already AAC) |
| `styles/definitions.py` | 10 style templates (Classic, TikTok Bold, Karaoke, Clean Box, Neon, Minimal, Cinematic, + compound Duo Tone, Mixed Weight & Keyword Pop); `base_font_size()` helper |

### Processing pipeline (two phases)

```
POST /api/upload
  → saves to /storage/uploads/{job_id}/
  → creates Job row (status=transcribing, no style yet)
  → enqueues transcribe_video Celery task

transcribe_video task (phase 1):
  1. Transcribe (Whisper), write transcript.srt + .txt + transcript.json  →  5..95%
  2. If remove_silences: detect + cut silent ranges, remap transcript timings onto
     the trimmed video, swap job.filename to point at it, and re-write the
     transcript files with the remapped timings  →  95..99%  →  status=ready
     (failure here fails the whole job and leaves the pre-trim transcript already
     persisted from step 1; the original video is never silently kept as a
     fallback — the job is reported failed, not quietly downgraded)

(user previews + picks style/placement in the editor)

POST /api/jobs/{job_id}/render  { style, position_x, position_y, scale }
  → updates Job, status=rendering
  → enqueues render_video Celery task

render_video task (phase 2):
  1. Load transcript.json   →  55%
  2. Build ASS (style + placement + size)  →  60%
  3. FFmpeg burn captions   →  60..99% (live, driven by FFmpeg -progress)  →  100%  →  status=complete
```

Output files land in `/storage/outputs/{job_id}/`: `output.mp4`, `transcript.srt`, `transcript.txt`, `transcript.json`, `captions.ass`.

### Frontend module map (`frontend/src/`)

| Path | Role |
|------|------|
| `App.tsx` | Top-level state machine: `idle → uploading → transcribing → editing → rendering → complete/error` |
| `components/UploadZone.tsx` | Drag-and-drop file input |
| `components/PreviewEditor.tsx` | Preview scrubber: plays the source video, overlays the current caption (previewing unsaved draft text, skipping draft-deleted segments), and lets the user drag/resize (snap-to-center, 5px click/drag dead zone) or click the caption block to edit its text inline, pick a style, then Save to render; owns the shared transcript draft via `useTranscriptDraft` |
| `components/TranscriptEditor.tsx` | Controlled segment list panel: per-row text editing + Delete/Undo flags (last survivor locked) and the Save Transcript button; all state comes from the shared draft hook |
| `components/StylePicker.tsx` | Caption style selection (embedded in the editor) |
| `components/captionStyle.ts` | Shared CSS approximation of each style's text appearance, used by both StylePicker and the preview overlay |
| `components/ProgressTracker.tsx` | Multi-step progress visualization |
| `components/DownloadPanel.tsx` | Download Video / Download Transcript links + a "Copy AI Prompt" button (fetches the transcript, interpolates it into an Instagram-caption prompt template, copies to clipboard) |
| `api/client.ts` | Axios HTTP client (`uploadVideo`, `getTranscript`, `updateTranscript`, `renderJob`, `sourceVideoUrl`); uses `XMLHttpRequest` for upload progress events |
| `hooks/useJobPolling.ts` | Polls `GET /api/jobs/{job_id}` every 2s until a configurable terminal state (`ready` for transcribe phase, `complete` for render phase) |
| `hooks/useTranscriptDraft.ts` | The one editable transcript draft (`{text, deleted}[]`, positional against server segments) shared by the panel and the overlay editor; owns dirty/busy state and the `PUT /transcript` save round-trip, re-baselining from each response |
| `types/index.ts` | Shared TypeScript interfaces: `StyleInfo`, `JobStatus`, `UploadResponse`, `TranscriptSegment`, `RenderRequest`, `CaptionPlacement` |

The frontend calls the API with relative `/api/*` paths; the edge nginx proxies those to `backend:8000`, so the frontend never needs to know the backend URL directly.

## Environment

Configuration is optional: copy `.env.example` to `.env` to override defaults. `docker compose` reads `.env` automatically and substitutes the values into the container env (each var defaults via `${VAR:-default}` in `docker-compose.yml`, so the stack also runs with no `.env`). `REDIS_URL` and `STORAGE_PATH` are deployment-fixed in compose (they match the network and the volume mount) and are not meant to be overridden; the tunables are:
```
WHISPER_MODEL=base.en         # default; see below
WHISPER_COMPUTE_TYPE=int8_float32
WHISPER_CPU_THREADS=0         # 0 = auto-detect physical cores
CLEANUP_DELAY_SECONDS=600
CORS_ORIGINS=                 # comma-separated; empty = same-origin only (no CORS)
LOG_LEVEL=INFO                # backend + worker log verbosity (DEBUG/INFO/WARNING)
MAX_UPLOAD_MB=2000            # app-level upload size cap; keep <= nginx's 2000m edge cap
MAX_ACTIVE_JOBS=10            # concurrent processing cap; extra uploads get 429
RENDER_STALL_SECONDS=7200     # sweep marks a job failed after this long in "rendering" (frees its cap slot)
SILENCE_THRESHOLD_DB=-30.0    # dBFS below which audio counts as silence
SILENCE_MIN_DURATION=0.5      # seconds of quiet required to count as removable
SILENCE_PADDING=0.15          # seconds kept around each cut, avoids clipping words
SILENCE_MAX_SEGMENTS=60       # safety cap on kept-segment count
```

`WHISPER_MODEL` defaults to **`base.en`** — the recommended starting point. `tiny.en` is faster but noticeably mis-hears words on real speech (verified against a known transcript in `backend/tests/test_real_speech_silence_removal.py`: it turned "the luckiest man" into "the luck group man" and "a bad break" into "a bad breath"); `base.en` gets both right. It can be changed to `tiny.en`/`small.en`/`medium.en` (English) or `tiny`/`small`/`medium`/`large-v3` (multilingual) — larger models are more accurate but slower and require more RAM. The model is downloaded on first use and cached in a Docker volume (`whisper_cache`).

Transcription runs on **faster-whisper** (CTranslate2), not the reference openai-whisper/torch stack — 3–4× faster on CPU via AVX2 int8 kernels with near-lossless quality, which is what makes `base.en` practical as the default despite running on CPU. `WHISPER_COMPUTE_TYPE` (default `int8_float32`) trades RAM vs. precision (`int8` < `int8_float32` < `float32`); `WHISPER_CPU_THREADS` (default `0` = auto-detect cores) pins thread count.

The `SILENCE_*` vars only matter when a user checks "Remove silences" at upload — see the pipeline section below.

When run standalone (outside Docker), the backend reads these same variables from a `backend/.env` or the process environment via pydantic-settings.
