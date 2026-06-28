from ..styles.definitions import STYLES

_SCRIPT_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 1
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{style_line}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
{events}"""


def _format_ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds % 1) * 100))
    if cs >= 100:
        cs = 99
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _build_karaoke_text(words: list) -> str:
    parts = []
    for word in words:
        duration_cs = max(1, int(round((word["end"] - word["start"]) * 100)))
        text = word["word"].strip()
        parts.append(f"{{\\k{duration_cs}}}{text} ")
    return "".join(parts).rstrip()


# ── Compound style helpers ────────────────────────────────────────────────────

def _flatten_words(segments: list) -> list:
    """Collect every word with timing from Whisper word-timestamp output."""
    all_words = []
    for seg in segments:
        for w in seg.get("words", []):
            word = w["word"].strip()
            if word:
                all_words.append({"word": word, "start": w["start"], "end": w["end"]})
    return all_words


def _build_accent_open_tags(accent: dict) -> str:
    """Build the opening ASS override tag block for the accent section.

    Inline color uses &H{BB}{GG}{RR} (no alpha byte; alpha set via \1a for hollow).
    """
    parts = []
    if "color" in accent:
        parts.append(f"\\c{accent['color']}&")
    if "scale_x" in accent:
        parts.append(f"\\fscx{accent['scale_x']}")
    if "scale_y" in accent:
        parts.append(f"\\fscy{accent['scale_y']}")
    if accent.get("hollow"):
        parts.append("\\1a&HFF&")  # make primary fill fully transparent
        if "outline_color" in accent:
            parts.append(f"\\3c{accent['outline_color']}&")
        if "border_width" in accent:
            parts.append(f"\\bord{accent['border_width']}")
    return ("{" + "".join(parts) + "}") if parts else ""


def _build_compound_events(
    segments: list, style: dict, style_name: str, pos_prefix: str = ""
) -> list:
    """Render a compound style: group words into fixed-size chunks and apply
    inline override tags to the trailing 'accent' words of each chunk."""
    words = _flatten_words(segments)
    if not words:
        return []

    wpg = style.get("words_per_group", 4)
    split_after = style.get("split_after", wpg // 2)
    accent = style.get("accent", {})
    two_line = style.get("two_line", True)

    open_tags = _build_accent_open_tags(accent)
    close_tags = "{\\r}" if open_tags else ""
    # \\N in Python source → \N in the string → ASS hard line break in the file
    separator = "\\N" if two_line else " "

    events = []
    for i in range(0, len(words), wpg):
        chunk = words[i : i + wpg]
        if not chunk:
            continue

        start = _format_ass_time(chunk[0]["start"])
        end = _format_ass_time(chunk[-1]["end"])

        base_part = " ".join(w["word"] for w in chunk[:split_after])
        accent_part = " ".join(w["word"] for w in chunk[split_after:])

        if base_part and accent_part:
            text = f"{base_part}{separator}{open_tags}{accent_part}{close_tags}"
        elif accent_part:
            text = f"{open_tags}{accent_part}{close_tags}"
        else:
            text = base_part

        if text:
            events.append(
                f"Dialogue: 0,{start},{end},{style_name},,0,0,0,,{pos_prefix}{text}"
            )

    return events


# ── Position & size overrides ─────────────────────────────────────────────────

def _scale_style_line(style_line: str, scale: float) -> str:
    """Multiply the Fontsize field of a `Style:` line by `scale`.

    Scaling the base font size (rather than an inline \\fs override) means compound
    styles' `{\\r}` accent resets still inherit the new size. Field index 2 of the
    comma-separated style line is Fontsize ("Style: Name", "Fontname", "Fontsize", …).
    """
    if scale == 1.0:
        return style_line
    parts = style_line.split(",")
    if len(parts) > 2:
        try:
            parts[2] = str(max(1, round(float(parts[2]) * scale)))
        except ValueError:
            pass
    return ",".join(parts)


def _build_pos_prefix(position, width: int, height: int) -> str:
    """Inline override placing the caption block center at the chosen anchor.

    `\\an5` anchors the block by its middle-center, matching the draggable preview
    overlay. Returns "" when no position is supplied (back-compat with style defaults).
    """
    if not position:
        return ""
    x = round(position[0] * width)
    y = round(position[1] * height)
    return f"{{\\an5\\pos({x},{y})}}"


# ── Main entry point ──────────────────────────────────────────────────────────

def build(
    segments: list,
    style_id: str,
    width: int = 1920,
    height: int = 1080,
    position=None,
    scale: float = 1.0,
) -> str:
    style = STYLES[style_id]
    style_line = _scale_style_line(style["ass_style"], scale)
    style_name = style["name"]
    pos_prefix = _build_pos_prefix(position, width, height)

    if style.get("compound"):
        event_lines = _build_compound_events(segments, style, style_name, pos_prefix)
    else:
        event_lines = []
        for seg in segments:
            start = _format_ass_time(seg["start"])
            end = _format_ass_time(seg["end"])

            if style_id == "karaoke" and seg.get("words"):
                text = _build_karaoke_text(seg["words"])
            elif style_id == "cinematic":
                text = "{\\fad(300,300)}" + seg["text"].strip()
            else:
                text = seg["text"].strip()

            event_lines.append(
                f"Dialogue: 0,{start},{end},{style_name},,0,0,0,,{pos_prefix}{text}"
            )

    return _SCRIPT_HEADER.format(
        width=width,
        height=height,
        style_line=style_line,
        events="\n".join(event_lines),
    )
