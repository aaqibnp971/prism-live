"""bridge/psv.py: valence cannot move, and the other three follow the body honestly."""

import copy
import dataclasses
import inspect
import json
import math
import random

import pytest
from conftest import LATENCY_MS, run_session, true_hr

import tools.synthetic_rr as synthetic
from bridge import psv
from bridge.authority import authority
from bridge.baseline import Baseline
from bridge.beat_scheduler import BeatScheduler, Interval, PacketResult
from bridge.contract import validate
from bridge.psv import (
    BaselinePhase,
    Confidences,
    Dims,
    PsvEstimate,
    PsvModel,
    SignalQuality,
    TaskLoad,
    Values,
    blend_cognitive_load,
)
from tools.synthetic_rr import Beat, Fault, Profile, generate

# 20 s of armband before a 45 s baseline, then load 65-140 s, regulate 140-215 s, resolve.
ARC = "68:65,68-105:75,105-75:75,75:45"
SMALL_RISE = "68:65,68-78:75,78-70:75,70:45"
TINY_RISE = "68:65,68-74:75,74-68:75,68:45"
STEEP_BASELINE = "95-70:65,70-80:75,80-72:75,72:45"
GENTLE_BASELINE = "80-74:65,74-84:75,84-76:75,76:45"
LOAD_END, REGULATE_END = 140, 214

GARBAGE = [
    math.nan,
    math.inf,
    -math.inf,
    None,
    "0.5",
    b"1",
    True,
    False,
    10**400,
    1e308,
    -1e308,
    -0.0,
    5e-324,
    -1,
    0,
    [],
    {},
    object(),
]


def assert_sound(estimate: PsvEstimate, where: str = "") -> None:
    """Valence exactly as it must be, everything else finite and in range, the log valid JSON,
    and a state message built from it valid against the contract."""
    for dims, want in ((estimate.psv, 0.5), (estimate.confidence, 0.0)):
        for got in (dims.valence, dims.to_wire()["valence"]):
            assert type(got) is float and got == want, where
            assert math.copysign(1.0, got) == 1.0, where  # not -0.0
        for name in ("arousal", "cognitive_load", "readiness"):
            value = getattr(dims, name)
            assert type(value) is float and math.isfinite(value) and 0.0 <= value <= 1.0, where
    s = estimate.signal
    parts = s.accepted_factor * s.silence_factor * s.recovery_factor * s.contact_factor
    assert math.isclose(s.factor, parts, abs_tol=1e-12), where
    json.dumps(estimate.log_fields(), allow_nan=False)
    for segment in ("baseline", "load", "regulate"):
        validate(state_message(estimate, segment), "out")


def state_message(estimate: PsvEstimate, segment: str) -> dict:
    confidence = estimate.confidence.to_wire()
    granted = authority(
        confidence, segment, segment_elapsed_ms=1000, segment_nominal_ms=75_000, resolve_entry=None
    )
    signal = estimate.signal
    return {
        "type": "state",
        "v": 1,
        "session": "S-20260913-0001",
        "seq": 1,
        "t_engine": round(estimate.t_ms),
        "t_session": 1000,
        "segment": segment,
        "segment_elapsed_ms": 1000,
        "segment_nominal_ms": 75_000,
        "psv": estimate.psv.to_wire(),
        "confidence": confidence,
        "authority": granted,
        "hr_bpm": estimate.hr_bpm,
        "hr_base": None,
        "signal": {
            "contact": signal.contact is not False,
            "rr_accepted_pct": signal.accepted_fraction or 0.0,
            "baseline_quality": 0.0,
        },
    }


def packets(spec: str, *faults: str, seed: int = 1) -> list[tuple[float, PacketResult]]:
    profile = Profile.from_spec(spec)
    scheduler = BeatScheduler()
    out = []
    for note in generate(profile, [Fault.from_spec(f) for f in faults], seed):
        now = note.t_s * 1000 + LATENCY_MS
        out.append((now, scheduler.on_packet(now, note.payload)))
    return out


def ready_model() -> tuple[PsvModel, float]:
    """A model part way into load, with a baseline that passed."""
    model = PsvModel()
    started = False
    for now, result in packets(ARC):
        if not started and now >= 20_000:
            started = model.start_baseline(20_000)
        model.on_packet(now, result)
        if now > 100_000:
            break
    assert model.estimate(now).phase is BaselinePhase.READY
    return model, now


# --- valence: structurally unable to move ---


@pytest.mark.parametrize(
    "cls", [Dims, Values, Confidences, PsvEstimate, SignalQuality, TaskLoad, PsvModel]
)
def test_nothing_can_hold_or_be_given_a_valence(cls):
    if dataclasses.is_dataclass(cls):
        assert "valence" not in [f.name for f in dataclasses.fields(cls)]
    assert "valence" not in inspect.signature(cls).parameters
    for _, method in inspect.getmembers(cls, inspect.isfunction):
        assert "valence" not in inspect.signature(method).parameters


def test_valence_is_neutral_with_exactly_zero_confidence_through_every_path():
    model, now = ready_model()
    assert_sound(model.estimate(now))
    assert psv.VALENCE == 0.5 and psv.VALENCE_CONFIDENCE == 0.0


def test_an_estimate_cannot_be_changed():
    model, now = ready_model()
    estimate = model.estimate(now)
    for target, name in (
        (estimate, "psv"),
        (estimate.psv, "arousal"),
        (estimate.psv, "valence"),
        (estimate.confidence, "valence"),
        (estimate.signal, "factor"),
    ):
        with pytest.raises(AttributeError):
            setattr(target, name, 1.0)


