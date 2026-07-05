"""Runtime selection of the FFmpeg H.264 encoder for the caption burn.

The image's FFmpeg has h264_nvenc / h264_qsv / h264_vaapi compiled in, but
"compiled in" says nothing about the host: without the right device node and
driver an encoder listed by `ffmpeg -encoders` still fails at runtime (the
dev WSL2 box lists h264_qsv with no /dev/dri at all). The only trustworthy
test is a real encode, so selection trial-encodes ~1s of synthetic video per
candidate — with the same filter shape production uses — and takes the first
success. The result is cached per process (like transcribe.get_model()).

IMPORTANT: this module must import only stdlib at module level. The unit
tests (backend/tests/test_encoder_selection.py) run on hosts without the
backend's dependencies; settings are imported lazily inside detect_encoder().
"""
import logging
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EncoderChoice:
    name: str
    # Output-side args: codec + quality flags, tuned for visual parity with
    # the legacy libx264 CRF 23 output (files may come out somewhat larger).
    output_args: tuple[str, ...]
    # Appended to the -vf chain. The ASS burn always renders in software
    # (subtitle rasterization is CPU-only), so hardware paths convert/upload
    # frames AFTER captioning — the upload step travels with the encoder.
    filter_suffix: str = ""
    # Args that must precede -i (e.g. the VAAPI device).
    pre_input_args: tuple[str, ...] = ()


ENCODER_PRIORITY: tuple[EncoderChoice, ...] = (
    EncoderChoice(
        name="h264_nvenc",
        output_args=(
            "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
            "-rc", "vbr", "-cq", "23", "-b:v", "0",
        ),
    ),
    EncoderChoice(
        name="h264_qsv",
        output_args=("-c:v", "h264_qsv", "-global_quality", "23", "-preset", "medium"),
        filter_suffix=",format=nv12",
    ),
    EncoderChoice(
        name="h264_vaapi",
        output_args=("-c:v", "h264_vaapi", "-qp", "25"),
        filter_suffix=",format=nv12,hwupload",
        # renderD128 covers the overwhelmingly common single-GPU case; on an
        # exotic multi-GPU host this probe just fails and selection moves on.
        pre_input_args=("-vaapi_device", "/dev/dri/renderD128"),
    ),
    # Terminal entry — the universal software floor. Args are byte-identical
    # to the pre-feature hardcoded command (regression-locked by unit test).
    EncoderChoice(
        name="libx264",
        output_args=("-c:v", "libx264", "-crf", "23", "-preset", "fast"),
    ),
)

SOFTWARE_FALLBACK = ENCODER_PRIORITY[-1]


def _run_probe(choice: EncoderChoice) -> bool:
    """Trial-encode 1s of synthetic video through the candidate's exact
    production arg shape (including the software-filter → upload step)."""
    cmd = [
        "ffmpeg", "-hide_banner", "-v", "error",
        *choice.pre_input_args,
        "-f", "lavfi", "-i", "color=black:size=320x240:rate=8:duration=1",
        "-vf", "null" + choice.filter_suffix,
        *choice.output_args,
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def select_encoder(
    configured: str,
    probe: Callable[[EncoderChoice], bool] = _run_probe,
) -> EncoderChoice:
    """Pure selection logic (probe injectable for tests).

    configured == "auto": first candidate whose probe passes wins; libx264 is
    accepted without probing (never brick rendering over a probe).
    configured == a known name: probe only it; on failure fall back to auto
    with a loud warning (a typo'd/wrong VIDEO_ENCODER must degrade to working
    software encode, not a render outage).
    """
    if configured != "auto":
        forced = next((c for c in ENCODER_PRIORITY if c.name == configured), None)
        if forced is None:
            logger.warning(
                "VIDEO_ENCODER=%r is not a known encoder (choices: %s); "
                "using auto detection",
                configured, ", ".join(c.name for c in ENCODER_PRIORITY),
            )
        elif forced.name == SOFTWARE_FALLBACK.name or probe(forced):
            logger.info("video encoder: %s (forced via VIDEO_ENCODER)", forced.name)
            return forced
        else:
            logger.warning(
                "VIDEO_ENCODER=%s failed its probe on this host; "
                "using auto detection", configured,
            )

    attempts = []
    for candidate in ENCODER_PRIORITY:
        if candidate.name == SOFTWARE_FALLBACK.name or probe(candidate):
            attempts.append(f"{candidate.name} ok")
            logger.info(
                "video encoder: %s (probed: %s)",
                candidate.name, ", ".join(attempts),
            )
            return candidate
        attempts.append(f"{candidate.name} unavailable")
    return SOFTWARE_FALLBACK  # defensive; the loop always returns at libx264


_selected: Optional[EncoderChoice] = None


def detect_encoder() -> EncoderChoice:
    """Best working encoder for this host, probed once per process on first
    render (lazy: a worker that only transcribes never pays for the probe,
    and the cache lives in the prefork child that actually encodes)."""
    global _selected
    if _selected is None:
        from ..config import settings  # lazy: keeps module stdlib-only for unit tests

        _selected = select_encoder(settings.video_encoder)
    return _selected
