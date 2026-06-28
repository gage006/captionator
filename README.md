# Captionator

Self-hosted video captioning app. Upload a video, pick a style, get back a captioned MP4 — no external APIs, no cloud.

Built for iOS video uploads but works from any browser.

## How it works

1. Upload a video — Whisper transcribes the audio locally
2. Preview the result in the editor: pick a caption style and drag/resize the caption placement
3. Save to render — captions are burned into the video with FFmpeg
4. Download the captioned MP4, SRT, or plain-text transcript

## Caption styles

- **Classic** — white text, black outline
- **TikTok Bold** — large bold text, centered
- **Karaoke** — word-by-word highlight timing
- **Clean Box** — semi-transparent background box
- **Neon** — glowing colored text
- **Minimal** — small, unobtrusive
- **Cinematic** — letterbox-style lower-third
- **Duo Tone** — two-color compound styling
- **Mixed Weight** — mixed font weights for emphasis

## Running locally

Requires Docker and Docker Compose.

```bash
cp .env.example .env
docker compose pull   # fetch prebuilt images from GHCR (skips the slow build)
docker compose up
```

Open [http://localhost](http://localhost).

The Whisper model (~150 MB for `base.en`) downloads on first run and is cached in a Docker volume so rebuilds don't re-download it.

### Prebuilt images vs. building locally

The custom images (`backend`, `frontend`, `nginx`) are built by GitHub Actions on every push to `main` and published to the GitHub Container Registry under `ghcr.io/gage006/captionator-*`. `docker compose pull` fetches them, so a first run no longer needs to compile PyTorch/Whisper locally.

- **Use prebuilt images (fast):** `docker compose pull && docker compose up`
- **Build locally instead:** `docker compose up --build` — rebuilds from the Dockerfiles, ignoring the published images.
- **Pin a specific version:** set `IMAGE_TAG` to any published tag (a git tag like `v1.2.3`, a branch name, or a short commit SHA). Defaults to `latest`.

  ```bash
  IMAGE_TAG=v1.0.0 docker compose up
  ```

The packages are public, so `docker compose pull` works without authenticating. To publish images yourself, push to `main` (or a `v*` tag) and the [Build and publish images](.github/workflows/docker-publish.yml) workflow handles the rest.

## Configuration

Edit `.env` before starting:

| Variable | Default | Notes |
|---|---|---|
| `WHISPER_MODEL` | `base.en` | `small.en`, `medium`, `large` for better accuracy (slower, more RAM) |
| `REDIS_URL` | `redis://redis:6379/0` | Change if using an external Redis |
| `STORAGE_PATH` | `/storage` | Where uploads and outputs are stored inside containers |
| `CLEANUP_DELAY_SECONDS` | `600` | How long a finished job's files are kept before deletion (see [File retention](#file-retention)) |

## File retention

Storage is cleaned up automatically — nothing is kept indefinitely:

- **Finished jobs** — once you render a video, its uploads and outputs (MP4, transcript, etc.) are deleted `CLEANUP_DELAY_SECONDS` (default 10 minutes) after the render completes. **Download your files before then.**
- **Abandoned or failed uploads** — a video you upload but never render (or one whose processing fails) is removed about an hour after upload, so source files don't pile up.

A Celery Beat scheduler re-checks the database every minute, so cleanup is durable across restarts rather than relying on an in-memory timer.

## Stack

- **Backend** — FastAPI + Celery + Redis + SQLite
- **Processing** — OpenAI Whisper (local) + FFmpeg
- **Frontend** — React + TypeScript + Vite
- **Proxy** — Nginx (port 80, 2 GB upload limit)

All six services (nginx, frontend, backend, worker, beat, redis) run in Docker Compose. Storage is persisted at `./backend/storage`.

## Development

Stream logs while running:

```bash
docker compose logs -f worker    # Celery task output
docker compose logs -f backend   # FastAPI logs
```

Run the frontend standalone:

```bash
cd frontend && npm install && npm run dev
```
