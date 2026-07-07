"""Browser-safe preview handling for the source video.

The preview scrubber streams the uploaded source through a plain <video>
element, so it can only show codecs the user's browser can decode. Phone
cameras commonly record HEVC/H.265, which Chrome frequently cannot play:
the container demuxes fine (duration, seeking, AAC audio all work) but no
frames decode, so videoWidth stays 0 and the preview is a black, collapsed
box. The render pipeline itself is unaffected — FFmpeg decodes anything.

Rather than transcoding the upload itself (which would add a lossy
generation to the final output, since the caption burn re-encodes again),
this module leaves the original untouched as the render source and, when the
codec isn't browser-safe, produces a throwaway H.264 `preview.mp4` sidecar
that only the preview route serves. Browser-safe sources instead get a
lossless in-place faststart remux (moov moved to the front, track headers
recomputed) so metadata loads promptly.

Deliberately stdlib-only at import time so its unit tests run without the
stack (same pattern as encoder.py).
"""
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Codecs (+ pixel formats) every mainstream desktop/mobile browser decodes in
# an MP4 container. Deliberately conservative: H.264 8-bit 4:2:0 only. VP9/AV1
# would play in Chrome but not reliably in Safari-in-mp4, and 10-bit H.264
# (High 10) has no browser hardware path — those all take the preview
# transcode instead.
_SAFE_VIDEO = {("h264", "yuv420p"), ("h264", "yuvj420p")}

PREVIEW_FILENAME = "preview.mp4"


def probe_video_codec(path: str) -> tuple[Optional[str], Optional[str]]:
    """(codec_name, pix_fmt) of the first video stream, or (None, None) when
    ffprobe can't read the file — callers treat unknown as not browser-safe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,pix_fmt",
                "-print_format", "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        streams = json.loads(result.stdout).get("streams", [])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None, None
    if not streams:
        return None, None
    return streams[0].get("codec_name"), streams[0].get("pix_fmt")


def is_browser_safe(codec_name: Optional[str], pix_fmt: Optional[str]) -> bool:
    return (codec_name, pix_fmt) in _SAFE_VIDEO


def build_preview_cmd(src: str, out: str) -> list[str]:
    """Preview-only H.264 encode. Quality is cosmetic here (the render never
    touches this file), so speed wins: veryfast preset, crf 23, audio always
    normalized to AAC. faststart so metadata loads before the whole file."""
    return [
        "ffmpeg", "-y", "-i", src,
        "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        out,
    ]


def create_preview(
    src: str,
    out: str,
    progress_callback: Optional[Callable[[float], None]] = None,
    duration: float = 0.0,
) -> bool:
    """Encode the preview sidecar. Returns False (removing any partial output)
    on failure — the job still works end-to-end, only the in-browser preview
    degrades, so this is never worth failing the job over."""
    cmd = build_preview_cmd(src, out)
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err_file:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_file, text=True)
            assert proc.stdout is not None
            for line in proc.stdout:
                if not (progress_callback and duration):
                    continue
                line = line.strip()
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    value = line.split("=", 1)[1]
                    if value.isdigit():
                        progress_callback(min(1.0, int(value) / 1_000_000 / duration))
            returncode = proc.wait()
            if returncode != 0:
                err_file.seek(0)
                tail = err_file.read()[-2000:]
                raise RuntimeError(f"FFmpeg preview transcode failed:\n{tail}")
    except Exception as exc:
        logger.warning("preview transcode failed for %s: %s", src, exc)
        Path(out).unlink(missing_ok=True)
        return False
    return True


def remux_faststart(path: str) -> bool:
    """Lossless in-place container rewrite (`-c copy -movflags +faststart`):
    moves a trailing `moov` to the front and recomputes track headers (some
    encoders write a zeroed tkhd width/height, which Chrome trusts for
    videoWidth — audio plays, no frame ever paints). Same filename afterwards,
    so nothing else needs to know. Returns False (original untouched) if
    ffmpeg can't remux this container."""
    tmp = path + ".faststart.mp4"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", path,
                "-map", "0", "-c", "copy", "-movflags", "+faststart",
                tmp,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("faststart remux failed for %s, keeping original: %s", path, exc)
        Path(tmp).unlink(missing_ok=True)
        return False
    os.replace(tmp, path)
    return True