def test_valence_holds_under_every_garbage_input():
    base_model, now = ready_model()
    real = packets(ARC)[120][1]
    interval = next(i for i in real.intervals)
    for junk in GARBAGE:
        calls = [
            lambda m, j=junk: m.on_packet(j, real),
            lambda m, j=junk: m.on_packet(now + 1000, j),
            lambda m, j=junk: m.on_packet(now + 1000, PacketResult(j, (), j)),
            lambda m, j=junk: m.on_packet(now + 1000, PacketResult((j, j), (), True)),
            lambda m, j=junk: m.start_baseline(j),
            lambda m, j=junk: m.add_task_event(j, j, j, j, j),
            lambda m, j=junk: m.add_task_event(now, "miss", j, j, j),
        ]
        calls += [
            lambda m, j=junk, f=f.name: m.on_packet(
                now + 1000, PacketResult((dataclasses.replace(interval, **{f: j}),), (), True)
            )
            for f in dataclasses.fields(Interval)
        ]
        calls += [
            lambda m, j=junk, f=f.name: setattr(
                m, "_baseline", dataclasses.replace(m._baseline, **{f: j})
            )
            for f in dataclasses.fields(Baseline)
        ]
        for k, call in enumerate(calls):
            model = copy.deepcopy(base_model)
            call(model)
            for read in (junk, now, now + 1000, now + 30_000):
                assert_sound(model.estimate(read), f"garbage {junk!r}, call {k}, read {read!r}")
    assert_sound(PsvModel(tuning="nonsense").estimate(0))  # type: ignore[arg-type]


@pytest.mark.parametrize("seed", range(16))
def test_valence_holds_through_random_sequences_of_real_and_garbage_inputs(seed):
    rng = random.Random(seed)
    stream = packets(ARC, "artefact_burst@90", "disconnect@150:6", "contact_lost@200:4", seed=seed)
    model, k, now = PsvModel(), 0, 0.0

    def junk():
        return rng.choice(GARBAGE)

    for op in range(200):
        choice = rng.random()
        if choice < 0.55 and k < len(stream):
            now, result = stream[k]
            k += rng.choice((1, 1, 1, 5))
            if rng.random() < 0.3:
                field = rng.choice(("intervals", "contact"))
                result = dataclasses.replace(result, **{field: junk()})
            model.on_packet(junk() if rng.random() < 0.1 else now, result)
        elif choice < 0.65:
            model.start_baseline(rng.choice((now, now - 50_000, junk())))
        elif choice < 0.7:
            model.mark_baseline_degraded()
        elif choice < 0.73:
            model.reset()
        elif choice < 0.8:
            model.add_task_event(now, rng.choice(("split", "lock", junk())), junk(), junk(), junk())
        elif choice < 0.85 and model._baseline is not None:
            field = rng.choice(dataclasses.fields(Baseline)).name
            model._baseline = dataclasses.replace(model._baseline, **{field: junk()})
        read = rng.choice((now, now + rng.uniform(-60_000, 60_000), junk()))
        assert_sound(model.estimate(read), f"seed {seed}, op {op}")


# --- the baseline: bars climb from zero, and meet the result without a step ---


def test_confidence_is_zero_when_the_baseline_starts_however_long_the_armband_was_on():
    run = run_session("68:120", baseline_at_s=60)
    before = run.at(58)
    assert before.phase is BaselinePhase.IDLE and before.signal.factor == 1.0
    for idle in run.series(0, 58):
        assert (idle.confidence.arousal, idle.confidence.readiness) == (0.0, 0.0)
    at_start = run.at(60)
    assert at_start.phase is BaselinePhase.CAPTURING
    assert (at_start.confidence.arousal, at_start.confidence.readiness) == (0.0, 0.0)
    assert run.at(100).confidence.arousal > 0.8


def test_values_stay_neutral_until_the_baseline_result():
    run = run_session(ARC)
    for estimate in run.series(0, 64):
        assert (estimate.psv.arousal, estimate.psv.readiness) == (0.5, 0.5)


@pytest.mark.parametrize(
    ("spec", "faults", "seed", "settled"),
    [
        (SMALL_RISE, (), 1, True),
        ("68:120", (), 1, True),
        # Still settling: the bars climb, then come down as the slope shows it, with no drop at
        # the result.
        (STEEP_BASELINE, (), 1, False),
        (GENTLE_BASELINE, (), 1, False),
        # The gate passes with no rmssd_base: the RMSSD part must not vanish in one read.
        ("68:120", ("missed_beat@42", "missed_beat@50", "missed_beat@58"), 5, True),
    ],
)
def test_confidence_climbs_through_baseline_and_meets_the_result_without_a_step(
    spec, faults, seed, settled
):
    run = run_session(spec, *faults, seed=seed)
    series = run.series(20, 100)
    assert any(e.phase is BaselinePhase.READY for e in series)
    arousal = [e.confidence.arousal for e in series]
    if settled:
        assert all(b >= a - 0.1 for a, b in zip(arousal, arousal[1:], strict=False)), arousal
    for before, after in zip(series, series[1:], strict=False):
        # A slope over part of the window moves a gentle baseline's bars by up to about 0.17.
        assert abs(after.confidence.arousal - before.confidence.arousal) <= 0.2, arousal
        assert abs(after.confidence.readiness - before.confidence.readiness) <= 0.15
        if after.confidence.arousal > 0.05:  # a value nothing trusts may move; it acts on nothing
            assert abs(after.psv.arousal - before.psv.arousal) <= 0.08


def test_a_baseline_still_falling_steeply_gives_no_confidence_to_what_is_measured_against_it():
    run = run_session(STEEP_BASELINE)
    assert run.model.baseline.baseline_quality == 0.0
    after = [e for e in run.series(66, 260) if e.phase is BaselinePhase.READY]
    assert after and all(e.confidence.arousal <= 0.05 for e in after)
    assert all(e.confidence.readiness <= 0.05 for e in after)


