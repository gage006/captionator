"""Upload-time faststart remux.

Stdlib-only at import time (like tasks/encoder.py) so its unit tests
(tests/test_upload_remux.py) run without the Docker stack or the backend's
dependencies installed.
"""
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def remux_faststart(src: Path, dest_dir: Path) -> Path | None:
    """Rewrite the container so the `moov` box (and track headers) sit at the
    front, freshly recomputed. Fixes two independent browser-preview bugs at
    once: a trailing `moov` can leave metadata stuck loading in browsers that
    don't proactively range-request the tail, and some encoders write a zeroed
    `tkhd` width/height even though the stream decodes fine — Chrome trusts
    `tkhd` for videoWidth/videoHeight, so that plays audio and reports a valid
    duration while never painting a frame. `-c copy` keeps the original codecs
    (no re-encode, just a header rewrite), so this is fast regardless of file
    size. Returns None (leaving `src` untouched) if ffmpeg can't remux it —
    the raw upload still works, it just may preview poorly in some browsers.
    """
    out = dest_dir / "source.mp4"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(src),
                "-map", "0", "-c", "copy", "-movflags", "+faststart",
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("faststart remux failed for %s, keeping original: %s", src.name, exc)
        out.unlink(missing_ok=True)
        return None
    if out != src:
        src.unlink(missing_ok=True)
    return out
