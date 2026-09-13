"""bridge/session.py: the segments, their hard ends, the regulate threshold, and sessions."""

import json
import math

import pytest
from conftest import run_live

from bridge import session as machine
from bridge.authority import authority
from bridge.baseline import DURATION_MS as BASELINE_MS
from bridge.baseline import Baseline
from bridge.contract import BASELINE_OVERRUN_MS, DIMENSIONS, REGULATE_OVERRUN_MS
from bridge.hrv import HrvReading
from bridge.logging import SessionLog
from bridge.psv import HeartRateWindow, PsvModel
from bridge.session import Session, Timings

# The armband is on from 0 s and the attendant presses start at 10 s, so the capture is 10 to 55 s.
# Each profile: 70 s at 68 bpm, 75 s of load climbing to 100, then regulate's own shape.
REGULATED = "68:70,68-100:75,100-72:40,72:150"  # met well before 75 s
EXTENDS = "68:70,68-100:75,100-72:75,72:150"  # met a couple of seconds after 75 s
STAYS_HIGH = "68:70,68-100:75,100:230"  # never met
FLAT = "68:400"
ZERO = dict.fromkeys(DIMENSIONS, 0.0)
NOMINAL = {"idle": 0, "baseline": 45_000, "load": 75_000, "regulate": 75_000, "resolve": 45_000}


def live(tmp_path, spec, *faults, **kw):
    return run_live(spec, *faults, log_dir=tmp_path, **kw)


def starts(run):
    """{segment: start} for the first run in the log, and the session_end time as 'end'."""
    out = {}
    for segment, at in run.segments():
        out.setdefault(segment, at)
    ends = run.events("session_end")
    if ends:
        out["end"] = ends[0]["at_ms"]
    return out


def first_session(run):
    first = run.states()[0]["session"]
    return [m for m in run.states() if m["session"] == first]


def result(run):
    (only,) = run.events("regulate_result")
    return only


# --- a whole session ---


def test_a_whole_session_walks_every_segment_and_each_ends_on_its_deadline(tmp_path):
    run = live(tmp_path, REGULATED)
    at = starts(run)
    assert at["baseline"] == 10_000
    held = at["load"] - (10_000 + BASELINE_MS)
    assert 2_000 <= held <= BASELINE_OVERRUN_MS
    assert at["regulate"] == at["load"] + 75_000
    assert at["resolve"] == at["regulate"] + 75_000
    assert at["reset"] == at["resolve"] + 45_000
    assert at["end"] == at["reset"] + 20_000
    assert [s for s, _ in run.segments()] == ["baseline", "load", "regulate", "resolve", "reset"]
    assert run.session.segment == "idle" and run.session.session == "S-20260913-0002"


def test_state_goes_out_every_2_s_and_at_every_boundary_with_the_segment_clock(tmp_path):
    run = live(tmp_path, EXTENDS)
    states = first_session(run)
    assert all(
        b["t_engine"] - a["t_engine"] <= 2_000 for a, b in zip(states, states[1:], strict=False)
    )
    assert all(b["t_engine"] >= a["t_engine"] for a, b in zip(states, states[1:], strict=False))
    at = starts(run)
    for segment, start in at.items():
        if segment == "end":
            continue
        boundary = [m for m in states if m["segment"] == segment][0]
        assert 0 <= boundary["t_engine"] - start < 100  # the first tick at or after it
    for msg in states:
        segment = msg["segment"]
        if segment == "idle":
            assert msg["t_session"] is None and msg["segment_nominal_ms"] == 0
            continue
        assert msg["t_session"] == msg["t_engine"] - 10_000
        assert abs(msg["segment_elapsed_ms"] - (msg["t_engine"] - at[segment])) <= 0.5
        expected = 20_000 if segment == "reset" else NOMINAL[segment]
        assert msg["segment_nominal_ms"] == expected


def test_a_press_between_ticks_sends_its_boundary_at_once(tmp_path):
    run = live(tmp_path, REGULATED, start_s=(10.05,), stop_s=(31.05,), until_s=40)
    pressed = [(m["t_engine"], m["segment"], m["segment_elapsed_ms"]) for m in run.states()]
    assert (10_050, "baseline", 0) in pressed and (31_050, "reset", 0) in pressed


def test_the_lattice_sends_nothing_at_a_moment_a_press_already_sent_one(tmp_path):
    run = live(tmp_path, REGULATED, stop_s=(100.0,), start_s=(10.0, 104.0))
    for session in {m["session"] for m in run.states()}:
        times = [m["t_engine"] for m in run.states() if m["session"] == session]
        assert len(times) == len(set(times))


def test_a_stop_on_a_boundary_sends_the_boundary_then_the_reset(tmp_path):
    clean = starts(live(tmp_path / "clean", REGULATED, until_s=150))
    run = live(tmp_path / "stop", REGULATED, stop_s=(clean["regulate"] / 1000,), until_s=150)
    at_stop = [m for m in first_session(run) if m["t_engine"] == round(clean["regulate"])]
    assert [m["segment"] for m in at_stop] == ["regulate", "reset"]
    assert at_stop[0]["seq"] + 1 == at_stop[1]["seq"]