def test_a_failed_gate_gives_no_confidence():
    run = run_session("68:120", "disconnect@30:12")
    assert run.model.baseline is not None and not run.model.baseline.passed
    late = run.series(80, 120)
    assert all(e.phase is BaselinePhase.FAILED for e in late)
    assert all((e.confidence.arousal, e.confidence.readiness) == (0.0, 0.0) for e in late)


def test_a_degraded_baseline_is_final_for_the_session():
    model = PsvModel()
    for now, result in packets(ARC, "disconnect@60:25"):
        if now >= 20_000 and model.estimate(now).phase is BaselinePhase.IDLE:
            model.start_baseline(20_000)
        if now >= 77_000 and model.estimate(now).phase is not BaselinePhase.DEGRADED:
            assert model.baseline is None  # no result 12 s after the window closed
            assert model.mark_baseline_degraded()
        model.on_packet(now, result)
        estimate = model.estimate(now)
        if now >= 77_000:
            assert estimate.phase is BaselinePhase.DEGRADED and model.baseline is None
            assert (estimate.confidence.arousal, estimate.confidence.readiness) == (0.0, 0.0)
            assert (estimate.psv.arousal, estimate.psv.readiness) == (0.5, 0.5)
    assert not model.mark_baseline_degraded() or model.estimate(now).phase is BaselinePhase.DEGRADED


def test_without_rmssd_base_arousal_rests_on_heart_rate_at_reduced_confidence():
    run = run_session("68:120", "missed_beat@22", "missed_beat@32", "missed_beat@42", seed=2,
                      baseline_at_s=0)  # fmt: skip
    assert run.model.baseline.passed and run.model.baseline.rmssd_base_ms is None
    ready = [e for e in run.series(55, 120) if e.phase is BaselinePhase.READY]
    assert ready and all(e.confidence.arousal <= 0.8 + 1e-9 for e in ready)
    assert max(e.confidence.arousal for e in ready) > 0.7
    assert all(e.confidence.readiness == 0.0 for e in ready)  # no rise, and no RMSSD part


def test_a_new_baseline_forgets_the_session_but_not_the_armband():
    run = run_session(ARC)
    model = run.model
    assert model.start_baseline(262_000)
    estimate = model.estimate(262_000)
    assert estimate.phase is BaselinePhase.CAPTURING and model.baseline is None
    assert estimate.confidence.arousal == 0.0 and estimate.signal.factor > 0.9


# --- arousal ---


def test_arousal_follows_the_task_and_comes_back_down_without_saturating():
    run = run_session(ARC)
    assert 0.52 < run.at(80).psv.arousal < 0.8
    assert run.at(LOAD_END).psv.arousal > 0.9
    assert run.at(REGULATE_END).psv.arousal <= run.at(LOAD_END).psv.arousal - 0.15
    assert run.at(LOAD_END).confidence.arousal > 0.95


@pytest.mark.parametrize(("spec", "most", "fall"), [(TINY_RISE, 0.8, 0.1), (SMALL_RISE, 0.9, 0.15)])
def test_a_small_rise_reads_as_a_small_rise(spec, most, fall):
    run = run_session(spec)
    load = run.series(80, LOAD_END)
    assert max(e.psv.arousal for e in load) < most
    assert run.at(REGULATE_END).psv.arousal <= run.at(LOAD_END).psv.arousal - fall


def test_the_rmssd_term_does_not_count_heart_rate_twice():
    """On the synthetic armband all of RMSSD's change is heart rate, so arousal must match
    heart rate alone, give or take the noise of a 30 s baseline."""
    run = run_session(ARC)
    gaps = []
    for estimate in run.series(80, 260):
        z_hr = dict(estimate.components)["z_hr"]
        hr_only = 0.5 + 0.5 * math.tanh(psv.AROUSAL_HR_WEIGHT * z_hr / psv.AROUSAL_Z_SCALE)
        gaps.append(abs(estimate.psv.arousal - hr_only))
    assert sum(gaps) / len(gaps) < 0.03 and max(gaps) < 0.08


# --- readiness and cognitive load ---


def test_readiness_holds_through_load_and_rises_as_the_rise_comes_back():
    run = run_session(ARC)
    load = [e.psv.readiness for e in run.series(80, LOAD_END)]
    assert all(0.3 < r < 0.65 for r in load)
    assert run.at(REGULATE_END).psv.readiness > run.at(LOAD_END).psv.readiness + 0.15
    assert all(e.confidence.readiness <= psv.READINESS_CAP for e in run.estimates.values())


def test_cognitive_load_has_no_confidence_without_task_events():
    run = run_session(ARC)
    assert all(e.psv.cognitive_load == 0.5 for e in run.estimates.values())
    assert all(e.confidence.cognitive_load == 0.0 for e in run.estimates.values())


def test_task_events_come_first_and_heart_rate_never_adds_confidence_alone():
    assert blend_cognitive_load(None, 0.9, 1.0) == (0.5, 0.0)
    value, confidence = blend_cognitive_load(TaskLoad(0.9, 0.5), 0.7, 1.0)
    assert value == pytest.approx(0.86) and confidence == pytest.approx(0.5)
    assert blend_cognitive_load(TaskLoad(0.9, 0.0), 0.7, 1.0)[1] == 0.0
    for junk in [j for j in GARBAGE if psv._num(j, 0.0, 1.0) is None]:
        assert blend_cognitive_load(TaskLoad(junk, 0.5), 0.7, 1.0) == (0.5, 0.0)
        assert blend_cognitive_load(TaskLoad(0.5, junk), 0.7, 1.0) == (0.5, 0.0)


