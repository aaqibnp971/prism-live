"""bridge/hrv.py: which intervals are clean, and what RMSSD and heart rate come out of them."""

import math

import pytest
from conftest import false_data, run_pipeline, true_hr, true_rmssd

from bridge.beat_scheduler import Interval
from bridge.hrv import IntervalCleaner, RollingHrv

FAULTS = (
    "dropped_packet@30",
    "doubled_beat@40",
    "missed_beat@40",
    "artefact_burst@40",
    "disconnect@40:8",
)


def reading(run, now_ms):
    hrv = RollingHrv()
    hrv.add(run.classified)
    return hrv.reading(now_ms)


# --- the numbers ---


def test_a_clean_run_gives_the_hearts_own_rmssd_and_rate(pipeline):
    run = pipeline("68:120")
    r = reading(run, 100_000)
    beats = run.true_beats()
    assert r.rmssd_ms == pytest.approx(true_rmssd(beats, 40, 100), rel=0.08)
    assert r.mean_hr_bpm == pytest.approx(true_hr(beats, 40, 100), abs=1.0)
    assert r.ln_rmssd == pytest.approx(math.log(r.rmssd_ms))
    assert r.differences >= 55 and r.covered_ms > 55_000


def test_variability_falls_as_the_rate_rises(pipeline):
    run = pipeline("68:60,105:60")
    assert reading(run, 60_000).rmssd_ms > 2 * reading(run, 120_000).rmssd_ms


def test_rmssd_waits_for_ten_differences(pipeline):
    run = pipeline("68:60")
    early = reading(run, 8_000)
    assert early.differences < 10
    assert early.rmssd_ms is None and early.ln_rmssd is None
    assert early.mean_hr_bpm is not None
    assert reading(run, 20_000).rmssd_ms is not None


def test_the_window_rolls(pipeline):
    run = pipeline("68:60,90:60")
    late = reading(run, 120_000)
    assert late.start_ms == 60_000
    assert late.mean_hr_bpm == pytest.approx(90, abs=2)


# --- the four known limits (docs/known-limits.md) ---


@pytest.mark.parametrize(
    ("spec", "faults", "seed"),
    [
        ("68:90", ("doubled_beat@40",), 1),
        ("68:90", ("missed_beat@40",), 1),
        ("68:90", ("artefact_burst@40",), 1),
        # Each of these let false data through a guard of 4 beats or less.
        ("52:90", ("artefact_burst@40",), 227),
        ("55:90", ("artefact_burst@40",), 288),
        ("60:90", ("artefact_burst@40",), 267),
        ("68:90", ("artefact_burst@40",), 283),
        # This one let false data through without the backward guard.
        ("60:90", ("artefact_burst@40",), 16),
        # These let false data through when a gap was not guarded.
        ("100:80", ("artefact_burst@40", "dropped_packet@41"), 1),
        ("55:80", ("artefact_burst@40", "dropped_packet@41.5"), 202),
        ("68:90", ("artefact_burst@40", "disconnect@42:3"), 1),
        ("50:80", ("artefact_burst@40:1",), 1),
        # One beat detected late and no rejection anywhere: rmssd_base read 62 % high with the
        # earlier pair test, which wanted the pair to sum to twice the local median.
        ("60:60", ("artefact_burst@35:0.5",), 4),
    ],
)
def test_no_false_data_reaches_hrv(spec, faults, seed):
    assert false_data(run_pipeline(spec, *faults, seed=seed)) == (0, 0)


def test_the_artefacts_that_pass_the_scheduler_do_not_reach_rmssd(pipeline):
    """Limit 1. The burst's accepted false intervals would inflate a naive RMSSD."""
    run = pipeline("68:90", "artefact_burst@40")
    assert any(i.accepted and not ok for i, ok in zip(run.intervals, run.real, strict=True))
    accepted = [i.rr_ms for i in run.intervals if i.accepted and 30_000 < i.t_beat <= 90_000]
    squares = sum((b - a) ** 2 for a, b in zip(accepted, accepted[1:], strict=False))
    naive = math.sqrt(squares / (len(accepted) - 1))
    truth = true_rmssd(run.true_beats(), 30, 90)
    assert naive > 1.3 * truth
    assert reading(run, 90_000).rmssd_ms == pytest.approx(truth, rel=0.12)