def test_the_state_lattice_stays_on_even_2_s_under_tick_jitter(tmp_path):
    states = first_session(live(tmp_path, FLAT, until_s=60, tick_jitter_ms=90.0))
    lattice = [b for a, b in zip(states, states[1:], strict=False) if a["segment"] == b["segment"]]
    assert len(lattice) > 20 and all(m["t_engine"] % 2_000 < 200 for m in lattice)


# --- baseline and its hold ---


def test_baseline_holds_for_hr_base_with_the_nominal_unchanged_and_nothing_acting(tmp_path):
    run = live(tmp_path, REGULATED)
    states = first_session(run)
    baseline = [m for m in states if m["segment"] == "baseline"]
    assert max(m["segment_elapsed_ms"] for m in baseline) > 45_000
    assert all(m["segment_nominal_ms"] == 45_000 and m["authority"] == ZERO for m in baseline)
    assert all(m["hr_base"] is None for m in states if m["segment"] in ("idle", "baseline"))
    later = [m for m in states if m["segment"] in ("load", "regulate", "resolve", "reset")]
    assert all(m["hr_base"] is not None and m["signal"]["baseline_quality"] > 0 for m in later)
    (end,) = run.events("baseline_end")
    assert end["outcome"] == "ready" and end["held_ms"] == starts(run)["load"] - 55_000


def test_a_disconnect_over_the_window_end_that_is_back_inside_the_hold_is_not_degraded(tmp_path):
    run = live(tmp_path, FLAT, "disconnect@54:5", until_s=100)
    (end,) = run.events("baseline_end")
    assert end["outcome"] == "ready" and end["held_ms"] < BASELINE_OVERRUN_MS


def test_no_hr_base_by_the_cap_enters_load_degraded_on_the_cap_and_stays_degraded(tmp_path):
    run = live(tmp_path, FLAT, "disconnect@52:20", until_s=300, tick_jitter_ms=16.0)
    at = starts(run)
    assert at["load"] == 55_000 + BASELINE_OVERRUN_MS
    (end,) = run.events("baseline_end")
    assert end["outcome"] == "degraded" and "no hr_base" in end["why"]
    later = [m for m in first_session(run) if m["segment"] not in ("idle", "baseline")]
    assert later and all(
        m["hr_base"] is None and m["signal"]["baseline_quality"] == 0.0 for m in later
    )
    # No threshold, so a plain 75 s, whatever the body does, with the reason on record.
    assert at["resolve"] == at["regulate"] + 75_000
    found = result(run)
    assert found["outcome"] == "no_threshold" and "degraded" in found["no_threshold"]
    (activation,) = run.events("load_activation")
    assert activation["activated"] is None and activation["by_hr"] is None
    assert "degraded" in activation["hr_unavailable"] and activation["rmssd_unavailable"]
    (back,) = run.events("rmssd_return")
    assert back["returned"] is None and back["rmssd_unavailable"]


def test_a_result_that_fails_the_gate_goes_to_a_short_reset_at_once(tmp_path):
    run = live(tmp_path, FLAT, "contact_lost@15:25", until_s=120)
    at = starts(run)
    assert "load" not in at
    assert 55_000 < at["reset"] < 55_000 + BASELINE_OVERRUN_MS  # when the result came, not the cap
    assert at["end"] == at["reset"] + 3_000
    (end,) = run.events("baseline_end")
    assert end["outcome"] == "failed" and any("clean data" in p for p in end["problems"])
    assert run.events("session_end")[0]["outcome"] == "gate_failed"
    resets = [m for m in first_session(run) if m["segment"] == "reset"]
    assert resets and all(m["segment_nominal_ms"] == 3_000 for m in resets)
    assert all(m["hr_base"] is None for m in resets)
    slow = live(tmp_path / "slow", FLAT, "contact_lost@15:25", until_s=120, tick_ms=500.0)
    assert starts(slow)["reset"] == at["reset"]  # the result's arrival, not the tick


def test_a_result_that_comes_after_a_stop_in_the_hold_is_never_sent(tmp_path):
    run = live(tmp_path, STAYS_HIGH, stop_s=(59.5,), until_s=70)
    assert run.events("baseline_end")[0]["outcome"] == "stopped"
    assert run.model.baseline is None  # forgotten with the session; it did arrive in the reset
    assert all(
        m["hr_base"] is None and m["signal"]["baseline_quality"] == 0.0 for m in run.states()
    )


@pytest.mark.parametrize("dies_at_s", [20, 40])
def test_an_armband_that_dies_in_baseline_fails_the_gate_at_the_cap(tmp_path, dies_at_s):
    run = live(tmp_path, f"68:{dies_at_s}", until_s=120, tick_jitter_ms=16.0)
    at = starts(run)
    assert "load" not in at and at["reset"] == 55_000 + BASELINE_OVERRUN_MS
    (end,) = run.events("baseline_end")
    assert end["outcome"] == "failed" and end["problems"]
    assert [e["lost"] for e in run.events("signal")][-1] is True
    assert run.session.segment == "idle" and run.session.signal_lost


