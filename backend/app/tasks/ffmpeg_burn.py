import json
import subprocess
import tempfile
from typing import Callable, Optional


def get_video_dimensions(video_path: str) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            video_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            return stream["width"], stream["height"]
    return 1920, 1080


def get_audio_codec(video_path: str) -> str | None:
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            video_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "audio":
            return stream.get("codec_name")
    return None


def get_video_duration(video_path: str) -> float:
    """Total duration in seconds, used to turn FFmpeg's elapsed output time into a
    0..1 progress fraction. Returns 0.0 when unavailable (callers then skip
    progress reporting rather than divide by zero)."""
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
    duration = data.get("format", {}).get("duration")
    if duration:
        return float(duration)
    # Fall back to the longest stream duration if the container omits format.duration.
    for stream in data.get("streams", []):
        if stream.get("duration"):
            return float(stream["duration"])
    return 0.0


def burn_subtitles(
    input_path: str,
    ass_path: str,
    output_path: str,
    progress_callback: Optional[Callable[[float], None]] = None,
) -> None:
    # ass= filter requires the path to use forward slashes and colons escaped on some platforms
    safe_ass_path = ass_path.replace("\\", "/").replace(":", "\\:")
    # Burning subtitles forces a video re-encode, but the audio is untouched — so
    # stream-copy it when it's already AAC (mp4-native) and only transcode otherwise.
    if get_audio_codec(input_path) == "aac":
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

    duration = get_video_duration(input_path) if progress_callback else 0.0

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
