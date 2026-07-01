import re
import subprocess
import tempfile
from typing import Callable, Optional

from .ffmpeg_burn import get_video_duration

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)\s*\|\s*silence_duration:\s*(-?[\d.]+)")


def detect_silences(video_path: str, noise_db: float, min_duration: float) -> list[tuple[float, float]]:
    """Run FFmpeg's silencedetect filter and parse the silence ranges it reports.

    silencedetect writes its findings to stderr as a small, bounded number of log
    lines (not stdout), so unlike burn_subtitles's per-frame progress stream this
    doesn't need the temp-file-draining dance — a single capture_output run is safe.
    """
    cmd = [
        "ffmpeg", "-i", video_path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg silencedetect failed:\n{result.stderr[-2000:]}")

    starts = [float(m.group(1)) for m in _SILENCE_START_RE.finditer(result.stderr)]
    ends = [(float(m.group(1)), float(m.group(2))) for m in _SILENCE_END_RE.finditer(result.stderr)]

    silences: list[tuple[float, float]] = []
    end_idx = 0
    for start in starts:
        if end_idx < len(ends):
            end, _duration = ends[end_idx]
            end_idx += 1
        else:
            # Silence runs to EOF: ffmpeg only emits silence_end when silence
            # actually ends before the stream does.
            end = get_video_duration(video_path)
        silences.append((max(0.0, start), end))

    return silences


def compute_kept_ranges(
    silences: list[tuple[float, float]],
    total_duration: float,
    padding: float,
    max_segments: int,
) -> list[tuple[float, float]]:
    """Invert silence ranges into kept (speech) ranges, pad them, merge overlaps,
    and cap the result to a bounded number of segments.

    Returns [(0.0, total_duration)] as a no-op signal when there's nothing to
    remove, and [] when removal would consume the entire video (caller must treat
    that as a hard failure rather than silently producing a zero-length output).
    """
    if total_duration <= 0:
        return [(0.0, total_duration)]
    if not silences:
        return [(0.0, total_duration)]

    silences = sorted(silences)
    # Merge any overlapping/adjacent silence ranges first so inversion is clean.
    merged_silences: list[tuple[float, float]] = []
    for s, e in silences:
        s = max(0.0, min(s, total_duration))
        e = max(0.0, min(e, total_duration))
        if e <= s:
            continue
        if merged_silences and s <= merged_silences[-1][1]:
            merged_silences[-1] = (merged_silences[-1][0], max(merged_silences[-1][1], e))
        else:
            merged_silences.append((s, e))

    # Invert into kept ranges.
    kept: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in merged_silences:
        if s > cursor:
            kept.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < total_duration:
        kept.append((cursor, total_duration))

    if not kept:
        return []

    # Pad each kept range outward (i.e. leave a sliver of the adjacent silence in
    # place) so we don't clip word onsets/decays, then re-merge anything that now
    # overlaps as a result.
    padded = [(max(0.0, s - padding), min(total_duration, e + padding)) for s, e in kept]
    remerged: list[tuple[float, float]] = []
    for s, e in padded:
        if remerged and s <= remerged[-1][1]:
            remerged[-1] = (remerged[-1][0], max(remerged[-1][1], e))
        else:
            remerged.append((s, e))
    kept = remerged

    if len(kept) <= max_segments:
        return kept

    # Safety cap: re-merge across the shortest intervening silences first, so the
    # video keeps as much real silence-removal benefit as possible while bounding
    # the eventual filter_complex graph size.
    while len(kept) > max_segments:
        # Find the gap (silence) between consecutive kept ranges with the smallest duration.
        gap_idx = min(range(len(kept) - 1), key=lambda i: kept[i + 1][0] - kept[i][1])
        merged_range = (kept[gap_idx][0], kept[gap_idx + 1][1])
        kept = kept[:gap_idx] + [merged_range] + kept[gap_idx + 2:]

    return kept


def trim_silences(
    video_path: str,
    kept_ranges: list[tuple[float, float]],
    output_path: str,
    progress_callback: Optional[Callable[[float], None]] = None,
) -> None:
    """Physically cut video/audio down to `kept_ranges` via a trim+concat filter
    graph. Cuts aren't keyframe-aligned, so -c:v copy is not an option here — this
    is a real re-encode, separate from (and prior to) the burn step's own re-encode.
    Uses a higher-quality intermediate (crf 18) than the burn step's crf 23 so the
    two unavoidable lossy passes don't visibly compound.
    """
    filters = []
    labels = []
    for i, (start, end) in enumerate(kept_ranges):
        filters.append(f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}]")
        filters.append(f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    n = len(kept_ranges)
    filters.append(f"{''.join(labels)}concat=n={n}:v=1:a=1[outv][outa]")
    filter_complex = ";".join(filters)

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
        "-progress", "pipe:1", "-nostats",
        output_path,
    ]

    # The output is the concatenation of kept_ranges, not the original
    # source — using get_video_duration(video_path) here (the pre-trim
    # source) would understate progress for the whole run, since ffmpeg's
    # out_time_us tracks position in the (shorter) trimmed output and would
    # never reach this larger denominator.
    target_duration = sum(e - s for s, e in kept_ranges)

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err_file:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_file, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            if not (progress_callback and target_duration):
                continue
            line = line.strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                value = line.split("=", 1)[1]
                if value.isdigit():
                    progress_callback(min(1.0, int(value) / 1_000_000 / target_duration))
        returncode = proc.wait()
        if returncode != 0:
            err_file.seek(0)
            tail = err_file.read()[-2000:]
            raise RuntimeError(f"FFmpeg silence trim failed:\n{tail}")


def remap_segments(segments: list[dict], kept_ranges: list[tuple[float, float]]) -> list[dict]:
    """Remap every segment/word timestamp from the original timeline onto the
    trimmed timeline defined by kept_ranges. A timestamp that falls inside a
    removed gap clamps to the nearest kept-range boundary. Segments/words that
    become zero-length after clamping (e.g. a stray timestamp fully inside a
    removed gap) are dropped.
    """
    # Precompute cumulative new-timeline offsets per kept range.
    offsets = []  # (orig_start, orig_end, new_start)
    cursor = 0.0
    for s, e in kept_ranges:
        offsets.append((s, e, cursor))
        cursor += e - s

    def remap_time(t: float) -> float:
        if not offsets:
            return 0.0
        for s, e, new_start in offsets:
            if t < s:
                return new_start
            if s <= t <= e:
                return new_start + (t - s)
        # Past the end of the last kept range.
        last_s, last_e, last_new_start = offsets[-1]
        return last_new_start + (last_e - last_s)

    out = []
    for seg in segments:
        new_start = remap_time(seg["start"])
        new_end = remap_time(seg["end"])
        if new_end <= new_start:
            continue
        new_words = []
        for w in seg.get("words", []):
            w_start = remap_time(w["start"])
            w_end = remap_time(w["end"])
            if w_end <= w_start:
                continue
            new_words.append({"word": w["word"], "start": w_start, "end": w_end})
        out.append({"start": new_start, "end": new_end, "text": seg["text"], "words": new_words})
    return out
