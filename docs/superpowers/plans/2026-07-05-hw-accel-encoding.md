# Hardware-Accelerated Video Encoding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-detect the best available hardware H.264 encoder (NVENC > QSV > VAAPI) for the caption burn via a cached trial-encode probe, falling back to libx264, with `VIDEO_ENCODER` override and per-job software retry.

**Architecture:** New `backend/app/tasks/encoder.py` owns an `EncoderChoice` registry, a real-encode probe, and pure selection logic (`select_encoder`) wrapped by a process-cached `detect_encoder()`. `ffmpeg_burn.py` splices the chosen encoder's args into its command and gains `burn_subtitles_with_fallback()` (retry-once-with-libx264). `render_video` calls the fallback wrapper. Device access arrives via an optional compose override + entrypoint group grant.

**Tech Stack:** Python 3.11, FFmpeg (Debian build in existing image — already has nvenc/qsv/vaapi compiled in), pytest, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-07-05-hw-accel-encoding-design.md`

## Global Constraints

- `encoder.py` must import ONLY stdlib at module level (settings imported lazily inside `detect_encoder()`): the pytest host lacks `pydantic_settings`, and unit tests must run without the Docker stack.
- Quality policy is visual parity with today's `libx264 -crf 23 -preset fast`; the libx264 registry entry must keep those args byte-for-byte (regression-locked by test).
- `libx264` is the terminal registry entry and is never probed-out — selection must return it even if its own probe would fail.
- One hardware render failure must NOT demote the cached choice for later jobs.
- All work on branch `hw-accel-encoding`; commit after each task.
- Tests: `~/.local/bin/pytest backend/tests/test_encoder_selection.py -v` from the repo root (host pytest; unit tests need no stack).

---

### Task 0: Branch

- [ ] **Step 1: Create feature branch**

```bash
git checkout -b hw-accel-encoding
```

---

### Task 1: Encoder registry + selection logic (`encoder.py`)

**Files:**
- Create: `backend/app/tasks/encoder.py`
- Test: `backend/tests/test_encoder_selection.py`

**Interfaces:**
- Produces: `EncoderChoice` (frozen dataclass: `name: str`, `output_args: tuple[str, ...]`, `filter_suffix: str = ""`, `pre_input_args: tuple[str, ...] = ()`); `ENCODER_PRIORITY: tuple[EncoderChoice, ...]`; `SOFTWARE_FALLBACK: EncoderChoice` (the libx264 entry); `select_encoder(configured: str, probe=_run_probe) -> EncoderChoice` (pure, injectable probe); `detect_encoder() -> EncoderChoice` (cached, reads `settings.video_encoder` lazily).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_encoder_selection.py`:

