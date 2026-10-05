"""The host-owned 30-second averages and spoken-N gate (contract v1.8)."""

from __future__ import annotations

import pytest

from bridge.trace import (
    MIN_COVERED_MS,
    build_trace,
    heart_rate_window,
)


def beats(start_ms: float, end_ms: float, bpm: float) -> list[tuple[float, float]]:
    rr = 60_000.0 / bpm
    at = start_ms + rr
    result = []
    while at <= end_ms + 1e-9:
        result.append((at, rr))
        at += rr
    return result


def session_evidence() -> tuple[list[tuple[float, float]], int, int, int]:
    baseline = 0
    load = 56_000
    regulate = 131_000
    intervals = [
        *beats(baseline, 45_000, 60),
        *beats(load, regulate, 75),
        *beats(regulate, regulate + 45_000, 80),
        *beats(regulate + 45_000, regulate + 75_000, 60),
        # An adaptive extension is deliberately very fast. It must not move After the task.
        *beats(regulate + 75_000, regulate + 105_000, 120),
    ]
    return intervals, baseline, load, regulate


def test_window_uses_interval_count_over_sum_and_a_separate_coverage_measure():
    intervals = [(1_000, 1_000), (2_000, 1_000), (3_500, 1_500)]
    window = heart_rate_window(intervals, 500, 3_500)
    assert window.bpm == pytest.approx(60_000 * 3 / 3_500)
    assert window.covered_ms == 3_000
    assert window.intervals == 3


def test_fixed_cards_wait_for_settlement_and_ignore_a_regulate_extension():
    intervals, baseline, load, regulate = session_evidence()
    common = dict(
        intervals=intervals,
        settle_ms=10_200,
        playback_delay_ms=12_000,
        baseline_start_ms=baseline,
        load_start_ms=load,
        regulate_start_ms=regulate,
        resolve_end_ms=regulate + 105_000 + 45_000,
        heart_rate_activation_passed=True,
    )
    pending = build_trace(now_ms=regulate + 75_000 + 10_199, **common)
    assert pending["at_rest_bpm"] == 60.0
    assert pending["highest_task_bpm"] == 75.0
    assert pending["after_task_bpm"] is None
    assert pending["spoken_n_bpm"] is None

    ready = build_trace(now_ms=regulate + 75_000 + 10_200, **common)
    assert ready["at_rest_bpm"] == 60.0
    assert ready["highest_task_bpm"] == 75.0
    assert ready["after_task_bpm"] == 60.0
    assert ready["spoken_n_bpm"] == 15.0


def test_spoken_n_requires_sustained_hr_activation_and_three_complete_cards():
    intervals, baseline, load, regulate = session_evidence()
    common = dict(
        intervals=intervals,
        now_ms=regulate + 100_000,
        settle_ms=10_200,
        playback_delay_ms=12_000,
        baseline_start_ms=baseline,
        load_start_ms=load,
        regulate_start_ms=regulate,
        resolve_end_ms=None,
    )
    assert build_trace(heart_rate_activation_passed=False, **common)["spoken_n_bpm"] is None
    assert build_trace(heart_rate_activation_passed=None, **common)["spoken_n_bpm"] is None

    thin_baseline = [item for item in intervals if not (15_000 < item[0] <= 37_500)]
    thin = build_trace(
        intervals=thin_baseline,
        now_ms=regulate + 100_000,
        settle_ms=10_200,
        playback_delay_ms=12_000,
        baseline_start_ms=baseline,
        load_start_ms=load,
        regulate_start_ms=regulate,
        resolve_end_ms=None,
        heart_rate_activation_passed=True,
    )
    assert thin["at_rest_bpm"] is None
    assert thin["spoken_n_bpm"] is None


def test_average_line_is_host_projected_and_null_where_coverage_is_thin():
    intervals = beats(0, 60_000, 60)
    # Remove enough evidence from the second candidate window to fail its 22.5 s rail.
    intervals = [item for item in intervals if not (8_000 < item[0] <= 31_000)]
    trace = build_trace(
        intervals,
        now_ms=45_000,
        settle_ms=2_000,
        playback_delay_ms=12_000,
        baseline_start_ms=0,
        load_start_ms=None,
        regulate_start_ms=None,
        resolve_end_ms=None,
        heart_rate_activation_passed=None,
    )
    assert trace["average_30s"][0]["t_play"] == 42_000
    assert trace["average_30s"][0]["hr_bpm"] is None
    assert all(point["t_play"] <= 45_000 for point in trace["average_30s"])
    assert MIN_COVERED_MS == 22_500
