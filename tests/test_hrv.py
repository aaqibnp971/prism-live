"""bridge/hrv.py: which intervals are clean, and what RMSSD and heart rate come out of them."""

import math
import random
import statistics

import pytest
from conftest import false_data, run_pipeline, true_hr, true_rmssd

from bridge import hrv as hrv_rules
from bridge.beat_scheduler import Interval
from bridge.hrv import ECTOPIC_QUARTILE_DEVIATIONS, SPREAD_DIFFS, IntervalCleaner, RollingHrv

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
        # One beat detected late and no rejection anywhere: rmssd_base read 62.5 % high with the
        # earlier pair test, which wanted the pair to sum to twice the local median.
        ("60:60", ("artefact_burst@35:0.5",), 4),
        # This leaked when the threshold came from 31 differences instead of 32.
        ("55:90", ("artefact_burst@40:0.5",), 492),
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


def steady_run(count, rr=1000.0, seed=3, start_ms=0.0):
    """count beats around rr, with the few ms of random variation a resting heart has."""
    rng = random.Random(seed)
    beats, t = [], start_ms
    for _ in range(count):
        size = rr + rng.gauss(0.0, 10.0)
        t += size
        beats.append(interval(size, t))
    return beats


def then(run, *sizes):
    """run continued by intervals of the given sizes."""
    out = list(run)
    for size in sizes:
        out.append(interval(size, out[-1].t_beat + size))
    return out


def threshold_of(run):
    sizes = [abs(b.rr_ms - a.rr_ms) for a, b in zip(run, run[1:], strict=False)][-SPREAD_DIFFS:]
    q1, _, q3 = statistics.quantiles(sizes, n=4)
    return ECTOPIC_QUARTILE_DEVIATIONS * (q3 - q1) / 2


@pytest.mark.parametrize("pair", [(1180, 820), (820, 1180)], ids=["late beat", "early beat"])
def test_a_misplaced_beat_is_suspect_without_any_rejection(pair):
    run = then(steady_run(20), *pair, *[1000] * 11)
    first, second = run[20], run[21]
    got = classify_all(IntervalCleaner(), run)
    suspect = [i.t_beat for i in got if not i.clean]
    assert {first.t_beat, second.t_beat} <= set(suspect)  # both halves of the pair
    assert min(suspect) >= first.t_beat - 6_200 and max(suspect) <= second.t_beat + 7_200
    assert all(i.accepted for i in got)


def test_the_neighbours_must_swing_back_far_enough(monkeypatch):
    """Their decision boundary, c1 and c2, decides a drop of 1.5 thresholds flanked by
    differences of the other sign of only 0.2 thresholds: not a misplaced beat."""
    steady = steady_run(24)
    level, step = steady[-1].rr_ms, threshold_of(steady)
    run = then(steady, level + 0.2 * step, level - 1.3 * step, *[level - 1.1 * step] * 12)
    assert all(i.clean for i in classify_all(IntervalCleaner(), run))
    monkeypatch.setattr(hrv_rules, "ECTOPIC_C1", 0.0)
    monkeypatch.setattr(hrv_rules, "ECTOPIC_C2", 0.0)
    assert not all(i.clean for i in classify_all(IntervalCleaner(), run))


def test_the_ectopic_rule_leaves_a_large_but_ordinary_swing_alone():
    """A big step that does not come back is a change of rate, not a misplaced beat."""
    steady = steady_run(20)
    got = classify_all(IntervalCleaner(), then(steady, *[1150] * 13))
    assert all(i.clean for i in got)


def test_the_threshold_comes_from_exactly_the_last_32_differences():
    """Built so that the 32 differences before the middle one give a threshold above it, and the
    newest 31 alone give one below it. The oldest difference, 1 ms, is the one that decides."""
    rng = random.Random(1)
    sizes = [1.0] + [rng.uniform(5, 40) for _ in range(30)] + [300.0]
    q1, _, q3 = statistics.quantiles(sizes, n=4)
    from_32 = ECTOPIC_QUARTILE_DEVIATIONS * (q3 - q1) / 2
    q1, _, q3 = statistics.quantiles(sizes[1:], n=4)
    from_31 = ECTOPIC_QUARTILE_DEVIATIONS * (q3 - q1) / 2
    middle = (from_31 + from_32) / 2
    assert from_31 < middle < from_32
    level, sign, steps = 1000.0, 1, []
    for size in sizes[:-1]:
        level += sign * size
        sign = -sign
        steps.append(level)
    steps += [level + 300.0, level + 300.0 - middle, level + 600.0 - middle]
    run = then([interval(1000.0, 1000.0)], *steps)
    assert all(i.clean for i in classify_all(IntervalCleaner(), run))
