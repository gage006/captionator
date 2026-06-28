# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Docker (primary dev workflow)
```bash
docker compose pull        # Fetch prebuilt images from GHCR (ghcr.io/gage006/captionator-*)
docker compose up          # Start all 6 services (redis, backend, worker, beat, frontend, nginx)
docker compose up --build  # Build images locally instead of using the prebuilt ones
docker compose logs -f worker   # Stream Celery worker logs
docker compose logs -f backend  # Stream FastAPI logs
```

The `backend`, `frontend`, and `nginx` services declare both an `image:` (GHCR) and a `build:` context. `docker compose pull` + `up` uses the prebuilt images; `up --build` rebuilds from the Dockerfiles. Images are published by `.github/workflows/docker-publish.yml` on push to `main` and `v*` tags. Override the published tag at run time with `IMAGE_TAG` (default `latest`).

### Frontend (standalone)
```bash
cd frontend
npm install
npm run build    # TypeScript check + production build → dist/
```

There is no dev server. The frontend ships as a static build served by nginx (see `frontend/Dockerfile`), so the running app is always the exact production artifact. Iterate by rebuilding the image (`docker compose up --build`).

### Backend (standalone, requires Redis running)
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
celery -A app.tasks.celery_app worker --loglevel=info  # In a separate terminal
```

There are no test suites configured for either frontend or backend.

## Architecture

Six Docker containers behind an Nginx reverse proxy (port 80):
- **nginx** — routes `/api/*` → `backend:8000`, all other paths → `frontend:3000`; 2 GB upload limit, 600s timeouts
- **frontend** — React + TypeScript static build (Vite), served by nginx
- **backend** — FastAPI, exposes REST API, writes jobs to SQLite, enqueues Celery tasks
- **worker** — Celery consumer, runs the video processing pipeline
- **beat** — Celery Beat scheduler; triggers the periodic `sweep_expired_jobs` cleanup task
- **redis** — Celery broker and result backend

Persistent storage is mounted at `./backend/storage` → `/storage` inside containers. SQLite DB lives at `/storage/captionator.db`.

### Backend module map (`backend/app/`)

| Path | Role |
|------|------|
| `main.py` | FastAPI app, CORS, router registration, lifespan handler |
| `config.py` | Settings loaded from `.env` (`REDIS_URL`, `STORAGE_PATH`, `WHISPER_MODEL`, `WHISPER_COMPUTE_TYPE`, `WHISPER_CPU_THREADS`, `CLEANUP_DELAY_SECONDS`) |
| `models.py` | SQLAlchemy `Job` model (id, filename, style (nullable), language, status, step, progress, caption placement `position_x/position_y/scale`, error, timestamps) |
| `schemas.py` | Pydantic models incl. `JobStatus`, `RenderRequest`, `TranscriptResponse`, `StyleInfo` |
| `database.py` | SQLite engine, `SessionLocal`, DB dependency for injection |
| `routers/upload.py` | `POST /api/upload` — saves file, creates Job, enqueues `transcribe_video` (no style at upload) |
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
  2. Build ASS (style + placement + size)  →  70%
  3. FFmpeg burn captions   →  100%  →  status=complete
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

Copy `.env.example` to `.env` before first run. Key variables:
```
REDIS_URL=redis://redis:6379/0
STORAGE_PATH=/storage
WHISPER_MODEL=base.en
```

`WHISPER_MODEL` can be changed to `small`, `medium`, `large`, etc. — larger models are more accurate but slower and require more RAM. The model is downloaded on first use and cached in a Docker volume (`whisper_cache`).

Transcription runs on **faster-whisper** (CTranslate2), not the reference openai-whisper/torch stack — 3–4× faster on CPU via AVX2 int8 kernels with near-lossless quality. `WHISPER_COMPUTE_TYPE` (default `int8_float32`) trades RAM vs. precision (`int8` < `int8_float32` < `float32`); `WHISPER_CPU_THREADS` (default `0` = auto-detect cores) pins thread count. Because faster-whisper is so much quicker, bumping `WHISPER_MODEL` to `small.en` lands near the old `base.en` wall-clock at higher accuracy.