```python
"""Unit tests for hardware-encoder selection — no Docker stack required.

encoder.py is deliberately stdlib-only at import time and select_encoder()
takes an injectable probe, so every selection path is testable on any host.
"""
from app.tasks.encoder import (
    ENCODER_PRIORITY,
    SOFTWARE_FALLBACK,
    EncoderChoice,
    select_encoder,
)


def test_registry_priority_order():
    assert [c.name for c in ENCODER_PRIORITY] == [
        "h264_nvenc", "h264_qsv", "h264_vaapi", "libx264",
    ]
    assert SOFTWARE_FALLBACK is ENCODER_PRIORITY[-1]
    assert SOFTWARE_FALLBACK.name == "libx264"


def test_libx264_entry_matches_legacy_args_exactly():
    # Quality-parity regression lock: the software path must stay identical
    # to the pre-feature hardcoded command.
    assert SOFTWARE_FALLBACK.output_args == (
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
    )
    assert SOFTWARE_FALLBACK.filter_suffix == ""
    assert SOFTWARE_FALLBACK.pre_input_args == ()


def test_auto_picks_first_probe_success():
    chosen = select_encoder("auto", probe=lambda c: c.name == "h264_qsv")
    assert chosen.name == "h264_qsv"


def test_auto_all_probes_fail_returns_libx264_without_probing_it():
    probed = []

    def probe(choice):
        probed.append(choice.name)
        return False

    chosen = select_encoder("auto", probe=probe)
    assert chosen.name == "libx264"
    # libx264 is terminal: selected even though every probe said no, and its
    # own probe is never run.
    assert "libx264" not in probed
    assert probed == ["h264_nvenc", "h264_qsv", "h264_vaapi"]


def test_forced_encoder_probes_only_that_encoder():
    probed = []

    def probe(choice):
        probed.append(choice.name)
        return True

    chosen = select_encoder("h264_vaapi", probe=probe)
    assert chosen.name == "h264_vaapi"
    assert probed == ["h264_vaapi"]


def test_forced_encoder_probe_failure_degrades_to_auto():
    # h264_vaapi forced but unusable; auto detection then finds nvenc.
    def probe(choice):
        return choice.name == "h264_nvenc"

    chosen = select_encoder("h264_vaapi", probe=probe)
    assert chosen.name == "h264_nvenc"


def test_forced_unknown_name_degrades_to_auto():
    chosen = select_encoder("h265_magic", probe=lambda c: False)
    assert chosen.name == "libx264"


def test_forced_libx264_never_probes():
    def probe(choice):
        raise AssertionError("libx264 must be accepted without probing")

    chosen = select_encoder("libx264", probe=probe)
    assert chosen.name == "libx264"


def test_encoder_choice_is_immutable():
    import dataclasses
    import pytest

    with pytest.raises(dataclasses.FrozenInstanceError):
        ENCODER_PRIORITY[0].name = "x"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `~/.local/bin/pytest backend/tests/test_encoder_selection.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'app.tasks.encoder'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/tasks/encoder.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `~/.local/bin/pytest backend/tests/test_encoder_selection.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/tasks/encoder.py backend/tests/test_encoder_selection.py
git commit -m "feat: encoder registry + probe-based selection for hw-accelerated burn"
```

---

### Task 2: Splice encoder into the burn command + software retry wrapper

**Files:**
- Modify: `backend/app/tasks/ffmpeg_burn.py`
- Test: `backend/tests/test_encoder_selection.py` (append)

**Interfaces:**
- Consumes: `EncoderChoice`, `SOFTWARE_FALLBACK`, `detect_encoder` from Task 1.
- Produces: `_build_cmd(input_path: str, ass_path: str, output_path: str, audio_codec: Optional[str], encoder: EncoderChoice) -> list[str]`; `burn_subtitles(..., encoder: Optional[EncoderChoice] = None)` (new trailing kwarg, `None` → `detect_encoder()`); `burn_subtitles_with_fallback(input_path, ass_path, output_path, progress_callback=None, media_info=None) -> str` (returns the name of the encoder that produced the output).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_encoder_selection.py`:

```python
# ---------------------------------------------------------------------------
# Command splicing + hardware-failure fallback (ffmpeg_burn integration points)
# ---------------------------------------------------------------------------
from app.tasks import ffmpeg_burn
from app.tasks.ffmpeg_burn import _build_cmd, burn_subtitles_with_fallback


def _entry(name):
    return next(c for c in ENCODER_PRIORITY if c.name == name)


def test_build_cmd_libx264_matches_legacy_command_exactly():
    cmd = _build_cmd("in.mp4", "/out/captions.ass", "out.mp4", "aac", _entry("libx264"))
    assert cmd == [
        "ffmpeg", "-y",
        "-i", "in.mp4",
        "-vf", "ass=/out/captions.ass",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "copy",
        "-progress", "pipe:1", "-nostats",
        "out.mp4",
    ]


def test_build_cmd_transcodes_non_aac_audio():
    cmd = _build_cmd("in.mp4", "c.ass", "out.mp4", "mp3", _entry("libx264"))
    idx = cmd.index("-c:a")
    assert cmd[idx : idx + 4] == ["-c:a", "aac", "-b:a", "128k"]