def test_hold_to_end_starts_load_on_the_end_of_the_hold_even_when_hr_base_came_sooner(tmp_path):
    timings = Timings(hold_ms=11_000, hold_to_end=True)
    run = live(tmp_path, REGULATED, timings=timings, tick_jitter_ms=16.0)
    assert starts(run)["load"] == 10_000 + 56_000
    assert run.events("baseline_end")[0]["outcome"] == "ready"
    # Without hr_base by then, the gate decides at the same moment: degraded load, or reset.
    for spec, fault, to in (
        (FLAT, "disconnect@52:20", "load"),
        ("68:40", "missed_beat@1", "reset"),
    ):
        run = live(tmp_path / to, spec, fault, timings=timings, until_s=100)
        assert starts(run)[to] == 66_000


def test_load_starts_when_the_result_came_whatever_the_ticks(tmp_path):
    runs = [
        live(tmp_path / "a", REGULATED, until_s=100),
        live(tmp_path / "b", REGULATED, until_s=100, tick_jitter_ms=16.0),
        live(tmp_path / "c", REGULATED, until_s=100, tick_ms=500.0),
        live(tmp_path / "d", REGULATED, until_s=100, skip_s=((56.0, 66.0),)),
    ]
    decided = runs[0].model.baseline_decided_ms
    assert 55_000 < decided < 67_000
    assert [starts(run)["load"] for run in runs] == [decided] * 4


@pytest.mark.parametrize(
    ("fault", "skip", "outcome"),
    [
        ("disconnect@52:9.5", ((66.95, 67.2),), "degraded"),  # the result lands just after the cap
        ("disconnect@52:20", ((60.0, 90.0),), "degraded"),
        ("disconnect@50:20", ((60.0, 80.0),), "failed"),
    ],
)
def test_the_hold_is_judged_as_it_stood_at_the_cap_however_late_the_tick(
    tmp_path, fault, skip, outcome
):
    clean = live(tmp_path / "clean", FLAT, fault, seed=29, until_s=100)
    late = live(tmp_path / "late", FLAT, fault, seed=29, until_s=100, skip_s=skip)
    for run in (clean, late):
        (end,) = run.events("baseline_end")
        assert end["outcome"] == outcome
        at = starts(run)
        assert at["load" if outcome == "degraded" else "reset"] == 67_000
        assert all(m["hr_base"] is None for m in first_session(run))


def test_a_zero_hold_judges_the_gate_the_moment_the_window_closes(tmp_path):
    run = live(tmp_path, "68:20", until_s=60, timings=Timings(hold_ms=0))
    assert starts(run)["reset"] == 55_000
    assert run.events("baseline_end")[0]["outcome"] == "failed"


# --- regulate ---


def test_regulated_before_75_s_ends_regulate_at_75_s(tmp_path):
    found = result(live(tmp_path, REGULATED))
    assert found["outcome"] == "regulated" and found["first_met_ms"] < 75_000
    assert found["ran_ms"] == 75_000


def test_not_regulated_by_75_s_extends_until_it_is(tmp_path):
    run = live(tmp_path, EXTENDS)
    found = result(run)
    assert found["outcome"] == "regulated"
    assert 75_000 < found["ran_ms"] == found["first_met_ms"] < 105_000
    at = starts(run)
    assert at["resolve"] == at["regulate"] + found["ran_ms"]
    (met,) = run.events("regulate_met")
    assert met["at_ms"] == found["first_met_ms"]
    regulate = [m for m in first_session(run) if m["segment"] == "regulate"]
    assert max(m["segment_elapsed_ms"] for m in regulate) > 75_000


def test_regulate_proceeds_to_resolve_at_105_s_whatever_the_body_does(tmp_path):
    run = live(tmp_path, STAYS_HIGH, until_s=320)
    found = result(run)
    assert found["outcome"] == "timeout" and found["first_met_ms"] is None
    assert found["ran_ms"] == 75_000 + REGULATE_OVERRUN_MS
    assert starts(run)["resolve"] == starts(run)["regulate"] + 105_000


def test_the_threshold_on_record_is_the_script_s_formula(tmp_path):
    for spec in (REGULATED, STAYS_HIGH):
        found = result(live(tmp_path / spec.replace(":", "_"), spec, until_s=320))
        rise = found["hr_load_bpm"] - found["hr_base_bpm"]
        assert found["rise_bpm"] == pytest.approx(rise)
        assert found["threshold_bpm"] == pytest.approx(found["hr_load_bpm"] - max(5.0, 0.5 * rise))
        assert found["drop_bpm"] == pytest.approx(found["hr_load_bpm"] - found["lowest_window_bpm"])


def test_an_extension_with_no_signal_ends_unjudged_at_once(tmp_path):
    run = live(tmp_path, "68:70,68-100:75,100:30", until_s=320)  # the armband dies 30 s in
    found = result(run)
    assert found["outcome"] == "unjudged" and found["ran_ms"] == 75_000
    (back,) = run.events("rmssd_return")
    assert back["returned"] is None and "classification" in back["rmssd_unavailable"]


