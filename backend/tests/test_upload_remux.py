"""Unit tests for the upload-time faststart remux — no Docker stack required.

remux_faststart lives in its own stdlib-only module (app/remux.py) precisely
so these tests can stub subprocess.run and run on a bare interpreter,
mirroring test_encoder_selection.py / tasks/encoder.py.
"""
import subprocess

import pytest

from app import remux as remux_module
from app.remux import remux_faststart


def test_success_replaces_original_with_source_mp4(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"original bytes")

    def fake_run(cmd, **kwargs):
        # Simulate ffmpeg producing the remuxed output at the -movflags target.
        out_path = cmd[cmd.index("-movflags") + 2]
        with open(out_path, "wb") as f:
            f.write(b"remuxed bytes")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(remux_module.subprocess, "run", fake_run)

    result = remux_faststart(src, tmp_path)

    assert result == tmp_path / "source.mp4"
    assert result.read_bytes() == b"remuxed bytes"
    assert not src.exists()  # original replaced, not left behind


def test_ffmpeg_failure_keeps_original_untouched(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"original bytes")

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="unsupported codec")

    monkeypatch.setattr(remux_module.subprocess, "run", fake_run)

    result = remux_faststart(src, tmp_path)

    assert result is None
    assert src.read_bytes() == b"original bytes"
    assert not (tmp_path / "source.mp4").exists()


def test_ffmpeg_timeout_keeps_original_untouched(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"original bytes")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 300)

    monkeypatch.setattr(remux_module.subprocess, "run", fake_run)

    result = remux_faststart(src, tmp_path)

    assert result is None
    assert src.exists()


def test_partial_output_from_failed_run_is_cleaned_up(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"original bytes")
    stray = tmp_path / "source.mp4"

    def fake_run(cmd, **kwargs):
        # ffmpeg wrote a partial file before erroring out.
        stray.write_bytes(b"partial")
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(remux_module.subprocess, "run", fake_run)

    remux_faststart(src, tmp_path)

    assert not stray.exists()
