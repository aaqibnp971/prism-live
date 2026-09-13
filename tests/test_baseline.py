"""bridge/baseline.py: hr_base and rmssd_base from the last 30 s, the slope, and the gate."""

import math

import pytest
from conftest import true_hr, true_rmssd

from bridge.baseline import FLAT_SLOPE, BaselineCapture


def capture(run, start_ms=0.0):
    baseline = BaselineCapture(start_ms)
    baseline.add(run.classified)
    return baseline.result()


def test_a_settled_person_passes_with_a_flat_trustworthy_baseline(pipeline):
    run = pipeline("68:60")
    b = capture(run)
    beats = run.true_beats()
    assert b.passed and b.problems == ()
    assert b.hr_base_bpm == pytest.approx(true_hr(beats, 15, 45), abs=1.0)
    assert b.rmssd_base_ms == pytest.approx(true_rmssd(beats, 15, 45), rel=0.12)
    assert b.ln_rmssd_base is not None
    assert abs(b.slope_bpm_per_min) < FLAT_SLOPE and b.baseline_quality > 0.8
    assert b.accepted_ms > 38_000 and b.accepted_intervals > 40


def test_a_rate_still_falling_steeply_uses_the_tail_and_scores_zero(pipeline):
    """Straight off a loud floor: 95 bpm falling to 70 across the whole 45 s."""
    run = pipeline("95-70:45,70:15", seed=2)
    b = capture(run)
    beats = run.true_beats()
    assert b.slope_bpm_per_min < -25  # the true slope is -33 bpm a minute
    assert b.baseline_quality == 0.0
    # Both come from the last 30 s, and both differ from what the whole 45 s would say.
    assert b.hr_base_bpm == pytest.approx(true_hr(beats, 15, 45), abs=1.0)
    assert b.hr_base_bpm < true_hr(beats, 0, 45) - 3
    assert true_rmssd(beats, 15, 45) > 1.1 * true_rmssd(beats, 0, 45)
    assert b.rmssd_base_ms == pytest.approx(true_rmssd(beats, 15, 45), rel=0.05)
    assert b.ln_rmssd_base == pytest.approx(math.log(b.rmssd_base_ms))
    # A falling rate is the person, not the signal: the gate still passes.
    assert b.passed


def test_a_rate_rising_steeply_scores_zero_too(pipeline):
    b = capture(pipeline("70-95:45,95:15"))
    assert b.slope_bpm_per_min > 25 and b.baseline_quality == 0.0


def test_quality_falls_between_flat_and_steep(pipeline):
    gentle = capture(pipeline("80-75:45,75:15"))  # about -7 bpm a minute
    assert 0.0 < gentle.baseline_quality < 1.0


def test_an_artefact_burst_does_not_move_the_baseline(pipeline):
    clean, burst = capture(pipeline("68:60")), capture(pipeline("68:60", "artefact_burst@25"))
    assert burst.passed
    assert burst.rmssd_base_ms == pytest.approx(clean.rmssd_base_ms, rel=0.15)
    assert burst.hr_base_bpm == pytest.approx(clean.hr_base_bpm, abs=1.0)
    assert burst.accepted_ms < clean.accepted_ms


def test_one_misplaced_beat_in_the_tail_does_not_inflate_rmssd_base(pipeline):
    """No rejection anywhere. The earlier pair test let this through at 57.9 ms, 62.5 % high."""
    clean = capture(pipeline("60:60", seed=4))
    late = capture(pipeline("60:60", "artefact_burst@35:0.5", seed=4))
    assert late.passed and late.rmssd_base_ms is not None
    assert late.rmssd_base_ms < 1.1 * clean.rmssd_base_ms


@pytest.mark.parametrize(
    ("spec", "faults", "problem"),
    [
        ("68:60", ("disconnect@20:5",), None),  # 37.1 s of clean data
        ("68:60", ("disconnect@20:9",), "clean data"),  # 32.7 s
        ("48:60", (), None),  # 32 accepted intervals
        ("42:60", (), "accepted intervals"),  # 28, with 40 s of clean data
    ],
)
def test_each_gate_threshold_on_its_own(pipeline, spec, faults, problem):
    b = capture(pipeline(spec, *faults))
    if problem is None:
        assert b.passed and b.problems == ()
    else:
        assert not b.passed and len(b.problems) == 1 and problem in b.problems[0]


