"""Unit tests for browser-safe preview handling — no Docker stack required.

transcode.py is deliberately stdlib-only at import time (same pattern as
encoder.py), so these run on any host with a stubbed subprocess.
"""
import subprocess

from app.tasks import transcode
from app.tasks.transcode import (
    build_preview_cmd,
    create_preview,
    is_browser_safe,
    remux_faststart,
)


# ---------------------------------------------------------------------------
# Safe-list decisions (pure)
# ---------------------------------------------------------------------------

def test_h264_yuv420p_is_safe():
    assert is_browser_safe("h264", "yuv420p")
    assert is_browser_safe("h264", "yuvj420p")


def test_hevc_is_not_safe():
    assert not is_browser_safe("hevc", "yuv420p")
    assert not is_browser_safe("hevc", "yuv420p10le")


def test_10bit_h264_is_not_safe():
    assert not is_browser_safe("h264", "yuv420p10le")


def test_other_codecs_are_not_safe():
    assert not is_browser_safe("vp9", "yuv420p")
    assert not is_browser_safe("av1", "yuv420p")
    assert not is_browser_safe("mpeg4", "yuv420p")


def test_unknown_probe_result_is_not_safe():
    assert not is_browser_safe(None, None)


# ---------------------------------------------------------------------------
# Preview command construction (pure)
# ---------------------------------------------------------------------------

def test_preview_cmd_encodes_h264_8bit_faststart():
    cmd = build_preview_cmd("in.mov", "preview.mp4")
    assert cmd[:5] == ["ffmpeg", "-y", "-i", "in.mov", "-c:v"]
    assert "libx264" in cmd
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"
    assert cmd[-1] == "preview.mp4"


def test_preview_cmd_normalizes_audio_to_aac():
    cmd = build_preview_cmd("in.mov", "out.mp4")
    assert cmd[cmd.index("-c:a") + 1] == "aac"


# ---------------------------------------------------------------------------
# create_preview / remux_faststart failure handling
# ---------------------------------------------------------------------------

def test_create_preview_failure_removes_partial_output(tmp_path, monkeypatch):
    out = tmp_path / "preview.mp4"

    class FakeProc:
        stdout = iter(())

        def wait(self):
            out.write_bytes(b"partial")  # ffmpeg wrote garbage before dying
            return 1

    monkeypatch.setattr(transcode.subprocess, "Popen", lambda *a, **k: FakeProc())

    assert create_preview("in.mp4", str(out)) is False
    assert not out.exists()


def test_create_preview_success_reports_progress(tmp_path, monkeypatch):
    out = tmp_path / "preview.mp4"
    fractions = []

    class FakeProc:
        stdout = iter(["out_time_us=2000000\n", "out_time_us=4000000\n"])

        def wait(self):
            return 0

    monkeypatch.setattr(transcode.subprocess, "Popen", lambda *a, **k: FakeProc())

    ok = create_preview(
        "in.mp4", str(out), progress_callback=fractions.append, duration=4.0
    )
    assert ok is True
    assert fractions == [0.5, 1.0]


def test_remux_replaces_file_in_place(tmp_path, monkeypatch):
    src = tmp_path / "video.mp4"
    src.write_bytes(b"original")

    def fake_run(cmd, **kwargs):
        with open(cmd[-1], "wb") as f:  # tmp output path is the last arg
            f.write(b"remuxed")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(transcode.subprocess, "run", fake_run)

    assert remux_faststart(str(src)) is True
    assert src.read_bytes() == b"remuxed"
    assert list(tmp_path.iterdir()) == [src]  # no temp file left behind


def test_remux_failure_keeps_original_and_cleans_temp(tmp_path, monkeypatch):
    src = tmp_path / "video.mp4"
    src.write_bytes(b"original")

    def fake_run(cmd, **kwargs):
        with open(cmd[-1], "wb") as f:
            f.write(b"partial")
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(transcode.subprocess, "run", fake_run)

    assert remux_faststart(str(src)) is False
    assert src.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [src]