@pytest.mark.parametrize("fault", FAULTS)
def test_a_difference_only_ever_spans_two_clean_consecutive_beats(pipeline, fault):
    """Limits 2 and 3: never across a rejection, never across a lost packet."""
    run = pipeline("68:90", fault)
    for k, current in enumerate(run.classified):
        if current.diff_ms is None:
            continue
        before, interval = run.classified[k - 1], run.intervals[k]
        assert k > 0 and current.clean and before.clean and interval.contiguous
        assert current.diff_ms == pytest.approx(current.rr_ms - before.rr_ms)
        assert abs(current.diff_ms) <= 0.2 * before.rr_ms


def test_a_lost_packet_is_guarded_on_both_sides(pipeline):
    """Limit 3."""
    run = pipeline("68:90", "disconnect@40:8")
    after_gap = [k for k, i in enumerate(run.intervals) if not i.contiguous]
    assert len(after_gap) == 2  # the first interval, and the first after the disconnect
    gap = after_gap[1]
    assert not any(c.clean for c in run.classified[gap - 6 : gap + 7])
    full = reading(pipeline("68:90"), 90_000).covered_ms
    assert reading(run, 90_000).covered_ms < full - 15_000


def test_bootstrap_intervals_are_never_clean(pipeline):
    """Limit 4: at the start and after a window reset."""
    run = pipeline("60:20,90:20")
    assert run.scheduler.stats.window_resets >= 1
    bootstrap = [k for k, i in enumerate(run.intervals[: len(run.classified)]) if i.bootstrap]
    assert bootstrap[:3] == [0, 1, 2] and len(bootstrap) >= 6
    assert not any(run.classified[k].clean for k in bootstrap)


@pytest.mark.parametrize("bpm", [50, 68, 100, 130])
def test_the_misplaced_beat_test_leaves_a_clean_run_alone(pipeline, bpm):
    run = pipeline(f"{bpm}:90")
    assert sum(1 for c in run.classified if not c.clean and not c.bootstrap) == 0


def test_readings_end_where_classification_has_reached():
    from bridge.beat_scheduler import BeatScheduler
    from tools.synthetic_rr import Profile, generate

    sched, cleaner, hrv = BeatScheduler(), IntervalCleaner(), RollingHrv()
    for note in generate(Profile.from_spec("68:120"), [], 1):
        now = note.t_s * 1000 + 40
        hrv.add(cleaner.add(sched.on_packet(now, note.payload).intervals))
        if now > 90_000:
            break
    live = hrv.reading(now, cleaner.horizon_ms)
    assert live.end_ms == cleaner.horizon_ms and 2_000 < live.lag_ms < 9_000
    assert live.covered_ms > 58_000
    assert hrv.reading(now).covered_ms < live.covered_ms  # ending at now leaves the newest empty


# --- the rules, one by one ---


def interval(rr, t, accepted=True, contiguous=True, bootstrap=False):
    return Interval(t, rr, accepted, t, contiguous=contiguous, bootstrap=bootstrap)


def classify_all(cleaner, run):
    """Classify every interval in run, by following it with plenty of steady clean beats."""
    last = run[-1]
    tail = [interval(last.rr_ms, last.t_beat + 1000 * k) for k in range(1, 12)]
    return cleaner.add([*run, *tail])[: len(run)]


def test_classification_waits_exactly_for_the_guard():
    slow = [interval(1000, 1000 * k) for k in range(1, 9)]
    cleaner = IntervalCleaner()  # 3 s or 6 beats
    assert cleaner.add(slow[:6]) == []  # 5 s but only 5 beats ahead of the first
    assert [i.t_beat for i in cleaner.add(slow[6:7])] == [1000]  # 6 beats ahead: decided
    fast = [interval(400, 400 * k) for k in range(1, 10)]
    cleaner = IntervalCleaner()
    assert cleaner.add(fast[:8]) == []  # 7 beats but only 2.8 s ahead of the first
    assert [i.t_beat for i in cleaner.add(fast[8:9])] == [400]
    assert cleaner.horizon_ms == 400


