"""Synthetic capture listening files preserve provenance, timing and silence.

No test reads a participant recording. The fixture generator uses only invented
intervals, quality patterns and marker timestamps with capture-shaped transport.
"""

import hashlib
import json
import math
import wave

import numpy as np
import pytest

from bridge.beat_scheduler import OK
from tests.synthetic_ppi_capture import MARKERS_MS, write_synthetic_capture
from tools.click_track import FS, _click
from tools.render_ppi_capture import (
    CLICK_GAIN,
    capture_windows,
    generate,
    in_window,
    replay_capture,
)


@pytest.fixture(scope="module")
def capture(tmp_path_factory):
    return write_synthetic_capture(tmp_path_factory.mktemp("synthetic-ppi-capture"))


@pytest.fixture(scope="module")
def replay(capture):
    raw = capture / "raw.jsonl"
    before = hashlib.sha256(raw.read_bytes()).hexdigest()
    result = replay_capture(capture)
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == before
    return result


@pytest.fixture(scope="module")
def rendered(tmp_path_factory, capture):
    directory = tmp_path_factory.mktemp("ppi-listening")
    return generate(capture, directory), directory


def test_capture_immutable_and_all_raw_samples_decode(replay, capture):
    records = [json.loads(line) for line in (capture / "raw.jsonl").read_text().splitlines()]
    raw_count = sum(len(record.get("json", {}).get("ppi", [])) for record in records)
    assert replay.raw_ppi_samples == len(replay.decoded) == raw_count
    assert replay.source_counters["ppi_messages"] == sum(
        record.get("topic", "").endswith("/ecg") for record in records
    )
    assert replay.source_counters["hr"] == sum(
        record.get("topic", "").endswith("/hr") for record in records
    )
    assert replay.source_counters["probation"] > 0
    assert len(replay.delivered) == raw_count - replay.source_counters["probation"]


def test_every_click_is_a_measured_sample_and_published_with_lead(replay):
    assert replay.played
    accepted = {id(receipt.sample) for receipt in replay.accepted}
    assert len({id(beat.receipt.sample) for beat in replay.played}) == len(replay.played)
    for beat in replay.played:
        assert id(beat.receipt.sample) in accepted
        assert beat.event.quality == OK
        assert beat.event.rr_ms == beat.receipt.sample.rr_ms
        assert not beat.receipt.sample.blocked
        assert round(beat.event.t_play) - beat.event.t_emitted >= 300.0
        assert beat.event.t_play > beat.receipt.arrival_ms
    assert all(
        a.event.t_play < b.event.t_play
        for a, b in zip(replay.played, replay.played[1:], strict=False)
    )


def test_strict_windows_are_disjoint_original_receipts_not_coarse_phase_labels(replay, capture):
    records = [json.loads(line) for line in (capture / "raw.jsonl").read_text().splitlines()]
    previous_end = -1
    for window in replay.windows:
        assert window.start_ms > previous_end
        previous_end = window.end_ms
        expected = sum(
            len(record.get("json", {}).get("ppi", []))
            for record in records
            if "perf_counter_ns" in record
            and window.start_ms <= (record["perf_counter_ns"] - replay.origin_ns) / 1e6
            < window.end_ms
        )
        assert expected > 0
        assert sum(in_window(receipt, window) for receipt in replay.decoded) == expected
    assert replay.windows[1].end_ms - replay.windows[1].start_ms == (
        MARKERS_MS["on_requested"] - MARKERS_MS["off"]
    )


def test_off_arm_survivors_are_disclosed_not_suppressed_by_contact(replay, rendered):
    report, _directory = rendered
    window = replay.windows[1]
    selected = [beat for beat in replay.played if in_window(beat.receipt, window)]
    assert selected  # Contrived off-arm labels must not hide surviving accepted intervals.
    off_report = report["files"][1]
    assert off_report["played_clicks"] == len(selected)
    assert any("not acquired while unworn" in text for text in report["limitations"])
    for beat, onset in zip(selected, off_report["onsets"], strict=True):
        assert onset["rr_ms"] == beat.receipt.sample.rr_ms
        assert 0 <= onset["arrival_ms_from_window_start"] < window.end_ms - window.start_ms


def test_wavs_exact_production_clicks_and_quiet_peak_without_normalization(rendered):
    report, _directory = rendered
    click = _click(1200.0, 30.0, CLICK_GAIN)
    for item in report["files"]:
        with wave.open(item["file"], "rb") as wav:
            assert wav.getframerate() == FS
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        expected = np.zeros(len(audio), dtype=np.float32)
        for onset in item["onsets"]:
            start = onset["frame"]
            expected[start : start + len(click)] += click
            assert onset["scheduled_onset_ms_from_window_start"] * FS / 1000 == pytest.approx(
                start, abs=0.5
            )
        assert np.array_equal(audio, np.rint(expected * 32767.0).astype("<i2"))
        assert float(np.max(np.abs(audio.astype(np.int32)))) / 32767 <= 10 ** (-6 / 20)
        assert item["sample_peak_dbfs"] <= -6
        assert item["normalization"] is False
        assert item["fixed_click_gain"] == CLICK_GAIN


def test_leading_wait_original_gaps_and_delayed_tail_not_trimmed(rendered):
    report, _directory = rendered
    for item in report["files"]:
        first = item["onsets"][0]["frame"]
        with wave.open(item["file"], "rb") as wav:
            audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        assert not np.any(audio[:first])
        assert item["first_click_s"] == first / FS
        window_duration = (item["window"]["end_ms"] - item["window"]["start_ms"]) / 1000
        assert item["duration_s"] >= window_duration
        last_onset = item["onsets"][-1]["scheduled_onset_ms_from_window_start"] / 1000
        assert item["duration_s"] >= last_onset + 0.03


def test_report_delay_is_receipt_to_onset_not_inferred_physical_latency(rendered):
    report, _directory = rendered
    for item in report["files"]:
        for onset in item["onsets"]:
            actual_delay = (
                onset["scheduled_onset_ms_from_window_start"]
                - onset["arrival_ms_from_window_start"]
            )
            assert onset["callback_to_onset_ms"] == pytest.approx(actual_delay)
            assert onset["publish_lead_ms"] >= 300
        assert math.isfinite(item["callback_to_onset_ms"]["median"])
    assert any("NOT physical beat latency" in text for text in report["limitations"])


def test_generated_outputs_require_explicit_overwrite(rendered, capture):
    _report, directory = rendered
    with pytest.raises(FileExistsError, match="--overwrite"):
        generate(capture, directory)


def test_missing_measurements_do_not_generate_fake_windows():
    with pytest.raises(ValueError, match="no PPI"):
        capture_windows([], 0)