def test_task_events_are_kept_only_when_they_make_sense():
    model = PsvModel()
    assert not model.add_task_event(999, "miss", 0.4, 480, 3100)  # no open split
    assert model.add_task_event(1000, "split", 0.4, 480, 3100)
    assert model.add_task_event(2000, "miss", 0.4, 480, 3100)
    assert not model.add_task_event(2001, "lock", 0.4, 480, 3100)  # already resolved
    assert not model.add_task_event(1000, "wobble", 0.4)
    assert not model.add_task_event(1000, "miss", 1.5)
    assert dict(model.estimate(2001).components)["task_events"] == 2


def add_task_rounds(
    model: PsvModel,
    outcomes: list[str],
    *,
    start_ms: float = 1_000,
    interval_ms: float = 3_000,
    difficulty: float = 0.7,
    dwell_ms: float = 600,
) -> float:
    """Contract-order rounds. A timeout miss lands immediately before the next split."""
    now = start_ms
    for outcome in outcomes:
        assert model.add_task_event(now, "split", difficulty, dwell_ms, interval_ms)
        if outcome == "lock":
            assert model.add_task_event(
                now + 0.4 * interval_ms, "lock", difficulty, dwell_ms, interval_ms
            )
        elif outcome == "abandon_lock":
            assert model.add_task_event(
                now + 0.2 * interval_ms, "abandon", difficulty, dwell_ms, interval_ms
            )
            assert model.add_task_event(
                now + 0.7 * interval_ms, "lock", difficulty, dwell_ms, interval_ms
            )
        elif outcome == "early_miss":
            assert model.add_task_event(
                now + 0.5 * interval_ms, "miss", difficulty, dwell_ms, interval_ms
            )
        elif outcome == "chasing_miss":
            assert model.add_task_event(
                now + 0.3 * interval_ms, "abandon", difficulty, dwell_ms, interval_ms
            )
            assert model.add_task_event(
                now + interval_ms, "miss", difficulty, dwell_ms, interval_ms
            )
        elif outcome == "timeout":
            assert model.add_task_event(
                now + interval_ms, "miss", difficulty, dwell_ms, interval_ms
            )
        else:
            raise AssertionError(f"unknown outcome {outcome}")
        now += interval_ms
    return now


def test_a_miss_is_load_only_when_the_event_stream_shows_active_pursuit():
    chasing, disengaged = PsvModel(), PsvModel()
    chasing_now = add_task_rounds(chasing, ["chasing_miss"] * 4)
    disengaged_now = add_task_rounds(disengaged, ["timeout"] * 4)
    chasing_load = chasing._task_load(chasing_now)
    disengaged_load = disengaged._task_load(disengaged_now)
    assert chasing_load is not None and disengaged_load is not None
    assert chasing_load.value == pytest.approx(0.72)
    assert chasing_load.engagement == pytest.approx(0.8) and chasing_load.strain == 1.0
    assert disengaged_load.value == pytest.approx(psv.TASK_DISENGAGED_VALUE)
    assert disengaged_load.engagement == 0.0 and disengaged_load.strain == 0.0
    assert chasing_load.confidence == disengaged_load.confidence == 0.5


def test_an_early_wrong_target_miss_is_engagement_without_an_abandon():
    early, timeout = PsvModel(), PsvModel()
    early_now = add_task_rounds(early, ["early_miss"] * 3)
    timeout_now = add_task_rounds(timeout, ["timeout"] * 3)
    assert early._task_load(early_now).value > 0.8
    assert timeout._task_load(timeout_now).value == psv.TASK_DISENGAGED_VALUE


def test_a_deadline_miss_preserves_when_the_last_real_pursuit_happened():
    early, late = PsvModel(), PsvModel()
    for model, abandon_at in ((early, 1_100), (late, 5_900)):
        assert model.add_task_event(1_000, "split", 0.7, 900, 5_000)
        assert model.add_task_event(abandon_at, "abandon", 0.7, 900, 5_000)
        assert model.add_task_event(6_000, "miss", 0.7, 900, 5_000)

    early_load, late_load = early._task_load(6_000), late._task_load(6_000)
    assert early_load is not None and late_load is not None
    assert early_load.interaction_gap_ms == 4_900
    assert late_load.interaction_gap_ms == 100
    assert early_load.engagement < late_load.engagement
    assert early_load.value < late_load.value


def test_task_confidence_starts_at_zero_then_rises_by_distinct_opportunities():
    model = PsvModel()
    assert model._task_load(0) is None
    assert model.estimate(0).confidence.cognitive_load == 0.0
    confidences = []
    now = 1_000.0
    for _ in range(psv.TASK_FULL_OPPORTUNITIES):
        now = add_task_rounds(model, ["lock"], start_ms=now, difficulty=0.8)
        confidences.append(model._task_load(now - 1_800).confidence)
    assert confidences[0] == pytest.approx(1 / psv.TASK_FULL_OPPORTUNITIES)
    assert confidences[-1] == 1.0
    assert all(after > before for before, after in zip(confidences, confidences[1:], strict=False))


def test_interaction_silence_means_disengagement_but_stream_silence_removes_confidence():
    model = PsvModel()
    now = add_task_rounds(model, ["lock"] * 4 + ["timeout"] * 4, difficulty=0.8)
    running = model._task_load(now)
    assert running is not None
    assert running.stream_freshness == 1.0 and running.confidence == 1.0
    assert running.engagement == 0.0 and running.value == psv.TASK_DISENGAGED_VALUE
    silent = model._task_load(now + 4 * 3_000)
    assert silent is not None
    assert silent.stream_freshness == 0.0 and silent.confidence == 0.0


def test_task_mapping_does_not_depend_on_mouse_or_head_dwell_distribution():
    mouse, head = PsvModel(), PsvModel()
    outcomes = ["lock", "abandon_lock", "early_miss", "lock"]
    mouse_now = add_task_rounds(mouse, outcomes, dwell_ms=420)
    head_now = add_task_rounds(head, outcomes, dwell_ms=1_200)
    assert mouse_now == head_now
    assert mouse._task_load(mouse_now) == head._task_load(head_now)


