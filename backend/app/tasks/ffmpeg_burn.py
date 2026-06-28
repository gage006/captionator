import json
import subprocess


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


def burn_subtitles(input_path: str, ass_path: str, output_path: str) -> None:
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
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg error:\n{result.stderr[-2000:]}")