def test_an_extension_ends_unjudged_6_2_s_after_the_last_beat_whatever_the_ticks(tmp_path):
    dies = "68:70,68-100:75,100:80"  # the armband dies about 89 s into regulate
    clean = live(tmp_path / "clean", dies, until_s=320)
    stalled = live(tmp_path / "stalled", dies, until_s=320, skip_s=((220.0, 240.0),))
    last = clean.model.last_trusted_beat_ms
    for run in (clean, stalled):
        found = result(run)
        assert found["outcome"] == "unjudged"
        assert starts(run)["resolve"] == pytest.approx(last + 6_200)
        # The loss is on record before the verdict it caused, in regulate.
        lines = [r for _, r in run.records() if r.get("event") in ("signal", "regulate_result")]
        lost = max(k for k, r in enumerate(lines) if r["event"] == "signal" and r["lost"])
        assert (
            lines[lost + 1]["event"] == "regulate_result" and lines[lost]["segment"] == "regulate"
        )
    assert 75_000 < result(clean)["ran_ms"] < 105_000


def test_a_threshold_met_before_the_armband_died_still_counts(tmp_path):
    run = live(tmp_path, "68:70,68-100:75,100-72:40,72:12", until_s=320)  # dies 62 s in
    found = result(run)
    assert found["outcome"] == "regulated" and found["ran_ms"] == 75_000


def test_a_signal_lost_mid_regulate_and_back_by_75_s_still_extends(tmp_path):
    run = live(tmp_path, EXTENDS, "disconnect@166:40")  # regulate 30 s to 70 s
    found = result(run)
    assert found["outcome"] == "regulated" and found["ran_ms"] > 75_000


def test_a_thin_hr_load_sets_no_threshold_and_regulate_runs_a_plain_75_s(tmp_path):
    run = live(tmp_path, STAYS_HIGH, "disconnect@110:20", until_s=320)
    found = result(run)
    assert found["outcome"] == "no_threshold" and found["ran_ms"] == 75_000
    assert found["hr_load_covered_ms"] < machine.LOAD_MIN_COVERED_MS
    assert "22.5" in found["no_threshold"] and found["drop_bpm"] is None
    (activation,) = run.events("load_activation")
    assert activation["by_hr"] is None and activation["hr_unavailable"] == found["no_threshold"]


class Scripted:
    """heart_rate() from a function of the window, recording every window asked for."""

    def __init__(self, bpm_covered):
        self.bpm_covered = bpm_covered
        self.asked = []

    def heart_rate(self, start, end):
        self.asked.append((start, end))
        bpm, covered = self.bpm_covered(start, end)
        return HeartRateWindow(bpm, covered, 0 if bpm is None else 20)


def regulate_with(hr_base, hr_load, load_covered, window):
    """A _Regulate starting at 100 s, HR_load taken, then windows judged to 205 s."""

    def bpm_covered(start, end):
        if end == 100_000:
            return hr_load, load_covered
        return window(start, end)

    model = Scripted(bpm_covered)
    r = machine._Regulate(100_000, hr_base, Timings())
    assert not r.take_hr_load(model, 101_999)
    assert r.take_hr_load(model, 102_000)
    r.judge_windows(model, 205_000)
    return r, model


@pytest.mark.parametrize(
    ("hr_base", "hr_load", "threshold"),
    [
        (70.0, 90.0, 80.0),
        (70.0, 80.0, 75.0),
        (70.0, 74.0, 69.0),
        (70.0, 66.0, 61.0),
        (70.0, 58.0, 53.0),
        (60.0, 130.0, 95.0),
    ],
)
def test_the_threshold_is_hr_load_less_the_larger_of_5_bpm_and_half_the_rise(
    hr_base, hr_load, threshold
):
    r, _ = regulate_with(hr_base, hr_load, 30_000, lambda s, e: (None, 0.0))
    assert r.threshold == pytest.approx(threshold)


def test_a_window_at_the_threshold_counts_and_one_under_15_s_covered_does_not():
    at, under = 180_000, 150_000

    def window(start, end):
        if end == under:
            return 70.0, 14_999.0
        if end == at:
            return 80.0, 15_000.0
        return 95.0, 20_000.0

    r, _ = regulate_with(70.0, 90.0, 30_000, window)
    assert r.threshold == 80.0 and r.first_met == at - 100_000
    assert r.lowest == 80.0


def test_windows_lie_wholly_inside_regulate_and_are_judged_once_on_the_grid():
    r, model = regulate_with(70.0, 90.0, 30_000, lambda s, e: (95.0, 20_000.0))
    windows = [(s, e) for s, e in model.asked if e != 100_000]
    assert windows[0] == (100_000, 120_000)
    assert all(e - s == machine.WINDOW_MS for s, e in windows)
    assert [e for _, e in windows] == list(range(120_000, 205_001, machine.WINDOW_STEP_MS))
    r.judge_windows(model, 205_000)
    assert len([w for w in model.asked if w[1] != 100_000]) == len(windows)


def test_hr_load_needs_22_5_of_its_30_s_covered():
    r, _ = regulate_with(70.0, 90.0, 22_499.0, lambda s, e: (60.0, 20_000.0))
    assert r.threshold is None and r.first_met is None and "22.5" in r.no_threshold
    r, _ = regulate_with(70.0, 90.0, 22_500.0, lambda s, e: (60.0, 20_000.0))
    assert r.threshold == 80.0 and r.first_met == 20_000


