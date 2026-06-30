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

The `backend` and `nginx` services declare both an `image:` (GHCR) and a `build:` context. `docker compose pull` + `up` uses the prebuilt images; `up --build` rebuilds from the Dockerfiles. The `nginx` image is multi-stage: it compiles the frontend (`./frontend`) and bakes the static build in alongside the edge proxy config, so its build context is the repo root. Images are published by `.github/workflows/docker-publish.yml` on push to `main` and `v*` tags. Override the published tag at run time with `IMAGE_TAG` (default `latest`).

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

Both suites drive the live stack over HTTP, so bring it up first (the dev override uses the fast `tiny.en` model):

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
| `config.py` | Settings from env / `.env` (`REDIS_URL`, `STORAGE_PATH`, `WHISPER_MODEL`, `WHISPER_COMPUTE_TYPE`, `WHISPER_CPU_THREADS`, `CLEANUP_DELAY_SECONDS`, `CORS_ORIGINS`) |
| `models.py` | SQLAlchemy `Job` model (id, filename, style (nullable), language, status, step, progress, caption placement `position_x/position_y/scale`, error, timestamps) |
| `schemas.py` | Pydantic models incl. `JobStatus`, `RenderRequest`, `TranscriptResponse`, `StyleInfo` |
| `database.py` | SQLite engine, `SessionLocal`, DB dependency for injection |
| `routers/upload.py` | `POST /api/upload` — saves file (filename sanitized to its basename; job id is a full uuid4 hex), creates Job, enqueues `transcribe_video` (no style at upload) |
| `routers/jobs.py` | `GET /api/jobs/{job_id}` (status); `GET /api/jobs/{job_id}/transcript` (segments for preview); `POST /api/jobs/{job_id}/render` (start render with style+placement) |
| `routers/preview.py` | `GET /api/preview/{job_id}/source` — streams the uploaded source video inline (Range-enabled) for the preview scrubber |
| `routers/download.py` | `GET /api/download/{job_id}/{file_type}` — streams output files |
| `routers/styles.py` | `GET /api/styles` — lists caption styles (incl. `base_font_size`) |
| `tasks/celery_app.py` | Celery app init, broker=Redis, initializes DB on startup |
| `tasks/pipeline.py` | `transcribe_video` (phase 1) + `render_video` (phase 2); `cleanup_job` deletes a job's files; `sweep_expired_jobs` is the Beat-driven durable cleanup backstop (also reaps abandoned pre-render uploads) |
| `tasks/transcribe.py` | faster-whisper (CTranslate2) transcription (globally cached model), int8 CPU-quantized with VAD; returns segments with word-level timing. Progress driven off each segment's end time vs. audio duration |
| `tasks/ass_generator.py` | Builds ASS subtitle file from segments + style; `build(..., position, scale)` applies a `{\an5\pos(x,y)}` placement override and scales the style font size; handles karaoke word-timing |
| `tasks/ffmpeg_burn.py` | Probes video dimensions + audio codec, burns ASS captions into video via FFmpeg (re-encodes video; stream-copies audio when already AAC) |
| `styles/definitions.py` | 9 style templates (Classic, TikTok Bold, Karaoke, Clean Box, Neon, Minimal, Cinematic, + compound Duo Tone & Mixed Weight); `base_font_size()` helper |

### Processing pipeline (two phases)

```
POST /api/upload
  → saves to /storage/uploads/{job_id}/
  → creates Job row (status=transcribing, no style yet)
  → enqueues transcribe_video Celery task

transcribe_video task (phase 1):
  1. Transcribe (Whisper)
  2. Write transcript.srt + .txt + transcript.json  →  status=ready

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
| `components/PreviewEditor.tsx` | Preview scrubber: plays the source video, overlays the current caption, and lets the user drag/resize (snap-to-center) the caption block + pick a style, then Save to render |
| `components/StylePicker.tsx` | Caption style selection (embedded in the editor) |
| `components/captionStyle.ts` | Shared CSS approximation of each style's text appearance, used by both StylePicker and the preview overlay |
| `components/ProgressTracker.tsx` | Multi-step progress visualization |
| `components/DownloadPanel.tsx` | Links to download MP4, SRT, TXT |
| `api/client.ts` | Axios HTTP client (`uploadVideo`, `getTranscript`, `renderJob`, `sourceVideoUrl`); uses `XMLHttpRequest` for upload progress events |
| `hooks/useJobPolling.ts` | Polls `GET /api/jobs/{job_id}` every 2s until a configurable terminal state (`ready` for transcribe phase, `complete` for render phase) |
| `types/index.ts` | Shared TypeScript interfaces: `StyleInfo`, `JobStatus`, `UploadResponse`, `TranscriptSegment`, `RenderRequest`, `CaptionPlacement` |

The frontend calls the API with relative `/api/*` paths; the edge nginx proxies those to `backend:8000`, so the frontend never needs to know the backend URL directly.

## Environment

Configuration is optional: copy `.env.example` to `.env` to override defaults. `docker compose` reads `.env` automatically and substitutes the values into the container env (each var defaults via `${VAR:-default}` in `docker-compose.yml`, so the stack also runs with no `.env`). `REDIS_URL` and `STORAGE_PATH` are deployment-fixed in compose (they match the network and the volume mount) and are not meant to be overridden; the tunables are:
```
WHISPER_MODEL=small.en        # default; see below
WHISPER_COMPUTE_TYPE=int8_float32
WHISPER_CPU_THREADS=0         # 0 = auto-detect physical cores
CLEANUP_DELAY_SECONDS=600
CORS_ORIGINS=                 # comma-separated; empty = same-origin only (no CORS)
```

`WHISPER_MODEL` defaults to **`small.en`**. It can be changed to `tiny.en`/`base.en`/`medium.en` (English) or `small`/`medium`/`large-v3` (multilingual) — larger models are more accurate but slower and require more RAM. The model is downloaded on first use and cached in a Docker volume (`whisper_cache`).

Transcription runs on **faster-whisper** (CTranslate2), not the reference openai-whisper/torch stack — 3–4× faster on CPU via AVX2 int8 kernels with near-lossless quality. Because faster-whisper is so much quicker, the default `small.en` lands near the old `base.en` wall-clock at higher accuracy. `WHISPER_COMPUTE_TYPE` (default `int8_float32`) trades RAM vs. precision (`int8` < `int8_float32` < `float32`); `WHISPER_CPU_THREADS` (default `0` = auto-detect cores) pins thread count.

When run standalone (outside Docker), the backend reads these same variables from a `backend/.env` or the process environment via pydantic-settings.