def test_abandon_frequency_cannot_evict_splits_or_inflate_confidence():
    model = PsvModel()
    now = add_task_rounds(model, ["lock"] * psv.TASK_FULL_OPPORTUNITIES)
    assert model._task_load(now - 1_800).confidence == 1.0

    assert model.add_task_event(now, "split", 0.8, 700, 3_000)
    for offset in range(600):
        assert model.add_task_event(now + offset + 1, "abandon", 0.8, 700, 3_000)
    flooded = model._task_load(now + 600)
    assert flooded is not None
    assert flooded.opportunities == psv.TASK_FULL_OPPORTUNITIES + 1
    assert flooded.confidence == 1.0


def test_orphan_and_duplicate_interactions_refresh_nothing():
    orphan = PsvModel()
    assert not orphan.add_task_event(1_000, "abandon", 0.8, 700, 3_000)
    assert not orphan.add_task_event(1_001, "lock", 0.8, 700, 3_000)
    assert orphan._task_load(1_001) is None

    model = PsvModel()
    now = add_task_rounds(model, ["lock"] * psv.TASK_FULL_OPPORTUNITIES)
    check_at = now + 9_000
    before = model._task_load(check_at)
    assert not model.add_task_event(check_at, "lock", 0.8, 700, 3_000)
    assert not model.add_task_event(check_at, "abandon", 0.8, 700, 3_000)
    assert model._task_load(check_at) == before


def test_relative_event_timing_not_raw_split_interval_classifies_engagement():
    fast, slow = PsvModel(), PsvModel()
    fast_now = add_task_rounds(fast, ["early_miss"] * 4, interval_ms=2_000)
    slow_now = add_task_rounds(slow, ["early_miss"] * 4, interval_ms=5_000)
    fast_load, slow_load = fast._task_load(fast_now), slow._task_load(slow_now)
    assert fast_load.value == slow_load.value
    assert fast_load.confidence == slow_load.confidence
    assert fast_load.engagement == slow_load.engagement == 1.0


def test_task_events_are_session_scoped_and_arrival_order_cannot_go_backwards():
    model = PsvModel()
    assert model.add_task_event(2_000, "split", 0.2, 900, 5_000)
    assert not model.add_task_event(1_999, "lock", 0.2, 900, 5_000)
    assert model._task_load(2_000) is not None
    model.reset()
    assert model._task_load(2_000) is None
    assert model.estimate(2_000).confidence.cognitive_load == 0.0


def test_task_diagnostics_are_in_the_psv_log_components():
    model = PsvModel()
    now = add_task_rounds(model, ["abandon_lock"] * 2)
    components = dict(model.estimate(now).components)
    for name in (
        "task_load",
        "task_load_confidence",
        "task_engagement",
        "task_difficulty",
        "task_strain",
        "task_stream_freshness",
        "task_opportunities",
        "task_interaction_gap_ms",
    ):
        assert components[name] is not None


# --- the signal ---


def test_one_lost_packet_barely_moves_confidence():
    clean, lost = run_session(ARC), run_session(ARC, "dropped_packet@100", "dropped_packet@170")
    for t, estimate in lost.estimates.items():
        assert abs(estimate.confidence.arousal - clean.estimates[t].confidence.arousal) <= 0.1, t


@pytest.mark.parametrize("seconds", [10, 20])
def test_a_disconnect_drops_confidence_holds_the_values_and_recovers(seconds):
    clean = run_session(ARC, tick_ms=1000)
    cut = run_session(ARC, f"disconnect@180:{seconds}", tick_ms=1000)
    before = cut.at(179)
    for estimate in cut.series(186, 180 + seconds):
        assert estimate.confidence.arousal == 0.0 and estimate.confidence.readiness == 0.0
    reconnect = next(
        t for t, e in cut.estimates.items() if t > 181_000 and e.signal.silence_ms < 1000
    )
    # The ramp back takes 15 s from where the silence left it, which was 0.
    ramp = [cut.estimates[reconnect + k * 1000].signal.recovery_factor for k in range(17)]
    assert ramp[1] < 0.15 and 0.2 < ramp[5] < 0.5 and ramp[16] == 1.0
    assert all(b >= a for a, b in zip(ramp, ramp[1:], strict=False))
    # Heart rate is held, not dropped to neutral, until the window has enough beats again.
    for estimate in cut.series(170, 230):
        assert_sound(estimate)
        assert estimate.confidence.arousal <= estimate.signal.recovery_factor + 1e-9
    held = dict(before.components)["hr_now_bpm"]
    for k in range(20):
        estimate = cut.estimates[reconnect + k * 1000]
        now_bpm = dict(estimate.components)["hr_now_bpm"]
        assert now_bpm is not None
        if now_bpm != held:
            break
        assert estimate.psv.arousal == pytest.approx(before.psv.arousal, abs=0.02)
    else:
        pytest.fail("heart rate never updated after the reconnect")
    for t in (210 + seconds, 216 + seconds):
        assert cut.at(t).confidence.arousal >= clean.at(t).confidence.arousal - 0.1


def test_a_short_gap_ramps_back_over_twice_its_length():
    cut = run_session(ARC, "disconnect@180:4", tick_ms=1000)
    reconnect = next(
        t for t, e in cut.estimates.items() if t > 181_000 and e.signal.silence_ms < 1000
    )
    assert cut.estimates[reconnect].signal.recovery_factor < 0.3
    assert cut.estimates[reconnect + 11_000].signal.recovery_factor == 1.0


