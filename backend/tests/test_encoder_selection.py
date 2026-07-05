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
