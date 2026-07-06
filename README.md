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
cp .env.example .env   # optional — the stack runs with defaults if you skip this
docker compose pull
docker compose up
```

### Accessing the app

Once the containers are up, open **[http://localhost](http://localhost)** in your browser. The `captionator` container serves the app on port 80 and is the only entry point — everything else (`captionator-backend`, `captionator-worker`, `captionator-beat`, `captionator-redis`) runs behind it on an internal network.

Check that everything is healthy with:

```bash
docker compose ps
```

The `captionator` container waits for the backend to pass its health check before starting, so the app is ready to use as soon as it shows `Up`. On the very first run, allow a minute or two: the Whisper model (~150 MB for `base.en`) downloads on first use and is cached in a Docker volume so restarts and rebuilds don't re-download it.

If port 80 is already taken on your machine, change the `ports` mapping on the `nginx` service in `docker-compose.yml` (e.g. `"8080:80"`) and open [http://localhost:8080](http://localhost:8080) instead.

## Releasing

The deployed version is shown in the footer of the app. It comes from `frontend/package.json`, which is the single source of truth: Vite bakes it into the build, and CI refuses to publish a `v*` release tag that doesn't match it. To cut a release, bump the version in `frontend/package.json` and `frontend/package-lock.json`, commit, then tag the commit `v<version>`.

## Configuration

Edit `.env` before starting:

| Variable | Default | Notes |
|---|---|---|
| `WHISPER_MODEL` | `base.en` | `small.en`, `medium`, `large` for better accuracy (slower, more RAM) |
| `REDIS_URL` | `redis://redis:6379/0` | Change if using an external Redis |
| `STORAGE_PATH` | `/storage` | Where uploads and outputs are stored inside containers |
| `CLEANUP_DELAY_SECONDS` | `600` | How long a finished job's files are kept before deletion