def test_losing_twenty_five_seconds_fails_both_gates(pipeline):
    b = capture(pipeline("68:60", "disconnect@5:25"))
    assert not b.passed
    assert len(b.problems) == 2
    assert "clean data" in b.problems[0] and "accepted intervals" in b.problems[1]


def test_a_thin_clean_tail_still_gives_hr_base_and_a_fair_slope(pipeline):
    """Three missed beats leave too few HRV-clean differences, but the armband was fine."""
    run = pipeline("68:70", "missed_beat@22", "missed_beat@32", "missed_beat@42", seed=2)
    b = capture(run)
    assert b.passed
    assert b.rmssd_base_ms is None  # honest: not enough clean differences
    assert b.hr_base_bpm == pytest.approx(true_hr(run.true_beats(), 15, 45), abs=1.0)
    assert b.baseline_quality > 0.9  # the heart was flat, and the slope says so


def test_nothing_at_all_fails_cleanly():
    b = BaselineCapture(0.0).result()
    assert not b.passed and len(b.problems) == 2
    assert b.hr_base_bpm is None and b.rmssd_base_ms is None and b.slope_bpm_per_min is None
    assert b.baseline_quality == 0.0


def test_only_beats_inside_the_window_count(pipeline):
    run = pipeline("68:120")
    later = capture(run, start_ms=60_000.0)
    assert later.start_ms == 60_000 and later.end_ms == 105_000
    assert later.passed and later.accepted_intervals < 55


def test_ready_waits_for_the_first_interval_classified_past_the_end(pipeline):
    run = pipeline("68:60")
    inside = [i for i in run.classified if i.t_beat <= 45_000]
    baseline = BaselineCapture(0.0)
    baseline.add(inside)
    assert not baseline.ready(46_000)
    baseline.add([i for i in run.classified if i.t_beat > 45_000][:1])
    assert baseline.ready(46_000)


def test_ready_gives_up_waiting_when_the_armband_goes_quiet():
    baseline = BaselineCapture(10_000.0)
    assert not baseline.ready(74_999)
    assert baseline.ready(75_000)


def test_the_heart_rate_spread_ignores_an_artefact_burst(pipeline):
    clean, burst = capture(pipeline("68:60")), capture(pipeline("68:60", "artefact_burst@35"))
    assert clean.hr_sd_bpm is not None
    assert burst.hr_sd_bpm == pytest.approx(clean.hr_sd_bpm, rel=0.25)


def test_rmssd_base_says_how_many_differences_it_rests_on(pipeline):
    from bridge.hrv import summarise

    run = pipeline("68:60")
    baseline = capture(run)
    tail = summarise(run.classified, baseline.end_ms - 30_000, baseline.end_ms)
    assert baseline.rmssd_base_differences == tail.differences >= 25


def test_progress_and_provisional_quality_grow_into_the_result(pipeline):
    run = pipeline("68:60")
    live = BaselineCapture(0)
    assert live.progress() == 0.0 and live.provisional_quality() is None
    halfway = [c for c in run.classified if c.t_beat <= 20_000]
    live.add(halfway)
    assert 0.3 < live.progress() < 0.7
    live.add(run.classified[len(halfway) :])
    result = live.result()
    assert live.progress() == 1.0 and result.passed
    assert live.provisional_quality() == result.baseline_quality


@pytest.mark.parametrize("seed", [2, 3, 4])
def test_the_heart_rate_spread_matches_the_true_beats(pipeline, monkeypatch, seed):
    """A spread large enough to leave psv.py's 4 bpm floor, checked against the same robust
    estimate taken over the true beats of the same 30 s."""
    import tools.synthetic_rr as synthetic
    from bridge.baseline import _robust_sd

    monkeypatch.setattr(synthetic, "RR_SD_MS_AT_60_BPM", 90.0)
    run = pipeline("68:60", seed=seed)
    truth = [60_000 / b.rr_ms for b in run.true_beats() if 15 < b.t_s <= 45]
    assert capture(run).hr_sd_bpm == pytest.approx(_robust_sd(truth), rel=0.1)
    assert _robust_sd(truth) > 4.5
