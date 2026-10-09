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

def _walk(builder, intervals, *, arrival_lag_ms, settle_ms=10_200, delay=12_000, until=300_000):
    """Feed a session forward as the bridge does: every 2 s, with intervals arriving late."""
    _, baseline, load, regulate = session_evidence()
    resolve_end = regulate + 105_000 + 45_000
    seen = []
    for now in range(0, until + 1, 2_000):
        arrived = [item for item in intervals if item[0] + arrival_lag_ms(item[0]) <= now]
        common = dict(
            now_ms=now,
            settle_ms=settle_ms,
            playback_delay_ms=delay,
            baseline_start_ms=baseline,
            load_start_ms=load if now >= load else None,
            regulate_start_ms=regulate if now >= regulate else None,
            resolve_end_ms=resolve_end if now >= regulate + 105_000 else None,
            heart_rate_activation_passed=True if now >= regulate + settle_ms else None,
        )
        seen.append((now, builder.build(arrived, **common), build_trace(arrived, **common)))
    return seen


def test_forward_builder_matches_the_full_recalculation_when_bursts_arrive_on_time():
    from bridge.trace import TraceBuilder

    intervals = session_evidence()[0]
    seen = _walk(TraceBuilder(), intervals, arrival_lag_ms=lambda t: 7_000)
    assert all(forward == recalculated for _, forward, recalculated in seen)
    final = seen[-1][1]
    assert (final["at_rest_bpm"], final["highest_task_bpm"], final["after_task_bpm"]) == (
        60.0,
        75.0,
        60.0,
    )
    assert final["spoken_n_bpm"] == 15.0 and len(final["average_30s"]) > 400


def test_published_cards_and_points_never_change_when_a_burst_arrives_after_settlement():
    from bridge.trace import TraceBuilder

    intervals, _, _, regulate = session_evidence()

    def lag(t_beat):
        # The phone holds regulate seconds 68 to 75 for 19 s and then delivers them in one burst.
        return 19_000 if regulate + 68_000 < t_beat <= regulate + 75_000 else 7_000

    seen = _walk(TraceBuilder(), intervals, arrival_lag_ms=lag)
    published: dict[int, float | None] = {}
    cards: dict[str, float | None] = {}
    decided_at: dict[str, int] = {}
    revised_by_recalculation = False
    for now, forward, recalculated in seen:
        for point in forward["average_30s"]:
            assert published.setdefault(point["t_play"], point["hr_bpm"]) == point["hr_bpm"]
        for key in ("at_rest_bpm", "highest_task_bpm", "after_task_bpm"):
            if forward[key] is not None:
                assert cards.setdefault(key, forward[key]) == forward[key]
                decided_at.setdefault(key, now)
        revised_by_recalculation |= forward != recalculated
    # The scenario is real: recalculating would have revised history, and the cards exist.
    assert revised_by_recalculation
    assert set(cards) == {"at_rest_bpm", "highest_task_bpm", "after_task_bpm"}
    assert decided_at["after_task_bpm"] <= regulate + 75_000 + 10_200 + 2_000
    times = [point["t_play"] for point in seen[-1][1]["average_30s"]]
    assert times == sorted(set(times))


def test_a_new_builder_starts_empty_whatever_the_previous_session_published():
    from bridge.trace import TraceBuilder

    intervals = session_evidence()[0]
    first = TraceBuilder()
    _walk(first, intervals, arrival_lag_ms=lambda t: 7_000)
    fresh = TraceBuilder().build(
        [],
        now_ms=4_000,
        settle_ms=10_200,
        playback_delay_ms=12_000,
        baseline_start_ms=0,
        load_start_ms=None,
        regulate_start_ms=None,
        resolve_end_ms=None,
        heart_rate_activation_passed=None,
    )
    assert fresh == {
        "at_rest_bpm": None,
        "highest_task_bpm": None,
        "after_task_bpm": None,
        "spoken_n_bpm": None,
        "average_30s": [],
    }


def _cards(intervals, *, activation=True, load=56_000, regulate=131_000):
    return build_trace(
        intervals,
        now_ms=regulate + 200_000,
        settle_ms=10_200,
        playback_delay_ms=12_000,
        baseline_start_ms=0,
        load_start_ms=load,
        regulate_start_ms=regulate,
        resolve_end_ms=None,
        heart_rate_activation_passed=activation,
    )


def test_card_windows_sit_exactly_where_the_contract_puts_them():
    regulate = 131_000
    # At rest is baseline seconds 15 to 45: what happens in seconds 0 to 15 and 45 to 56 is ignored.
    rest = [*beats(0, 15_000, 120), *beats(15_000, 45_000, 60), *beats(45_000, 56_000, 120)]
    assert _cards(rest)["at_rest_bpm"] == 60.0
    # Highest during the task never reaches back before load or forward into regulate.
    task = [
        *beats(26_000, 56_000, 150),
        *beats(56_000, regulate, 80),
        *beats(regulate, regulate + 30_000, 150),
    ]
    assert _cards(task)["highest_task_bpm"] == 80.0
    # Its candidate windows step by 500 ms: a peak placed on the half second is found whole.
    peak = [
        *beats(56_000, 70_500, 70),
        *beats(70_500, 100_500, 100),
        *beats(100_500, regulate, 70),
    ]
    assert _cards(peak)["highest_task_bpm"] == 100.0
    # After the task is regulate seconds 45 to 75 and nothing else.
    after = [
        *beats(regulate, regulate + 45_000, 110),
        *beats(regulate + 45_000, regulate + 75_000, 66),
        *beats(regulate + 75_000, regulate + 105_000, 50),
    ]
    assert _cards(after)["after_task_bpm"] == 66.0


@pytest.mark.parametrize(
    "high, after, activation, expected",
    [
        (80.0, 77.0, True, 3.0),
        (80.0, 77.1, True, None),  # a fall of 2.9 is not spoken
        (80.1, 77.1, True, 3.0),  # the displayed one-decimal values are subtracted
        (90.0, 70.0, False, None),
        (90.0, 70.0, None, None),
    ],
)
def test_spoken_n_needs_activation_and_a_displayed_fall_of_three(high, after, activation, expected):
    regulate = 131_000
    intervals = [
        *beats(0, 45_000, 60),
        *beats(56_000, regulate, high),
        *beats(regulate + 45_000, regulate + 75_000, after),
    ]
    trace = _cards(intervals, activation=activation)
    assert (trace["highest_task_bpm"], trace["after_task_bpm"]) == (high, after)
    assert trace["spoken_n_bpm"] == expected
