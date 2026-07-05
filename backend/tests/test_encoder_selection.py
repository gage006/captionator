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