def test_no_window_is_judged_past_the_cap():
    r, model = regulate_with(70.0, 90.0, 30_000, lambda s, e: (95.0, 20_000.0))
    r.judge_windows(model, 400_000)
    assert max(e for _, e in model.asked) == 100_000 + 105_000


@pytest.mark.parametrize(("met_at", "cap"), [(60_000, 75_000), (75_000, 75_000), (90_000, 90_000)])
def test_no_window_is_judged_past_where_regulate_ends(met_at, cap):
    def window(start, end):
        return (80.0 if end == 100_000 + met_at else 95.0), 20_000.0

    r, model = regulate_with(70.0, 90.0, 30_000, window)
    r.judge_windows(model, 400_000)
    assert r.first_met == met_at and r.cap() == 100_000 + cap
    assert max(e for _, e in model.asked) == 100_000 + cap
    plain, model = regulate_with(None, 90.0, 30_000, lambda s, e: (60.0, 20_000.0))
    plain.judge_windows(model, 400_000)
    assert plain.cap() == max(e for _, e in model.asked) == 175_000


def test_after_a_stalled_tick_the_result_counts_only_windows_inside_regulate(tmp_path):
    spec = "68:70,68-100:75,100-60:150"
    clean = result(live(tmp_path / "clean", spec))
    stalled = result(live(tmp_path / "stalled", spec, skip_s=((210.0, 240.0),)))
    assert clean["outcome"] == "regulated" and clean["ran_ms"] > 75_000
    assert {**stalled, "t_engine": None} == {**clean, "t_engine": None}  # logged later, same result


def test_a_long_stall_over_the_start_of_regulate_keeps_the_threshold(tmp_path):
    clean = result(live(tmp_path / "clean", EXTENDS))
    stalled = result(live(tmp_path / "stalled", EXTENDS, skip_s=((135.0, 200.0),)))
    assert stalled["no_threshold"] is None
    assert stalled["threshold_bpm"] == pytest.approx(clean["threshold_bpm"], abs=0.5)


# --- the records ---


def test_the_script_s_numbers():
    assert (machine.THRESHOLD_FLOOR_BPM, machine.THRESHOLD_RISE_SHARE) == (5.0, 0.5)
    assert (machine.WINDOW_MS, machine.WINDOW_MIN_COVERED_MS, machine.WINDOW_STEP_MS) == (
        20_000,
        15_000,
        500,
    )
    assert (machine.LOAD_MIN_COVERED_MS, machine.SETTLE_MS, machine.SIGNAL_LOST_MS) == (
        22_500,
        2_000,
        6_200,
    )
    assert (machine.ACTIVATION_BPM, machine.ACTIVATION_RMSSD_RATIO) == (6.0, 0.80)
    assert (machine.RETURN_RMSSD_RATIO, machine.RMSSD_MIN_DIFFERENCES) == (0.95, 20)


class Stub:
    """The two windows the records read, scripted."""

    def __init__(self, hr_load, rmssd):
        self.hr_load, self.reading = hr_load, rmssd

    def heart_rate(self, start, end):
        return HeartRateWindow(self.hr_load, 30_000.0, 40)

    def rmssd(self, start, end):
        return self.reading


def record_with(tmp_path, *, hr_base, hr_load, rmssd, differences, base_rmssd, base_differences):
    log = SessionLog(tmp_path)
    model = Stub(hr_load, HrvReading(0, 0, 0, None, rmssd, None, differences, differences, 0))
    session = Session(model, log, lambda msg: None, now_ms=0)
    session._baseline = Baseline(0, 45_000, hr_base, base_rmssd, None, 0.0, 1.0, 40_000, 50, True,
                                 (), 4.0, base_differences)  # fmt: skip
    session._regulate = machine._Regulate(100_000, hr_base, Timings())
    session._regulate.take_hr_load(model, 102_000)
    session._try_activation(102_000, final=True)
    session._return_window = (145_000, 175_000)
    session._try_return(180_000, final=True)
    log.close()
    events = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    (activation,) = [e for e in events if e.get("event") == "load_activation"]
    (back,) = [e for e in events if e.get("event") == "rmssd_return"]
    return activation, back


def test_the_activation_and_return_bounds_are_inclusive(tmp_path):
    base = {"hr_base": 70.0, "differences": 20, "base_rmssd": 40.0, "base_differences": 20}
    at, back = record_with(tmp_path / "at", hr_load=76.0, rmssd=32.0, **base)
    assert at["by_hr"] is True and at["by_rmssd"] is True and back["returned"] is False
    under, back = record_with(tmp_path / "under", hr_load=75.99, rmssd=32.01, **base)
    assert under["by_hr"] is False and under["by_rmssd"] is False and under["activated"] is False
    _, back = record_with(tmp_path / "back", hr_load=76.0, rmssd=38.0, **base)
    assert back["returned"] is True
    _, back = record_with(tmp_path / "short", hr_load=76.0, rmssd=37.99, **base)
    assert back["returned"] is False


