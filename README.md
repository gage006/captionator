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
docker compose pull
docker compose up
```

Open [http://localhost](http://localhost).

The Whisper model (~150 MB for `base.en`) downloads on first run and is cached in a Docker volume so rebuilds don't re-download it.

## Configuration

Edit `.env` before starting:

| Variable | Default | Notes |
|---|---|---|
| `WHISPER_MODEL` | `base.en` | `small.en`, `medium`, `large` for better accuracy (slower, more RAM) |
| `REDIS_URL` | `redis://redis:6379/0` | Change if using an external Redis |
| `STORAGE_PATH` | `/storage` | Where uploads and outputs are stored inside containers |
| `CLEANUP_DELAY_SECONDS` | `600` | How long a finished job's files are kept before deletion