def test_a_lost_packet_during_a_recovery_never_raises_it():
    plain = run_session(ARC, "disconnect@150:10", tick_ms=500)
    lost = run_session(ARC, "disconnect@150:10", "dropped_packet@162", tick_ms=500)
    for t, estimate in lost.estimates.items():
        assert estimate.signal.recovery_factor <= plain.estimates[t].signal.recovery_factor + 1e-9


def test_losing_contact_drops_confidence_gradually_and_ramps_it_back():
    clean = run_session(ARC, tick_ms=1000)
    lost = run_session(ARC, "contact_lost@230:5", tick_ms=1000)
    falling = [e for e in lost.series(230, 236) if e.signal.contact is False]
    assert falling and 0.2 < falling[0].signal.contact_factor < 1.0  # falls over 2 s, not at once
    assert min(e.confidence.arousal for e in falling) <= 0.05
    back = [e for e in lost.series(235, 260) if e.signal.contact is True]
    assert 0.0 < back[0].signal.contact_factor < 0.3
    assert all(e.signal.contact_factor < 1.0 for e in back[:6])  # ramps back over about 10 s
    assert back[12].signal.contact_factor == 1.0
    assert all(e.signal.accepted_fraction == 1.0 for e in lost.series(230, 260))
    assert lost.at(256).confidence.arousal >= clean.at(256).confidence.arousal - 0.1


def test_a_sensor_that_never_reports_contact_is_read_as_in_contact(monkeypatch):
    supported = run_session(ARC)
    plain = synthetic.encode_hrm
    monkeypatch.setattr(
        synthetic,
        "encode_hrm",
        lambda hr, rr=(), **kw: plain(hr, rr, **{**kw, "contact_supported": False}),
    )
    unsupported = run_session(ARC)
    for t, estimate in unsupported.estimates.items():
        assert estimate.signal.contact is None
        assert estimate.confidence == supported.estimates[t].confidence, t


def test_contact_flapping_never_raises_confidence_when_contact_goes():
    flap = run_session(ARC, "contact_lost@160:3", "contact_lost@164.5:1.5", tick_ms=250)
    series = flap.series(158, 190)
    for before, after in zip(series, series[1:], strict=False):
        if after.signal.contact is False:
            # At most what the ramp gained in the 250 ms before contact went.
            assert after.signal.contact_factor <= before.signal.contact_factor + 0.02


def test_a_long_contact_loss_is_answered_by_the_contact_factor_alone():
    clean, lost = run_session(ARC), run_session(ARC, "contact_lost@140:20")
    assert lost.at(150).confidence.arousal == 0.0
    for t in (180, 190, 200):
        assert lost.at(t).signal.accepted_fraction == clean.at(t).signal.accepted_fraction
        assert lost.at(t).confidence.arousal >= clean.at(t).confidence.arousal - 0.1


def test_a_long_artefact_burst_lowers_confidence_through_the_accepted_share():
    burst = run_session(ARC, "artefact_burst@150:20")
    for t in (190, 200):
        estimate = burst.at(t)
        assert estimate.signal.accepted_fraction < 0.8 and estimate.signal.accepted_factor < 0.7
        assert estimate.confidence.arousal < 0.7


def test_an_artefact_burst_lowers_confidence_without_collapsing_it():
    clean, burst = run_session(ARC), run_session(ARC, "artefact_burst@170")
    during = burst.series(170, 200)
    assert min(e.confidence.arousal for e in during) < min(
        e.confidence.arousal for e in clean.series(170, 200)
    )
    assert min(e.confidence.arousal for e in during) > 0.5
    for before, after in zip(during, during[1:], strict=False):
        assert abs(after.confidence.arousal - before.confidence.arousal) <= 0.2


def test_hr_bpm_is_never_null_after_the_first_accepted_interval():
    run = run_session(ARC, "disconnect@100:10", "contact_lost@150:5")
    seen = False
    for estimate in run.estimates.values():
        seen = seen or estimate.hr_bpm is not None
        assert estimate.hr_bpm is not None or not seen
    model = PsvModel()
    now, result = packets("68:10")[0]
    assert any(i.accepted and i.bootstrap for i in result.intervals)
    model.on_packet(now, result)
    assert model.estimate(now).hr_bpm is not None


# --- reading ---


def test_reading_more_often_or_at_other_moments_changes_nothing():
    plain = run_session(ARC, "disconnect@180:10")
    busy = run_session(ARC, "disconnect@180:10", phase_ms=1300, tick_ms=700,
                       extra_reads_s=tuple(t / 1000 for t in plain.estimates))  # fmt: skip
    for t, estimate in plain.estimates.items():
        assert busy.estimates[t] == estimate, t


def test_the_same_moment_read_twice_is_the_same_estimate():
    model, now = ready_model()
    assert model.estimate(now + 500) == model.estimate(now + 500)


# --- more of the contract with session.py ---


def test_reset_forgets_the_visitor_but_not_the_armband():
    model = run_session(ARC).model
    model.reset()
    estimate = model.estimate(262_000)
    assert estimate.phase is BaselinePhase.IDLE and model.baseline is None
    assert (estimate.confidence.arousal, estimate.confidence.readiness) == (0.0, 0.0)
    assert (estimate.psv.arousal, estimate.psv.readiness) == (0.5, 0.5)
    assert estimate.signal.factor > 0.9


@pytest.mark.parametrize(("spec", "faults"), [(ARC, ()), ("68:120", ("disconnect@30:12",))])
def test_a_finished_baseline_cannot_be_marked_degraded(spec, faults):
    run = run_session(spec, *faults)
    before = run.model.estimate(run.model._last_arrival)
    assert before.phase in (BaselinePhase.READY, BaselinePhase.FAILED)
    assert not run.model.mark_baseline_degraded()
    assert run.model.estimate(run.model._last_arrival) == before