def test_an_rmssd_part_needs_20_differences_on_both_sides(tmp_path):
    common = {"hr_base": 70.0, "hr_load": 90.0, "rmssd": 20.0, "base_rmssd": 40.0}
    thin_base, back = record_with(tmp_path / "b", differences=30, base_differences=19, **common)
    assert thin_base["by_rmssd"] is None and "rmssd_base from 19" in thin_base["rmssd_unavailable"]
    assert back["returned"] is None and thin_base["activated"] is True
    thin, back = record_with(tmp_path / "w", differences=19, base_differences=20, **common)
    assert (
        thin["by_rmssd"] is None and "19 clean differences in load's" in thin["rmssd_unavailable"]
    )
    assert back["returned"] is None and "regulate's last 30 s" in back["rmssd_unavailable"]


def test_load_activation_and_rmssd_return_are_recorded_once_each(tmp_path):
    run = live(tmp_path, EXTENDS)
    at = starts(run)
    (activation,) = run.events("load_activation")
    assert at["regulate"] < activation["t_engine"] < at["regulate"] + 10_000  # once classified
    (back,) = run.events("rmssd_return")
    assert at["resolve"] < back["t_engine"] < at["resolve"] + 10_000
    assert activation["by_hr"] is True and activation["activated"] is True
    assert activation["hr_load_bpm"] >= activation["hr_base_bpm"] + machine.ACTIVATION_BPM
    assert isinstance(activation["by_rmssd"], bool) and activation["rmssd_unavailable"] is None
    assert activation["rmssd_differences"] >= machine.RMSSD_MIN_DIFFERENCES
    assert activation["by_rmssd"] == (activation["rmssd_ms"] <= 0.8 * activation["rmssd_base_ms"])
    (back,) = run.events("rmssd_return")
    assert isinstance(back["returned"], bool)
    assert back["returned"] == (back["rmssd_ms"] >= 0.95 * back["rmssd_base_ms"])


def test_a_flat_run_is_not_activated(tmp_path):
    run = live(tmp_path, FLAT, until_s=320)
    (activation,) = run.events("load_activation")
    assert activation["by_hr"] is False
    assert result(run)["threshold_bpm"] == pytest.approx(result(run)["hr_load_bpm"] - 5.0)


def test_rmssd_parts_are_unavailable_under_20_differences_and_heart_rate_decides(tmp_path):
    run = live(tmp_path, EXTENDS, "artefact_burst@42:8")
    assert run.events("baseline_end")[0]["rmssd_base_differences"] < 20
    (activation,) = run.events("load_activation")
    assert activation["by_rmssd"] is None and "needs 20" in activation["rmssd_unavailable"]
    assert activation["activated"] is True and activation["by_hr"] is True
    (back,) = run.events("rmssd_return")
    assert back["returned"] is None and "needs 20" in back["rmssd_unavailable"]


def test_an_artefact_burst_late_in_load_leaves_its_rmssd_part_unavailable(tmp_path):
    run = live(tmp_path, EXTENDS, "artefact_burst@110:3", "artefact_burst@122:3")
    assert run.events("baseline_end")[0]["rmssd_base_differences"] >= 20
    (activation,) = run.events("load_activation")
    assert activation["by_rmssd"] is None and "load's last 30 s" in activation["rmssd_unavailable"]
    assert activation["activated"] is True


def test_stopping_just_into_regulate_still_records_the_threshold_and_activation(tmp_path):
    clean = starts(live(tmp_path / "clean", REGULATED, until_s=150))
    stop_s = (clean["regulate"] + 900) / 1000
    run = live(tmp_path / "stop", REGULATED, stop_s=(stop_s,), until_s=150)
    (threshold,) = run.events("regulate_threshold")
    (activation,) = run.events("load_activation")
    assert threshold["hr_load_bpm"] is not None and activation["by_hr"] is True
    assert "classification" in activation["rmssd_unavailable"]
    assert not run.events("regulate_result") and not run.events("rmssd_return")


# --- the attendant ---


def test_start_is_refused_with_no_signal_and_while_a_session_runs(tmp_path):
    run = live(tmp_path, EXTENDS, "disconnect@0:20", start_s=(10.0, 30.0, 40.0, 100.0))
    assert run.refusals == [
        (10.0, "no_signal"),
        (30.0, None),
        (40.0, "running"),
        (100.0, "running"),
    ]
    assert [e["reason"] for e in run.events("start_refused")] == ["no_signal", "running", "running"]
    assert [s for s, _ in run.segments()].count("baseline") == 1
    assert starts(run)["baseline"] == 30_000


def test_start_is_refused_while_resetting_and_accepted_the_moment_reset_ends(tmp_path):
    run = live(tmp_path, REGULATED, start_s=(10.0, 60.0, 61.0), stop_s=(58.0,), until_s=70)
    assert run.refusals[1:] == [(60.0, "resetting"), (61.0, None)]


def test_start_is_refused_6_2_s_after_the_last_beat(tmp_path):
    run = live(tmp_path, "68:20", until_s=30)
    last = run.model.last_trusted_beat_ms
    for after, expected in ((6_199, None), (6_200, "no_signal")):
        log = SessionLog(tmp_path / f"direct{after}")
        session = Session(run.model, log, lambda msg: None, now_ms=last)
        assert session.start(last + after) == expected
        log.close()