def test_build_cmd_vaapi_places_device_before_input_and_uploads_after_ass():
    cmd = _build_cmd("in.mp4", "c.ass", "out.mp4", "aac", _entry("h264_vaapi"))
    assert cmd.index("-vaapi_device") < cmd.index("-i")
    assert cmd[cmd.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
    assert cmd[cmd.index("-vf") + 1] == "ass=c.ass,format=nv12,hwupload"
    assert "-crf" not in cmd


def test_build_cmd_qsv_appends_format_suffix():
    cmd = _build_cmd("in.mp4", "c.ass", "out.mp4", "aac", _entry("h264_qsv"))
    assert cmd[cmd.index("-vf") + 1] == "ass=c.ass,format=nv12"
    assert "h264_qsv" in cmd


def test_build_cmd_escapes_ass_path():
    cmd = _build_cmd("in.mp4", "C:\\tmp\\c.ass", "out.mp4", "aac", _entry("libx264"))
    assert cmd[cmd.index("-vf") + 1] == "ass=C\\:/tmp/c.ass"


def test_fallback_retries_hw_failure_with_libx264(monkeypatch):
    calls = []

    def fake_burn(input_path, ass_path, output_path,
                  progress_callback=None, media_info=None, encoder=None):
        calls.append(encoder.name)
        if encoder.name != "libx264":
            raise RuntimeError("FFmpeg error:\nsimulated driver failure")

    monkeypatch.setattr(ffmpeg_burn, "detect_encoder", lambda: _entry("h264_qsv"))
    monkeypatch.setattr(ffmpeg_burn, "burn_subtitles", fake_burn)

    used = burn_subtitles_with_fallback("in.mp4", "c.ass", "out.mp4")
    assert used == "libx264"
    assert calls == ["h264_qsv", "libx264"]


def test_fallback_reports_hw_encoder_on_success(monkeypatch):
    monkeypatch.setattr(ffmpeg_burn, "detect_encoder", lambda: _entry("h264_qsv"))
    monkeypatch.setattr(
        ffmpeg_burn, "burn_subtitles",
        lambda *a, **k: None,
    )
    assert burn_subtitles_with_fallback("in.mp4", "c.ass", "out.mp4") == "h264_qsv"


def test_fallback_does_not_mask_software_failure(monkeypatch):
    import pytest

    def fake_burn(*args, **kwargs):
        raise RuntimeError("FFmpeg error:\ndisk full")

    monkeypatch.setattr(ffmpeg_burn, "detect_encoder", lambda: _entry("libx264"))
    monkeypatch.setattr(ffmpeg_burn, "burn_subtitles", fake_burn)

    with pytest.raises(RuntimeError, match="disk full"):
        burn_subtitles_with_fallback("in.mp4", "c.ass", "out.mp4")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `~/.local/bin/pytest backend/tests/test_encoder_selection.py -v`
Expected: ImportError — `cannot import name '_build_cmd' from 'app.tasks.ffmpeg_burn'`

- [ ] **Step 3: Modify `backend/app/tasks/ffmpeg_burn.py`**

Add imports + logger at the top (after existing imports):

```python
import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional

from .encoder import EncoderChoice, SOFTWARE_FALLBACK, detect_encoder

logger = logging.getLogger(__name__)
```

Replace the body of `burn_subtitles` from its signature down to (not including) the `duration = ...` line with:

```python
def _build_cmd(
    input_path: str,
    ass_path: str,
    output_path: str,
    audio_codec: Optional[str],
    encoder: EncoderChoice,
) -> list[str]:
    # ass= filter requires the path to use forward slashes and colons escaped
    # on some platforms
    safe_ass_path = ass_path.replace("\\", "/").replace(":", "\\:")
    # Burning subtitles forces a video re-encode, but the audio is untouched —
    # so stream-copy it when it's already AAC (mp4-native) and only transcode
    # otherwise.
    if audio_codec == "aac":
        audio_args = ["-c:a", "copy"]
    else:
        audio_args = ["-c:a", "aac", "-b:a", "128k"]
    return [
        "ffmpeg", "-y",
        *encoder.pre_input_args,
        "-i", input_path,
        "-vf", f"ass={safe_ass_path}" + encoder.filter_suffix,
        *encoder.output_args,
        *audio_args,
        # Machine-readable progress to stdout; suppress the human stats line.
        "-progress", "pipe:1", "-nostats",
        output_path,
    ]


def burn_subtitles(
    input_path: str,
    ass_path: str,
    output_path: str,
    progress_callback: Optional[Callable[[float], None]] = None,
    media_info: Optional[MediaInfo] = None,
    encoder: Optional[EncoderChoice] = None,
) -> None:
    # Callers that already probed the input (render_video needs the dimensions
    # for the ASS header anyway) pass their MediaInfo to avoid a second probe.
    info = media_info or probe_media(input_path)
    enc = encoder or detect_encoder()
    cmd = _build_cmd(input_path, ass_path, output_path, info.audio_codec, enc)
```

(The rest of `burn_subtitles` — `duration = ...`, the Popen/progress/stderr-tail block — is unchanged.)

Append at the end of the file:

```python
def burn_subtitles_with_fallback(
    input_path: str,
    ass_path: str,
    output_path: str,
    progress_callback: Optional[Callable[[float], None]] = None,
    media_info: Optional[MediaInfo] = None,
) -> str:
    """Burn with the detected encoder; if a HARDWARE encoder fails on this
    file (probe passed, but e.g. an odd resolution trips the driver), retry
    once with libx264 so no user job dies over an encoder quirk. One failure
    does NOT demote the cached choice — the next job tries hardware again.
    Returns the name of the encoder that produced the output."""
    enc = detect_encoder()
    try:
        burn_subtitles(
            input_path, ass_path, output_path,
            progress_callback=progress_callback,
            media_info=media_info,
            encoder=enc,
        )
        return enc.name
    except RuntimeError as exc:
        if enc.name == SOFTWARE_FALLBACK.name:
            raise
        logger.warning(
            "hardware encoder %s failed (%s); retrying with %s",
            enc.name, exc, SOFTWARE_FALLBACK.name,
        )
        burn_subtitles(
            input_path, ass_path, output_path,
            progress_callback=progress_callback,
            media_info=media_info,
            encoder=SOFTWARE_FALLBACK,
        )
        return SOFTWARE_FALLBACK.name
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `~/.local/bin/pytest backend/tests/test_encoder_selection.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/tasks/ffmpeg_burn.py backend/tests/test_encoder_selection.py
git commit -m "feat: splice detected encoder into burn command; retry hw failures on libx264"
```

---

### Task 3: Wire `render_video` to the fallback wrapper

**Files:**
- Modify: `backend/app/tasks/pipeline.py` (import at line 14; burn call around lines 240-247; completion log around line 258)

**Interfaces:**
- Consumes: `burn_subtitles_with_fallback` from Task 2.

- [ ] **Step 1: Update the import**

In `backend/app/tasks/pipeline.py` change:

```python
from .ffmpeg_burn import probe_media, get_video_duration, burn_subtitles
```

to:

```python
from .ffmpeg_burn import probe_media, get_video_duration, burn_subtitles_with_fallback
```

- [ ] **Step 2: Replace the burn call in `render_video`**

Change:

```python
        output_video = str(output_dir / "output.mp4")
        burn_subtitles(
            video_path,
            str(ass_path),
            output_video,
            progress_callback=on_burn_progress,
            media_info=media_info,
        )
```

to:

```python
        output_video = str(output_dir / "output.mp4")
        encoder_used = burn_subtitles_with_fallback(
            video_path,
            str(ass_path),
            output_video,
            progress_callback=on_burn_progress,
            media_info=media_info,
        )
```

- [ ] **Step 3: Name the encoder in the completion log**

Change:

```python
        logger.info("render complete: job=%s took=%.1fs", job_id, time.monotonic() - started)
```

to:

```python
        logger.info(
            "render complete: job=%s encoder=%s took=%.1fs",
            job_id, encoder_used, time.monotonic() - started,
        )
```

- [ ] **Step 4: Sanity check + full unit run**

Run: `python3 -m py_compile backend/app/tasks/pipeline.py && ~/.local/bin/pytest backend/tests/test_encoder_selection.py -v`
Expected: compiles; 17 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/tasks/pipeline.py
git commit -m "feat: render_video burns via encoder fallback wrapper, logs encoder used"
```

---

### Task 4: Config, compose env, hwaccel override, entrypoint group grant

**Files:**
- Modify: `backend/app/config.py` (after `whisper_cpu_threads`, line 22)
- Modify: `docker-compose.yml` (x-app-env block)
- Create: `docker-compose.hwaccel.yml`
- Modify: `backend/docker-entrypoint.sh`
- Modify: `.env.example`

- [ ] **Step 1: Add the setting**

In `backend/app/config.py` after the `whisper_cpu_threads: int = 0` line add:

```python
    # FFmpeg video encoder for the caption burn. "auto" (default) probes for a
    # working hardware encoder at the worker's first render — h264_nvenc, then
    # h264_qsv, then h264_vaapi — and falls back to libx264. Force one of those
    # names to skip detection (a forced encoder that fails its probe degrades
    # back to auto with a warning). Hardware encoders additionally need the GPU
    # device passed into the worker container: see docker-compose.hwaccel.yml.
    video_encoder: str = "auto"
```

- [ ] **Step 2: Add to compose env block**

In `docker-compose.yml` x-app-env, after `WHISPER_CPU_THREADS: ${WHISPER_CPU_THREADS:-0}` add:

```yaml
  VIDEO_ENCODER: ${VIDEO_ENCODER:-auto}
```

- [ ] **Step 3: Create `docker-compose.hwaccel.yml`**

```yaml
# Opt-in override granting the worker access to the host GPU's render node so
# the h264_qsv / h264_vaapi probes can succeed (Intel/AMD). This cannot live in
# docker-compose.yml: a devices: entry for a node the host doesn't have fails
# container creation outright on GPU-less hosts (e.g. Docker Desktop on WSL2).
#
# Usage:
#   docker compose -f docker-compose.yml -f docker-compose.hwaccel.yml up -d
# or persistently, in .env:
#   COMPOSE_FILE=docker-compose.yml:docker-compose.hwaccel.yml
#
# Smoke check after a render: `docker compose logs worker | grep "video encoder"`
# should name h264_qsv (Intel) — and the render_video completion line reports
# the encoder each job actually used.
#
# NVIDIA hosts: don't use this file; instead install the NVIDIA container
# toolkit and add a worker override with `gpus: all` — h264_nvenc probes first.
# Only the worker encodes; backend/beat never need the device.
services:
  worker:
    devices:
      - /dev/dri:/dev/dri
```

- [ ] **Step 4: Entrypoint group grant**

In `backend/docker-entrypoint.sh`, inside the `if [ "$(id -u)" = "0" ]; then` block, after the `for d in ...; done` chown loop and before `exec gosu app "$@"`, add:

```sh
    # A GPU render node passed through via docker-compose.hwaccel.yml is owned
    # by the host's render group, whose GID varies per host — grant "app"
    # membership in whatever group actually owns each node so the h264_qsv /
    # h264_vaapi probes can open it after privileges drop. No-op without /dev/dri.
    for node in /dev/dri/renderD*; do
        [ -e "$node" ] || continue
        gid=$(stat -c '%g' "$node")
        group=$(getent group "$gid" | cut -d: -f1)
        if [ -z "$group" ]; then
            group="render$gid"
            groupadd -g "$gid" "$group"
        fi
        usermod -aG "$group" app
    done
```

- [ ] **Step 5: Document in `.env.example`**

After the `WHISPER_CPU_THREADS=0` block add:

```
# Video encoder for the caption burn. "auto" probes for a working hardware
# encoder at the worker's first render (h264_nvenc > h264_qsv > h264_vaapi)
# and falls back to libx264. Hardware encoders need the GPU passed into the
# worker container — see docker-compose.hwaccel.yml. Force a specific value
# (h264_nvenc / h264_qsv / h264_vaapi / libx264) to skip detection.
VIDEO_ENCODER=auto
```

- [ ] **Step 6: Validate compose + shell syntax**

Run: `docker compose -f docker-compose.yml -f docker-compose.hwaccel.yml config --quiet && sh -n backend/docker-entrypoint.sh && echo OK`
Expected: OK

- [ ] **Step 7: Commit**

```bash
git add backend/app/config.py docker-compose.yml docker-compose.hwaccel.yml backend/docker-entrypoint.sh .env.example
git commit -m "feat: VIDEO_ENCODER setting, hwaccel compose override, render-node group grant"
```

---

### Task 5: Documentation (CLAUDE.md)

**Files:**
- Modify: `CLAUDE.md` (backend module map table, config.py row, Environment section)

- [ ] **Step 1: Module map** — add a row after `tasks/emphasis.py`:

```markdown
| `tasks/encoder.py` | Encoder registry + trial-encode probe: `detect_encoder()` picks the best working H.264 encoder (h264_nvenc > h264_qsv > h264_vaapi > libx264) once per process, honoring `VIDEO_ENCODER`; `select_encoder` is the pure, probe-injectable core. Stdlib-only at import time so its unit tests run without the stack |
```

Update the `tasks/ffmpeg_burn.py` row description to end with: `; burns via the detected encoder, and burn_subtitles_with_fallback retries a failed hardware encode once on libx264`.

Update the `config.py` row's env list to include `VIDEO_ENCODER`.

- [ ] **Step 2: Environment section** — add to the tunables block:

```
VIDEO_ENCODER=auto            # caption-burn encoder: auto-probe hw (nvenc>qsv>vaapi) with libx264 fallback, or force one
```

And after the `SILENCE_*` paragraph add:

```markdown
Hardware-accelerated encoding: `VIDEO_ENCODER=auto` probes for a working GPU encoder at the worker's first render and burns captions with it (visual parity targets vs. libx264 CRF 23). The GPU must be passed into the worker container — opt in with `docker compose -f docker-compose.yml -f docker-compose.hwaccel.yml up` (Intel/AMD `/dev/dri`; see that file's comments for NVIDIA). Without the device (e.g. Docker Desktop on WSL2) every probe fails and the burn uses libx264 exactly as before. A hardware encode that fails on a specific file is retried once with libx264; the job only fails if software encoding also fails.
```

- [ ] **Step 3: Also note the unit-test exception** in the Tests section: change "There are no unit tests; coverage is end-to-end against the running services." to "Coverage is mostly end-to-end against the running services; the exception is `backend/tests/test_encoder_selection.py` — pure unit tests for encoder selection that run without the stack."

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document VIDEO_ENCODER, hwaccel compose override, encoder module"
```

---

### Task 6: End-to-end verification

- [ ] **Step 1: Rebuild the backend image and start the stack**

Run: `docker compose up -d --build` (from repo root)
Expected: all 5 services healthy (`docker compose ps`)

- [ ] **Step 2: Full backend test suite against the stack**

Run: `~/.local/bin/pytest backend/tests/ -v`
Expected: all pass (renders exercise auto-detection; on this /dev/dri-less host every hw probe fails → libx264, proving the fallback chain end-to-end). Confirm with: `docker compose logs worker | grep "video encoder"` → `video encoder: libx264 (probed: h264_nvenc unavailable, h264_qsv unavailable, h264_vaapi unavailable, libx264 ok)`

- [ ] **Step 3: Forced-encoder degradation check (spec layer 2, e2e)**

Run: `VIDEO_ENCODER=h264_qsv docker compose up -d worker` then re-run one render test: `~/.local/bin/pytest backend/tests/test_real_speech_silence_removal.py -k silence_removal_preserves -v`
Expected: passes; worker log shows the `failed its probe on this host; using auto detection` warning followed by `video encoder: libx264`. Then restore: `docker compose up -d worker` (unsets the forced value).

- [ ] **Step 4: Hardware probe dry-run (proves probe args are valid FFmpeg)**

Run each probe command manually in the worker container to confirm the failures are *device* failures, not arg typos — libx264's probe must succeed and the hw ones must fail with a device/driver error (not "Unrecognized option"):

```bash
docker compose exec worker sh -c 'ffmpeg -hide_banner -v error -f lavfi -i color=black:size=320x240:rate=8:duration=1 -vf null -c:v libx264 -crf 23 -preset fast -f null - && echo LIBX264_OK'
docker compose exec worker sh -c 'ffmpeg -hide_banner -v error -vaapi_device /dev/dri/renderD128 -f lavfi -i color=black:size=320x240:rate=8:duration=1 -vf null,format=nv12,hwupload -c:v h264_vaapi -qp 25 -f null -; echo "vaapi exit=$?"'
```

- [ ] **Step 5: Commit any fixes, then finish**

Use superpowers:finishing-a-development-branch (merge `hw-accel-encoding` → main per repo convention).

**N150 activation (manual, post-merge — for the user):** on the N150 host: `git pull`, add `COMPOSE_FILE=docker-compose.yml:docker-compose.hwaccel.yml` to `.env`, `docker compose pull && docker compose up -d`, render once, then `docker compose logs worker | grep "video encoder"` should say `h264_qsv`.
