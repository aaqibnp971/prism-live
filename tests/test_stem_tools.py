"""Placeholder generation and the file-based stem acceptance checker (prompt 2.7)."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from tools import check_stems
from tools.make_placeholder_stems import generate, write_float32_wav


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    directory = tmp_path_factory.mktemp("placeholder-stems")
    paths = generate(directory)
    return {path.stem: path for path in paths}


def test_generated_placeholders_pass_the_independent_file_checker(generated):
    reports = [check_stems.check_stem(role, generated[role]) for role in check_stems.ROLES]
    failures = {report.role: report.failures for report in reports if not report.passed}
    assert failures == {}


def test_generated_placeholders_are_gitignored():
    ignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert "assets/placeholders/*.wav" in ignore


def test_checker_rejects_pcm_instead_of_silently_converting_it(tmp_path):
    path = tmp_path / "bed.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(check_stems.RATE)
        output.writeframes(np.zeros(32, dtype="<i2").tobytes())
    with pytest.raises(check_stems.StemFormatError, match="not IEEE float"):
        check_stems.read_float32_wav(path)


def test_checker_reads_a_daw_style_extensible_float_wav(tmp_path):
    samples = np.linspace(-0.25, 0.25, 32, dtype="<f4")
    fmt = struct.pack(
        "<HHIIHHHHI16s",
        check_stems.WAVE_FORMAT_EXTENSIBLE,
        1,
        check_stems.RATE,
        check_stems.RATE * 4,
        4,
        32,
        22,
        32,
        4,
        check_stems.IEEE_FLOAT_SUBFORMAT,
    )

    def chunk(name, payload):
        padding = b"\0" if len(payload) & 1 else b""
        return name + struct.pack("<I", len(payload)) + payload + padding

    body = chunk(b"fmt ", fmt) + chunk(b"data", samples.tobytes())
    path = tmp_path / "air.wav"
    path.write_bytes(struct.pack("<4sI4s", b"RIFF", 4 + len(body), b"WAVE") + body)
    loaded = check_stems.read_float32_wav(path)
    assert loaded.format_name == "extensible IEEE float"
    assert loaded.channels == 1 and loaded.sample_rate == check_stems.RATE
    np.testing.assert_array_equal(loaded.samples, samples)


def test_checker_reports_reserved_energy_peak_and_missing_bed_harmonics(tmp_path, monkeypatch):
    frames = check_stems.RATE
    time = np.arange(frames) / check_stems.RATE

    reserved = 0.5 * np.sin(2.0 * math.pi * 44.0 * time)
    sub_path = tmp_path / "sub.wav"
    write_float32_wav(sub_path, reserved)
    monkeypatch.setitem(check_stems.EXPECTED_FRAMES, "sub", frames)
    sub = check_stems.check_stem("sub", sub_path)
    assert any("reserved-band energy" in failure for failure in sub.failures)

    pure_bed = np.sin(2.0 * math.pi * 200.0 * time)
    bed_path = tmp_path / "bed.wav"
    write_float32_wav(bed_path, pure_bed)
    monkeypatch.setitem(check_stems.EXPECTED_FRAMES, "bed", frames)
    bed = check_stems.check_stem("bed", bed_path)
    assert any("true peak" in failure for failure in bed.failures)
    assert any("does not reach 6 kHz" in failure for failure in bed.failures)


def test_missing_directory_expands_to_four_clear_file_failures(tmp_path):
    inputs = check_stems._inputs([str(tmp_path / "not-made-yet")])
    assert [role for role, _ in inputs] == list(check_stems.ROLES)
    assert all(not path.exists() for _, path in inputs)


def test_an_odd_wrong_frame_count_is_reported_without_crashing_the_meter(tmp_path):
    path = tmp_path / "pulse.wav"
    write_float32_wav(path, np.zeros(1_001, dtype=np.float32))
    report = check_stems.check_stem("pulse", path)
    assert any("expected 528,000 frames" in failure for failure in report.failures)