@pytest.mark.parametrize("stop_at_s", [30.0, 100.0, 170.0, 230.0])
def test_stop_goes_through_a_3_s_reset_to_a_new_session(tmp_path, stop_at_s):
    run = live(tmp_path, EXTENDS, stop_s=(stop_at_s,), until_s=stop_at_s + 10)
    at = starts(run)
    assert at["reset"] == stop_at_s * 1000 and at["end"] == at["reset"] + 3_000
    assert run.events("session_end")[0]["outcome"] == "stopped"
    resets = [m for m in first_session(run) if m["segment"] == "reset"]
    assert resets and all(m["authority"] == ZERO for m in resets)
    assert all(m["segment_nominal_ms"] == 3_000 for m in resets)
    assert run.session.segment == "idle" and run.session.session == "S-20260913-0002"
    assert run.session.regulate_result is None
    stopped_in_regulate = at.get("regulate", math.inf) < at["reset"]
    assert len(run.events("load_activation")) == int(stopped_in_regulate)
    if at.get("load", math.inf) > at["reset"]:
        (end,) = run.events("baseline_end")
        assert end["outcome"] == "stopped" and end["held_ms"] == 0


def test_stop_does_nothing_in_idle_or_reset(tmp_path):
    run = live(tmp_path, REGULATED, stop_s=(5.0, 30.0, 31.0), until_s=40)
    assert [s for s, _ in run.segments()] == ["baseline", "reset"]
    model, log = PsvModel(), SessionLog(tmp_path / "direct")
    session = Session(model, log, lambda msg: None, now_ms=0)
    assert session.stop(1_000) is False and session.segment == "idle"
    log.close()


# --- sessions ---


def test_each_session_numbers_its_messages_from_1_and_logs_in_its_own_file(tmp_path):
    run = live(tmp_path, EXTENDS, start_s=(10.0, 300.0), stop_s=(320.0,), until_s=330)
    sessions = sorted({m["session"] for m in run.sent})
    assert sessions == ["S-20260913-0001", "S-20260913-0002", "S-20260913-0003"]
    for session in sessions:
        for kind in ("state", "beat"):
            seqs = [m["seq"] for m in run.sent if m["session"] == session and m["type"] == kind]
            assert seqs == list(range(1, len(seqs) + 1)), (session, kind)
    records = run.records()
    for file_session, record in records:
        if "msg" in record:
            assert record["msg"]["session"] == file_session
    for session in sessions:
        lines = [r for s, r in records if s == session]
        assert lines[0]["event"] == "session_start" and lines[0]["session"] == session
    ends = [(s, r) for s, r in records if r.get("event") == "session_end"]
    assert [s for s, _ in ends] == sessions[:2]
    assert [r["outcome"] for _, r in ends] == ["completed", "stopped"]
    for session, _ in ends:
        events = [r for s, r in records if s == session and "event" in r]
        assert events[-1]["event"] == "session_end"


def test_a_new_session_forgets_the_baseline_but_not_the_armband(tmp_path):
    run = live(tmp_path, EXTENDS, start_s=(10.0, 300.0), until_s=380)
    first_end = run.events("session_end")[0]["at_ms"]
    second = [m for m in run.states() if m["session"] == "S-20260913-0002"]
    idle = [m for m in second if m["segment"] == "idle"]
    assert idle and all(m["hr_base"] is None and m["authority"] == ZERO for m in idle)
    assert all(m["confidence"] == ZERO and set(m["psv"].values()) == {0.5} for m in idle)
    assert all(m["hr_bpm"] is not None for m in second)
    assert [e["outcome"] for e in run.events("baseline_end")] == ["ready", "ready"]
    starts_of_sessions = run.events("session_start")
    assert starts_of_sessions[1]["at_ms"] == first_end
    assert idle[0]["segment_elapsed_ms"] == round(idle[0]["t_engine"] - first_end)


def test_a_session_adopts_the_log_s_open_session(tmp_path):
    log = SessionLog(tmp_path)
    opened = log.start_session()
    sent = []
    session = Session(PsvModel(), log, sent.append, now_ms=5_000)
    session.tick(5_000)
    log.close()
    assert session.session == opened and sent[0]["session"] == opened and sent[0]["seq"] == 1
    assert len(list(tmp_path.glob("S-*.jsonl"))) == 1


def test_a_session_on_a_closed_log_takes_a_new_session(tmp_path):
    log = SessionLog(tmp_path)
    closed = log.start_session()
    log.close()
    sent = []
    session = Session(PsvModel(), log, sent.append, now_ms=0)
    session.tick(0)
    log.close()
    assert session.session != closed and session.session == log.session == sent[0]["session"]
    assert f'"session":"{session.session}"' in log.path.read_text(encoding="utf-8")


# --- time ---


