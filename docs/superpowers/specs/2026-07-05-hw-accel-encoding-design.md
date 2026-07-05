# Hardware-Accelerated Video Encoding — Design

**Date:** 2026-07-05
**Status:** Approved

## Problem

The caption burn (`backend/app/tasks/ffmpeg_burn.py`) re-encodes video with
`libx264 -crf 23 -preset fast` on the CPU. Encoding dominates phase-2 render
time. The production host (Intel N150, native Linux + Docker) has Quick Sync
sitting idle; other users' machines and possible future cloud workers have
different hardware (NVIDIA, AMD, none). The app should detect the best
available hardware encoder at runtime and use it, falling back to libx264,
with zero required configuration.

## Decisions (from brainstorming)

- **Deployment target:** production worker runs on native Linux + Docker on an
  Intel N150 → QSV/VAAPI reachable via `/dev/dri` passthrough. Dev happens on
  Docker Desktop/WSL2, where no `/dev/dri` exists — dev always exercises the
  libx264 fallback path.
- **Quality policy:** visual parity with today's libx264 CRF 23. Hardware
  encoders are tuned to an equivalent quality target (files may be somewhat
  larger); no separate quality knob.
- **Approach:** trial-encode probe + priority registry (chosen over
  config-only and over capability-listing; `ffmpeg -encoders` lists `h264_qsv`
  even on hosts where it cannot run, so only an actual encode is trustworthy).
- **NVENC included now:** the probe makes it self-verifying, so support for
  future NVIDIA cloud workers costs nothing and cannot mis-fire.

## Architecture

One new module, `backend/app/tasks/encoder.py`, owns encoder choice:

- `EncoderChoice` — frozen dataclass: `name`, `output_args` (list of `-c:v`
  and quality/preset flags), `filter_suffix` (appended to the `-vf` chain,
  e.g. `,format=nv12,hwupload`), `pre_input_args` (e.g. `-vaapi_device ...`).
- `detect_encoder() -> EncoderChoice` — probes on first call, caches in a
  module global for the process lifetime (same pattern as
  `transcribe.get_model()`). Lazy: probed on the first render, not at worker
  boot, so the result lives in the prefork child that encodes.

### Registry (priority order, parity-tuned)

| Encoder | Output args | Filter suffix | Pre-input args |
|---|---|---|---|
| `h264_nvenc` | `-c:v h264_nvenc -preset p5 -tune hq -rc vbr -cq 23 -b:v 0` | — | — |
| `h264_qsv` | `-c:v h264_qsv -global_quality 23 -preset medium` | `,format=nv12` | — |
| `h264_vaapi` | `-c:v h264_vaapi -qp 25` | `,format=nv12,hwupload` | `-vaapi_device /dev/dri/renderD128` |
| `libx264` | `-c:v libx264 -crf 23 -preset fast` | — | — |

The ASS subtitle filter always renders in software (CPU-only by nature);
hardware paths upload frames to the GPU *after* captioning, which is why the
upload step travels with the encoder choice.

`burn_subtitles()` changes minimally: it splices the chosen encoder's three
arg groups into the command it already builds. Progress reporting
(`-progress pipe:1`) is encoder-independent and unchanged.

### Probe

For each candidate in priority order, run a ~1s synthetic encode exercising
the production arg shape:

```
ffmpeg -f lavfi -i color=black:size=320x240:rate=8:duration=1 \
  [pre_input_args] -vf "null[filter_suffix]" [output_args] -f null -
```

First exit-0 candidate wins. The selection is logged once at INFO, e.g.
`video encoder: h264_qsv (probed: h264_nvenc ✗, h264_qsv ✓)`.

## Configuration

- `config.py`: `video_encoder: str = "auto"` (env `VIDEO_ENCODER`), values
  `auto | h264_nvenc | h264_qsv | h264_vaapi | libx264`. Added to compose
  `x-app-env` as `${VIDEO_ENCODER:-auto}`; documented in `.env.example` and
  CLAUDE.md.
- A specific (non-`auto`) value probes only that encoder.

### Compose / device access

- New optional override `docker-compose.hwaccel.yml`: adds
  `devices: ["/dev/dri:/dev/dri"]` to the **worker** service only (backend
  and beat never encode). Opt-in on hardware hosts:
  `docker compose -f docker-compose.yml -f docker-compose.hwaccel.yml up`
  or `COMPOSE_FILE` in `.env`. It cannot live in the main compose file — a
  `devices:` entry for a missing node fails container creation on GPU-less
  hosts.
- NVIDIA workers are documented (add `gpus: all` + NVIDIA container toolkit),
  not pre-built — untestable until such a worker exists.

### Entrypoint

`docker-entrypoint.sh` runs as root before dropping to `app`. New step: if
`/dev/dri/renderD*` exists, read the node's GID and add `app` to that group
(the `render` group GID varies by host). No-op on hosts without the device.

## Error handling

Three independent layers:

1. **Probe failure** — candidate skipped silently, next tried. `libx264` is
   the terminal entry and is used even if its own probe somehow fails
   (never brick rendering over a probe).
2. **Forced encoder fails its probe** — loud WARNING, degrade to `auto`
   behavior. A typo'd `VIDEO_ENCODER` yields working software encode, not a
   render outage.
3. **Real render fails on a hardware encoder** (probe passed; file trips a
   driver edge case — odd resolution, 10-bit source): `render_video` catches
   the error, logs the stderr tail, retries that job once with `libx264`,
   and only then marks it failed. The cached choice is **not** globally
   demoted by one failure — one weird file must not disable acceleration for
   subsequent jobs.

## Testing

- **Unit tests** (first in the repo; live in `backend/tests/`, no stack
  needed): registry ordering, forced-override paths, probe-failure
  fallthrough, arg splicing — with a faked probe runner.
- **E2E on dev (WSL2, no /dev/dri):** existing render tests double as the
  fallback-path test. One new e2e assertion: render completes with
  `VIDEO_ENCODER=h264_qsv` forced on a host without QSV (layer-2 fallback
  end-to-end).
- **N150 smoke check (manual, documented):** run the stack with the hwaccel
  override, render the Gehrig fixture, confirm the worker log line names
  `h264_qsv`, compare wall time against the libx264 baseline.

## Out of scope

- HEVC/AV1 output, 10-bit handling, quality knob (`ENCODER_QUALITY`),
  AMD AMF, face-tracking crop, decode acceleration (`-hwaccel` input side —
  the decode cost is minor next to encode).
- Pre-built NVIDIA compose stanza (documented only).

## Environment notes (verified 2026-07-05)

- The existing image's Debian FFmpeg already has `h264_nvenc`, `h264_qsv`,
  `h264_vaapi` compiled in, with `intel-media-va-driver` and `libvpl2`
  installed — no Dockerfile changes required for Intel support.
- Dev WSL2 box: no `/dev/dri`; hardware paths unreachable in containers
  there by design of the test plan.