def test_heart_rate_over_a_window_is_the_true_rate_from_the_beats_hr_base_trusts():
    beats = list(synthetic.true_beats(Profile.from_spec(ARC), random.Random("1:beats")))
    model = PsvModel()
    for now, result in packets(ARC):
        model.on_packet(now, result)
        if now >= 130_000:
            break
    window = model.heart_rate(95_000, 125_000)
    assert window.bpm == pytest.approx(true_hr(beats, 95, 125), abs=0.3)
    assert 29_000 <= window.covered_ms <= 30_000 and window.intervals > 30
    # Only what has arrived: a window reaching past the newest beat covers less.
    assert model.heart_rate(110_000, 140_000).covered_ms < 21_000  # beats up to about 130 s
    for start, end in ((125_000, 95_000), (math.nan, 1.0), (None, 1.0), (0.0, "1")):
        assert model.heart_rate(start, end) == psv.HeartRateWindow(None, 0.0, 0)
    assert model.heart_rate(500_000, 530_000).bpm is None


def test_heart_rate_takes_a_beat_at_the_window_end_but_not_at_its_start():
    model = PsvModel()
    for now, result in packets("68:60"):
        model.on_packet(now, result)
    beat = model._trusted[20][0]
    ending = model.heart_rate(beat - 5_000, beat)
    starting = model.heart_rate(beat, beat + 5_000)
    assert (
        ending.intervals + starting.intervals
        == model.heart_rate(beat - 5_000, beat + 5_000).intervals
    )
    assert model.heart_rate(beat - 1, beat).intervals == 1
    assert model.heart_rate(beat, beat + 1).intervals == 0
    assert model.last_trusted_beat_ms == model._trusted[-1][0]
    assert PsvModel().last_trusted_beat_ms is None


def test_rmssd_is_known_the_moment_classification_reaches_the_window_end():
    model = PsvModel()
    for now, result in packets(ARC):
        model.on_packet(now, result)
        if now >= 100_000:
            break
    horizon = model._cleaner.horizon_ms
    assert model.rmssd(horizon - 30_000, horizon) is not None
    assert model.rmssd(horizon - 30_000, horizon + 0.001) is None


def test_heart_rate_leaves_out_beats_sent_without_contact():
    model = PsvModel()
    for now, result in packets("68:60", "contact_lost@20:20"):
        model.on_packet(now, result)
    assert model.heart_rate(22_000, 38_000).intervals == 0
    assert model.heart_rate(42_000, 58_000).intervals > 10


def test_rmssd_over_a_window_waits_for_classification_to_pass_its_end():
    model, asked = PsvModel(), []
    for now, result in packets(ARC):
        model.on_packet(now, result)
        asked.append((now, model.rmssd(60_000, 90_000)))
        if now >= 100_000:
            break
    horizon = model._cleaner.horizon_ms
    assert all(r is None for now, r in asked if now < 90_000 + 3_000)
    reading = model.rmssd(60_000, 90_000)
    assert reading is not None and reading.differences >= 20 and reading.rmssd_ms > 0
    assert model.rmssd(horizon - 30_000, horizon + 1) is None
    assert model.rmssd(math.nan, 90_000) is None


def test_closing_a_baseline_with_no_result_judges_the_gate_on_what_was_captured():
    # Window 20 to 65 s. The armband dies 20 s in: nothing more is coming, and the gate fails.
    model = run_session("68:40").model
    assert model.estimate(77_000).phase is BaselinePhase.AWAITING and model.baseline is None
    assert model.close_baseline(77_000) is BaselinePhase.FAILED
    assert not model.baseline.passed and any("clean data" in p for p in model.baseline.problems)
    assert model.estimate(77_000).confidence.arousal == 0.0
    # It dies 2 s before the window closes: what was captured passes, only hr_base is missing.
    model = run_session("68:63").model
    assert model.close_baseline(77_000) is BaselinePhase.DEGRADED and model.baseline is None
    assert model.estimate(77_000).phase is BaselinePhase.DEGRADED


def test_closing_a_baseline_leaves_an_open_window_or_a_result_in_by_then_alone():
    model = PsvModel()
    assert model.close_baseline(10_000) is BaselinePhase.IDLE
    model.start_baseline(0)
    for as_of in (10_000, 44_999, math.nan, None, "77000"):
        assert model.close_baseline(as_of) is BaselinePhase.CAPTURING
    assert model.baseline_decided_ms is None
    for spec, faults in ((ARC, ()), ("68:120", ("disconnect@30:12",))):
        run = run_session(spec, *faults)
        before, decided = run.model.estimate(262_000), run.model.baseline_decided_ms
        assert 65_000 < decided < 77_000  # the arrival of the packet that completed it
        assert run.model.close_baseline(decided) is before.phase
        assert run.model.estimate(262_000) == before and run.model.baseline_decided_ms == decided


def test_a_result_that_came_after_the_hold_ended_is_set_aside():
    # Ready at about 71 s; the hold is taken to have ended at 67 s, before that.
    run = run_session(ARC)
    assert run.model.estimate(262_000).phase is BaselinePhase.READY
    assert run.model.baseline_decided_ms > 67_000
    assert run.model.close_baseline(67_000) is BaselinePhase.DEGRADED
    assert run.model.baseline is None and run.model.baseline_decided_ms is None
    assert run.model.estimate(262_000).confidence.arousal == 0.0
    # Lost from 45 to 70 s: by 67 s too little had been classified, so the gate fails as of then.
    run = run_session("68:120", "disconnect@45:25")
    late = run.model.baseline_decided_ms
    assert late > 67_000 and run.model.close_baseline(67_000) is BaselinePhase.FAILED
    assert run.model.baseline_decided_ms == 67_000 and not run.model.baseline.passed
    assert run.model.close_baseline(90_000) is BaselinePhase.FAILED  # decided by then: it stands