def test_a_stalled_loop_sends_every_boundary_it_missed_in_order_on_exact_deadlines(tmp_path):
    clean = live(tmp_path / "clean", REGULATED)
    stalled = live(tmp_path / "stalled", REGULATED, skip_s=((130.0, 140.0), (200.0, 270.0)))
    assert starts(stalled) == starts(clean)
    states = first_session(stalled)
    order = [m["segment"] for m in states]
    assert order.index("regulate") < order.index("resolve") < order.index("reset")
    at = starts(stalled)
    caught_up = [m for m in states if m["t_engine"] == 270_000]
    assert [m["segment"] for m in caught_up] == ["resolve", "reset"]
    resolve, reset = caught_up
    assert resolve["segment_elapsed_ms"] == round(at["reset"] - at["resolve"])
    assert reset["segment_elapsed_ms"] == round(270_000 - at["reset"])
    assert resolve["authority"] == ZERO  # its taper is over by 45 s


def test_the_resolve_taper_starts_from_the_last_regulate_message(tmp_path):
    states = first_session(live(tmp_path, REGULATED))
    k = next(i for i, m in enumerate(states) if m["segment"] == "resolve")
    entry, first = states[k - 1], states[k]
    assert entry["segment"] == "regulate"
    rule = authority(
        first["confidence"],
        "resolve",
        segment_elapsed_ms=first["segment_elapsed_ms"],
        segment_nominal_ms=first["segment_nominal_ms"],
        resolve_entry=entry["authority"],
    )
    assert first["authority"] == rule and first["authority"]["arousal"] > 0.9


def test_time_never_runs_backwards_or_past_what_the_link_can_carry(tmp_path):
    sent = []
    log = SessionLog(tmp_path)
    session = Session(PsvModel(), log, sent.append, now_ms=0)
    session.tick(10_000)
    for bad in (9_500, float("nan"), float("inf"), -1, True, 2**53, 2**60):
        session.tick(bad)
    session.tick(12_000)
    log.close()
    assert [m["t_engine"] for m in sent] == [10_000, 12_000]
    assert [m["segment_elapsed_ms"] for m in sent] == [10_000, 12_000]


def test_tick_jitter_moves_no_boundary(tmp_path):
    clean = starts(live(tmp_path / "clean", STAYS_HIGH, until_s=320))
    jittered = starts(live(tmp_path / "jitter", STAYS_HIGH, until_s=320, tick_jitter_ms=16.0))
    assert jittered == pytest.approx(clean)


# --- the shape of it ---


def test_the_worst_case_from_start_fits_the_4_45_cap():
    t = Timings()
    worst = BASELINE_MS + t.hold_ms + t.load_ms + t.regulate_ms + t.extension_ms + t.resolve_ms
    assert worst == 282_000 <= 285_000
    assert (t.hold_ms, t.extension_ms) == (BASELINE_OVERRUN_MS, REGULATE_OVERRUN_MS)


@pytest.mark.parametrize(
    "bad",
    [
        {"hold_ms": BASELINE_OVERRUN_MS + 1},
        {"extension_ms": REGULATE_OVERRUN_MS + 1},
        {"hold_ms": -1},
        {"load_ms": 0},
        {"regulate_ms": 19_999},
        {"resolve_ms": math.nan},
        {"reset_ms": True},
        {"load_ms": math.inf},
        {"stopped_reset_ms": 0},
        {"hold_to_end": "yes"},
    ],
)
def test_timings_outside_the_contract_are_refused(bad):
    with pytest.raises(ValueError):
        Timings(**bad)


def test_the_schedule_says_when_each_segment_can_end(tmp_path):
    log = SessionLog(tmp_path)
    session = Session(PsvModel(), log, lambda msg: None, now_ms=0)
    assert session.schedule == machine.Schedule("idle", 0.0, None, None)
    log.close()
    schedule = machine.Schedule

    def at(spec, until_s, *faults, **kw):
        run = live(
            tmp_path / f"{len(list(tmp_path.iterdir()))}", spec, *faults, until_s=until_s, **kw
        )
        return run.session.schedule, starts(run)

    assert at(FLAT, 30)[0] == schedule("baseline", 10_000.0, 55_000.0, 67_000.0)
    held = Timings(hold_ms=11_000, hold_to_end=True)
    assert at(FLAT, 30, timings=held)[0] == schedule("baseline", 10_000.0, 66_000.0, 66_000.0)
    now, s = at(FLAT, 100)
    assert now == schedule("load", s["load"], s["load"] + 75_000, s["load"] + 75_000)
    now, s = at(FLAT, 150, "disconnect@52:20")
    assert now == schedule("regulate", 142_000.0, 217_000.0, 217_000.0)
    now, s = at(STAYS_HIGH, 150)
    r = s["regulate"]
    assert now == schedule("regulate", r, r + 75_000, r + 105_000)
    now, s = at(REGULATED, 190)
    assert now == schedule(
        "regulate", s["regulate"], s["regulate"] + 75_000, s["regulate"] + 75_000
    )
    now, s = at(REGULATED, 230)
    assert now == schedule("resolve", s["resolve"], s["resolve"] + 45_000, s["resolve"] + 45_000)
    now, s = at(REGULATED, 262)
    assert now == schedule("reset", s["reset"], s["reset"] + 20_000, s["reset"] + 20_000)
    now, s = at(REGULATED, 32, stop_s=(30.0,))
    assert now == schedule("reset", 30_000.0, 33_000.0, 33_000.0)


def test_the_session_never_takes_a_packet():
    assert not [name for name in dir(Session) if "packet" in name.lower()]