def test_the_rules_with_a_one_beat_guard():
    run = [
        interval(800, 1000),  # 0: one beat before a rejection
        interval(500, 2000, accepted=False),  # 1: rejected
        interval(805, 3000),  # 2: one beat after it
        interval(810, 4000),  # 3: clean, but the beat before is not
        interval(820, 5000),  # 4: clean, difference usable
        interval(1100, 6000),  # 5: one beat before a lost packet
        interval(1090, 7000, contiguous=False),  # 6: the first after a lost packet
        interval(1095, 8000, bootstrap=True),  # 7: bootstrap, and one beat after the gap
        interval(1080, 9000),  # 8: clean, but the beat before is not
        interval(1085, 10000),  # 9: clean, difference usable
    ]
    got = classify_all(IntervalCleaner(guard_ms=0, guard_beats=1), run)
    assert [i.clean for i in got] == [
        False,
        False,
        False,
        True,
        True,
        False,
        False,
        False,
        True,
        True,
    ]
    assert [i.diff_ms for i in got] == [None, None, None, None, 10.0, None, None, None, None, 5.0]


def test_a_jump_beyond_twenty_percent_keeps_the_beats_but_not_the_difference():
    run = [interval(820, 1000), interval(1000, 2000), interval(1010, 3000)]
    got = classify_all(IntervalCleaner(guard_ms=0, guard_beats=0), run)
    assert [i.clean for i in got] == [True, True, True]
    assert [i.diff_ms for i in got] == [None, None, 10.0]


def test_the_time_guard_reaches_both_ways():
    run = [interval(1000, 1000 * k, accepted=k != 5) for k in range(1, 10)]  # rejected at 5 s
    got = classify_all(IntervalCleaner(guard_ms=2500, guard_beats=0), run)
    assert [i.t_beat for i in got if i.clean] == [1000, 2000, 8000, 9000]


def test_the_default_guard_covers_six_beats_at_a_slow_rate():
    run = [interval(1300, 1300 * k, accepted=k != 10) for k in range(1, 20)]  # 46 bpm
    got = classify_all(IntervalCleaner(), run)
    assert [round(i.t_beat / 1300) for i in got if not i.clean] == list(range(4, 17))


def test_a_gap_is_guarded_on_both_sides():
    before = [interval(1000, 1000 * k) for k in range(1, 13)]
    after = [interval(1000, 30_000, contiguous=False)]
    after += [interval(1000, 30_000 + 1000 * k) for k in range(1, 12)]
    got = classify_all(IntervalCleaner(), before + after)
    assert [round(i.t_beat / 1000) for i in got if i.clean] == [1, 2, 3, 4, 5, 6, *range(37, 42)]


def test_a_misplaced_beat_is_suspect_without_any_rejection():
    steady = [interval(1000 + (7 if k % 2 else -7), 1000 * k) for k in range(1, 20)]
    late = [interval(1180, 20_000), interval(820, 20_820)]  # one beat detected 180 ms late
    more = [interval(1000, 20_820 + 1000 * k) for k in range(1, 12)]
    got = classify_all(IntervalCleaner(), steady + late + more)
    suspect = [i.t_beat for i in got if not i.clean]
    assert {20_000, 20_820} <= set(suspect)  # both halves of the pair
    assert min(suspect) >= 20_000 - 6 * 1000 and max(suspect) <= 20_820 + 7 * 1000
    assert all(i.accepted for i in got)


def test_the_ectopic_rule_leaves_a_large_but_ordinary_swing_alone():
    """A big step that does not come back is a change of rate, not a misplaced beat."""
    steady = [interval(1000 + (7 if k % 2 else -7), 1000 * k) for k in range(1, 20)]
    step = [interval(1150 + (7 if k % 2 else -7), 19_000 + 1150 * k) for k in range(1, 14)]
    got = classify_all(IntervalCleaner(), steady + step)
    assert all(i.clean for i in got)