def test_the_person_s_own_spread_sets_the_heart_rate_unit_inside_its_bounds():
    model = run_session(ARC).model
    for spread, unit in ((1.0, 4.0), (5.0, 5.0), (9.0, 6.0), (None, 4.0)):
        model._baseline = dataclasses.replace(model._baseline, hr_sd_bpm=spread)
        assert dict(model.estimate(262_000).components)["hr_unit_bpm"] == unit


def test_baseline_confidence_reaches_its_published_ceiling_by_the_end_of_the_45_s():
    """docs/vr-handoff.md divides the mean of these three by 0.393 to drive the baseline world."""
    assert round(psv.BASELINE_CONFIDENCE_MAX, 3) == 0.393
    run = run_session("68:180", baseline_at_s=70)
    reached = [
        (e.confidence.arousal + e.confidence.cognitive_load + e.confidence.readiness) / 3
        for e in run.series(70, 115)
    ]
    assert reached[0] == 0.0
    assert reached[-1] == pytest.approx(psv.BASELINE_CONFIDENCE_MAX, abs=0.01)
    assert max(reached) <= psv.BASELINE_CONFIDENCE_MAX + 1e-9


def test_an_interval_placed_after_its_packet_arrived_is_ignored():
    model, now = ready_model()
    real = packets(ARC)
    later = [(t, r) for t, r in real if t > now][:3]
    bogus = dataclasses.replace(later[0][1].intervals[0], t_beat=1e308)
    model.on_packet(later[0][0], PacketResult((bogus,), (), True))
    for t, result in later[1:]:
        model.on_packet(t, result)
    assert dict(model.estimate(later[-1][0]).components)["hr_fill"] > 0.9


# --- the RMSSD term's direction, which the synthetic armband alone never exercises ---


def variability_switch(before_ms, after_ms, at_s=100.0):
    """true_beats, with the beat-to-beat spread changing at at_s while the rate does not."""

    def beats(profile, rng):
        t = 0.0
        while True:
            bpm = profile.hr_at(t)
            spread = (before_ms if t < at_s else after_ms) * (60.0 / bpm) ** 2
            rr = max(300.0, 60_000.0 / bpm + rng.gauss(0.0, spread))
            t += rr / 1000.0
            if t > profile.duration_s:
                return
            yield Beat(t, rr)

    return beats


@pytest.mark.parametrize(
    ("before_ms", "after_ms", "more_aroused"), [(30, 10, True), (10, 30, False)]
)
def test_less_variability_at_the_same_rate_reads_as_more_arousal_and_less_readiness(
    monkeypatch, before_ms, after_ms, more_aroused
):
    monkeypatch.setattr(synthetic, "true_beats", variability_switch(before_ms, after_ms))
    estimate = run_session("68:240").at(200)
    components = dict(estimate.components)
    assert abs(components["z_hr"]) < 1.0  # the rate did not move
    if more_aroused:
        assert components["z_rmssd"] > 1.0
        assert estimate.psv.arousal > 0.55 and estimate.psv.readiness < 0.45
    else:
        assert components["z_rmssd"] < -1.0
        assert estimate.psv.arousal < 0.45 and estimate.psv.readiness > 0.55


# --- after the second review ---


def test_the_first_packet_starts_a_ramp_from_nothing():
    run = run_session("68:40", baseline_at_s=None, tick_ms=1000)
    ramp = [run.at(t).signal.recovery_factor for t in range(2, 20)]
    assert ramp[0] < 0.1 and ramp[-1] == 1.0
    assert all(b >= a for a, b in zip(ramp, ramp[1:], strict=False))


def test_a_reconnect_inside_a_recovery_never_lifts_the_signal_at_once():
    run = run_session(ARC, "disconnect@150:10", "disconnect@163:3.8", tick_ms=100)
    series = run.series(150, 200)
    for before, after in zip(series, series[1:], strict=False):
        assert after.signal.factor <= before.signal.factor + 0.02, after.t_ms


def test_a_contact_loss_before_a_disconnect_does_not_slow_the_recovery_further():
    both = run_session(ARC, "contact_lost@180:2.5", "disconnect@182:10", tick_ms=500)
    only = run_session(ARC, "disconnect@182:10", tick_ms=500)
    for t in (195, 198, 200, 203, 208):
        assert both.at(t).signal.factor == pytest.approx(only.at(t).signal.factor, abs=0.01)


def test_hr_bpm_follows_the_heart_after_a_burst_resets_the_scheduler_s_window():
    run = run_session(ARC, "artefact_burst@150:20", seed=6)
    profile = Profile.from_spec(ARC)
    for estimate in run.series(150, 200):
        assert abs(estimate.hr_bpm - profile.hr_at(estimate.t_ms / 1000)) < 20, estimate.t_ms


def test_losing_contact_in_baseline_fails_the_gate():
    run = run_session(ARC, "contact_lost@30:20")
    assert not run.model.baseline.passed
    assert run.at(100).phase is BaselinePhase.FAILED


def test_intervals_sent_without_contact_reach_neither_heart_rate_nor_hrv():
    run = run_session(ARC, "contact_lost@140:20", tick_ms=1000)
    before = run.at(139)
    during = run.series(155, 159)
    assert all(dict(e.components)["hr_fill"] == 0.0 for e in during)
    assert all(e.hr_bpm == run.at(145).hr_bpm for e in during)  # held, not fed
    # From 141 s: the cleaner is still classifying beats from before the loss until then.
    differences = [dict(e.components)["rmssd_differences"] for e in run.series(141, 159)]
    assert all(b <= a for a, b in zip(differences, differences[1:], strict=False))
    assert differences[-1] < dict(before.components)["rmssd_differences"]
