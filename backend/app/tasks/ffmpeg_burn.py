import json
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class MediaInfo:
    width: int
    height: int
    duration: float  # 0.0 when unavailable (callers skip progress reporting)
    audio_codec: Optional[str]


def probe_media(video_path: str) -> MediaInfo:
    """One ffprobe pass for everything the render pipeline needs — dimensions
    (ASS PlayRes), duration (progress fraction denominator), and audio codec
    (stream-copy vs. transcode) — instead of a probe per question."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            video_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)

    width, height = 1920, 1080
    audio_codec = None
    stream_duration = None
    found_video = False
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not found_video:
            found_video = True
            width, height = stream["width"], stream["height"]
        elif stream.get("codec_type") == "audio" and audio_codec is None:
            audio_codec = stream.get("codec_name")
        if stream_duration is None and stream.get("duration"):
            stream_duration = float(stream["duration"])

    # Prefer the container duration; fall back to a stream's if the container
    # omits format.duration.
    duration = float(data.get("format", {}).get("duration") or stream_duration or 0.0)

    return MediaInfo(width=width, height=height, duration=duration, audio_codec=audio_codec)


def get_video_duration(video_path: str) -> float:
    """Total duration in seconds, used to turn FFmpeg's elapsed output time into a
    0..1 progress fraction. Returns 0.0 when unavailable (callers then skip
    progress reporting rather than divide by zero)."""
    return probe_media(video_path).duration


def burn_subtitles(
    input_path: str,
    ass_path: str,
    output_path: str,
    progress_callback: Optional[Callable[[float], None]] = None,
    media_info: Optional[MediaInfo] = None,
) -> None:
    # Callers that already probed the input (render_video needs the dimensions
    # for the ASS header anyway) pass their MediaInfo to avoid a second probe.
    info = media_info or probe_media(input_path)
    # ass= filter requires the path to use forward slashes and colons escaped on some platforms
    safe_ass_path = ass_path.replace("\\", "/").replace(":", "\\:")
    # Burning subtitles forces a video re-encode, but the audio is untouched — so
    # stream-copy it when it's already AAC (mp4-native) and only transcode otherwise.
    if info.audio_codec == "aac":
        audio_args = ["-c:a", "copy"]
    else:
        audio_args = ["-c:a", "aac", "-b:a", "128k"]
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", f"ass={safe_ass_path}",
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "fast",
        *audio_args,
        # Machine-readable progress to stdout; suppress the human stats line.
        "-progress", "pipe:1", "-nostats",
        output_path,
    ]

    duration = info.duration if progress_callback else 0.0

    # Stream stdout for progress while sending stderr (the encoder log) to a temp
    # file. Draining only one pipe inline avoids the classic deadlock where a
    # full, unread stderr buffer stalls FFmpeg; on failure we read the file's tail.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err_file:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=err_file,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if not (progress_callback and duration):
                continue
            line = line.strip()
            # FFmpeg emits out_time_us (µs); older builds emit out_time_ms (also µs).
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                value = line.split("=", 1)[1]
                if value.isdigit():
                    progress_callback(min(1.0, int(value) / 1_000_000 / duration))
        returncode = proc.wait()
        if returncode != 0:
            err_file.seek(0)
            tail = err_file.read()[-2000:]
            raise RuntimeError(f"FFmpeg error:\n{tail}")
