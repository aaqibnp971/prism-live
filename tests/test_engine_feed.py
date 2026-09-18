"""bridge/engine_feed.py: PSV, session gain, and heartbeat level (prompts 2.5-2.6).

The sink is a recording fake with the three calls the feed makes. A whole session comes from
tools/fixtures/synthetic-clean.jsonl, every state message the bridge sent, in order.
"""

import ast
import copy
import json
import math
import random
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import run_live

from bridge.engine_feed import (
    FADE_MS,
    GATED,
    HEARTBEAT_BASELINE_END_DBFS,
    HEARTBEAT_BASELINE_RAMP_MS,
    HEARTBEAT_BASELINE_START_DBFS,
    HEARTBEAT_CHAIN_LATENCY_MS,
    HEARTBEAT_FINAL_FADE_MS,
    HEARTBEAT_LEVEL_SMOOTH_MS,
    HEARTBEAT_LOAD_MAX_DBFS,
    HEARTBEAT_REGULATE_END_DBFS,
    HEARTBEAT_RESTART_RAMP_MS,
    LOOP_MS,
    MARGIN,
    HeartbeatLevel,
    PsvFeed,
    SessionGain,
    heartbeat_load_level_dbfs,
    session_gain_for,
)
from bridge.engine_mapping import DIMENSIONS, GATE_THRESHOLDS, density, map_effective
from bridge.logging import SessionLog
from bridge.phase import PULSE_ALIGNMENT_PHASE, SAMPLE_RATE, PhaseTracker, frames_from_ms
from bridge.poses import pose_inputs

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tools" / "fixtures" / "synthetic-clean.jsonl"
FIXTURE_PROFILE = "68:61,68-100:75,100-72:115,72:150"  # tools/record_fixture.py


class Frames:
    def __init__(self, value=0, block=512):
        self.value = value
        self.config = SimpleNamespace(max_block_frames=block)

    def frames_rendered(self):
        return self.value


def feed_and_frames(sink, log, source="body", *, session_gates=False):
    frames = Frames()
    return (
        PsvFeed(
            sink,
            log,
            source,
            phase=PhaseTracker.for_shim(frames),
            session_gates=session_gates,
        ),
        frames,
    )


class Recorder:
    def __init__(self):
        self.moods = []
        self.gains = []
        self.heartbeats = []

    def set_mood_override(self, arousal, cognitive_load, readiness):
        self.moods.append((arousal, cognitive_load, readiness))

    def set_session_gain(self, target, ramp_ms):
        self.gains.append((target, ramp_ms))

    def set_heartbeat_level(self, target_dbfs, ramp_ms):
        self.heartbeats.append((target_dbfs, ramp_ms))


def fixture_states():
    records = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]
    return [r["msg"] for r in records if r["dir"] == "out" and r["msg"]["type"] == "state"]


def open_log(tmp_path):
    log = SessionLog(tmp_path, clock=lambda: 0.0)
    log.start_session(date(2026, 9, 14))
    return log


def events(log, name):
    lines = log.path.read_text(encoding="utf-8").splitlines()
    return [r for r in map(json.loads, lines) if r.get("event") == name]


def message(
    t,
    arousal,
    load=0.5,
    readiness=0.5,
    segment="regulate",
    elapsed=0,
    nominal=75_000,
    hr_bpm=None,
    hr_base=None,
):
    """A state message whose body source sends exactly these values: authority 1 everywhere."""
    return {
        "type": "state",
        "t_engine": t,
        "segment": segment,
        "segment_elapsed_ms": elapsed,
        "segment_nominal_ms": nominal,
        "psv": {"arousal": arousal, "valence": 0.5, "cognitive_load": load, "readiness": readiness},
        "confidence": {"arousal": 1.0, "valence": 0.0, "cognitive_load": 1.0, "readiness": 1.0},
        "authority": {"arousal": 1.0, "valence": 0.0, "cognitive_load": 1.0, "readiness": 1.0},
        "hr_bpm": hr_bpm,
        "hr_base": hr_base,
    }


def at_density(t, d, load=0.5):
    """A message whose inputs have density d, by arousal."""
    return message(t, 0.5 + (d - 0.5 + (load - 0.5)) / 0.9, load)


class Checked:
    """A feed, and the invariants every update must keep."""

    def __init__(self, log, source="body"):
        self.sink = Recorder()
        self.feed, self.frames = feed_and_frames(self.sink, log, source)
        self.updates = []
        self.flips = {stem: [] for stem in GATED}

    def send(self, msg):
        self.frames.value = frames_from_ms(msg["t_engine"])
        before = copy.deepcopy(msg)
        u = self.feed.on_state(msg)
        assert msg == before
        self.updates.append(u)
        sent, d = u.sent, u.params.density
        assert all(0.0 <= sent[k] <= 1.0 for k in DIMENSIONS)
        assert sent["readiness"] == u.chosen["readiness"]
        assert not u.gates["air"] or u.gates["pulse"]
        for stem in GATED:
            threshold = GATE_THRESHOLDS[stem]
            # Never inside the band, and on the side of the state.
            assert d >= threshold + MARGIN if u.gates[stem] else d <= threshold - MARGIN
            assert u.params.gates[stem] == u.gates[stem]
            if stem in u.flipped:
                self.flips[stem].append(u.t_engine)
        chosen_d = density(u.chosen["arousal"], u.chosen["cognitive_load"])
        lo, hi = band(u.gates)
        if lo <= chosen_d <= hi:
            assert sent == u.chosen
        return u


def band(gates):
    if not gates["pulse"]:
        return -1.0, 0.35 - MARGIN
    if not gates["air"]:
        return 0.35 + MARGIN, 0.55 - MARGIN
    return 0.55 + MARGIN, 2.0


# --- a whole session ---


@pytest.mark.parametrize("source", ["body", "pose"])
def test_one_override_per_state_message_through_a_whole_session(tmp_path, source):
    states = fixture_states()
    assert len(states) == 150
    log = open_log(tmp_path)
    run = Checked(log, source)
    for msg in states:
        run.send(msg)
    assert len(run.sink.moods) == len(states)
    assert all(len(call) == 3 for call in run.sink.moods)
    logged = events(log, "engine_psv")
    assert len(logged) == len(states)
    for msg, record, call in zip(states, logged, run.sink.moods, strict=True):
        assert record["t_engine"] == msg["t_engine"]
        assert record["segment"] == msg["segment"]
        assert record["source"] == source
        sent = record["sent"]
        assert (sent["valence"], sent["confidence"], sent["mode_hint"]) == (0.5, 1.0, None)
        assert (sent["arousal"], sent["cognitive_load"], sent["readiness"]) == call
        assert set(record) >= {"body", "input", "hysteresis", "mapping"}
        assert set(record["mapping"]) >= {"cutoff_hz", "density", "gates", "gains"}
        expected = map_effective(*call)
        assert record["mapping"]["cutoff_hz"] == round(expected.cutoff_hz, 1)
        assert record["mapping"]["gates"] == expected.gates


def test_the_body_source_sends_the_psv_pre_blended_with_the_messages_authority(tmp_path):
    run = Checked(open_log(tmp_path), "body")
    for msg in fixture_states():
        u = run.send(msg)
        wanted = {d: 0.5 + (msg["psv"][d] - 0.5) * msg["authority"][d] for d in DIMENSIONS}
        assert u.body == wanted and u.chosen == wanted
        if msg["segment"] in ("idle", "baseline", "reset"):
            # Authority 0: a neutral PSV, 2,282 Hz with pulse open. Not silence.
            assert u.sent == dict.fromkeys(DIMENSIONS, 0.5)
            assert round(u.params.cutoff_hz) == 2282 and u.gates == {"pulse": True, "air": False}
        if msg["segment"] == "load":
            assert 0.4 <= u.sent["arousal"] <= 0.6  # load's 0.2 ceiling
        if not u.moved:
            assert u.sent == wanted
    moved = [u for u in run.updates if u.moved]
    # The fixture's load holds air closed at the band edge while arousal climbs through it.
    assert moved and all(round(u.params.density, 12) in (0.54, 0.56) for u in moved)


def test_the_pose_source_sends_each_segments_pose(tmp_path):
    run = Checked(open_log(tmp_path), "pose")
    previous = None
    for msg in fixture_states():
        u = run.send(msg)
        segment, first = msg["segment"], msg["segment"] != previous
        previous = segment
        assert u.chosen == pose_inputs(msg)
        assert u.body == {d: 0.5 + (msg["psv"][d] - 0.5) * msg["authority"][d] for d in DIMENSIONS}
        if segment in ("idle", "baseline", "reset"):
            assert u.sent == {"arousal": 0.486, "cognitive_load": 0.65, "readiness": 0.5}
            assert u.gates == {"pulse": False, "air": False}
        elif segment == "load":
            level = min(1.0, max(0.0, (msg["psv"]["arousal"] - 0.5) / 0.35))
            level *= msg["confidence"]["arousal"]
            assert u.chosen["arousal"] == pytest.approx(0.486 + 0.285 * level)
            assert (u.sent["cognitive_load"], u.sent["readiness"]) == (0.65, 0.5)
        elif segment == "regulate":
            assert (u.sent["cognitive_load"], u.sent["readiness"]) == (0.948, 0.11)
            # Front-loaded: both gates close on regulate's first message.
            assert u.gates == {"pulse": False, "air": False}
            if first:
                assert set(u.flipped) == {"pulse", "air"}
        elif segment == "resolve":
            elapsed = msg["segment_elapsed_ms"]
            if elapsed <= 25_000:
                assert u.sent["arousal"] == 0.506
            if elapsed >= 33_000:
                assert u.sent["arousal"] == 0.618
            assert 619 < u.params.cutoff_hz < 901 and u.gates == {"pulse": False, "air": False}
    flips = [(u.t_engine, u.flipped) for u in run.updates if u.flipped]
    assert [f for _, f in flips] == [("pulse",), ("air",), ("pulse", "air")]


# --- authority and the message ---


def test_authority_is_read_from_the_message_never_recomputed(tmp_path):
    # An authority no ceiling allows in load: the feed sends it as it is.
    msg = message(0, arousal=0.9, load=0.1, readiness=0.3, segment="load")
    msg["authority"] = {"arousal": 0.7, "valence": 0.0, "cognitive_load": 0.3, "readiness": 0.9}
    sink = Recorder()
    feed, _ = feed_and_frames(sink, open_log(tmp_path))
    feed.on_state(msg)
    assert sink.moods == [(0.5 + 0.4 * 0.7, 0.5 - 0.4 * 0.3, 0.5 - 0.2 * 0.9)]


def test_the_feed_does_not_import_bridge_authority():
    for name in ("engine_feed", "engine_mapping", "poses"):
        tree = ast.parse((ROOT / "bridge" / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            else:
                continue
            assert not any(m.startswith(("bridge.authority", "bridge.state")) for m in modules)
    probe = (
        "import sys; import bridge.engine_feed; "
        "print(sorted(m for m in sys.modules if m in ('bridge.authority', 'bridge.state')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


def test_state_messages_are_never_modified(tmp_path):
    log = open_log(tmp_path)
    feed, _ = feed_and_frames(Recorder(), log)
    gain = SessionGain(Recorder(), log)
    states = fixture_states()
    pristine = copy.deepcopy(states)
    for k, msg in enumerate(states):
        if k == 60:
            feed.set_source("pose")
        feed.on_state(msg)
        gain.on_state(msg)
    assert states == pristine


def test_the_source_switch_is_logged_and_takes_effect_on_the_next_message(tmp_path):
    log = open_log(tmp_path)
    sink = Recorder()
    feed, _ = feed_and_frames(sink, log)
    states = fixture_states()
    load = [k for k, m in enumerate(states) if m["segment"] == "load"]
    switch_after = load[10]
    for msg in states[: switch_after + 1]:
        assert feed.on_state(msg).source == "body"
    calls = len(sink.moods)
    feed.set_source("pose", t_engine=states[switch_after]["t_engine"] + 500)
    assert len(sink.moods) == calls  # nothing is sent until the next message
    assert feed.source == "pose"
    switched = events(log, "engine_psv_source")
    assert len(switched) == 1
    assert switched[0]["source"] == "pose" and switched[0]["previous"] == "body"
    assert switched[0]["t_engine"] == states[switch_after]["t_engine"] + 500
    nxt = states[switch_after + 1]
    u = feed.on_state(nxt)
    assert u.source == "pose" and u.chosen == pose_inputs(nxt)
    assert events(log, "engine_psv")[-1]["source"] == "pose"
    with pytest.raises(ValueError):
        feed.set_source("heart")
    assert feed.source == "pose"
    feed.set_source("body")
    assert feed.on_state(states[switch_after + 2]).source == "body"
    with pytest.raises(ValueError):
        feed_and_frames(Recorder(), log, source="script")


def test_the_gate_state_outlives_a_source_switch(tmp_path):
    run = Checked(open_log(tmp_path))
    run.send(message(0, 0.5))  # neutral body schedules pulse to open at 11 s
    run.feed.set_source("pose")
    # The pose closes it before that boundary. The pending open cancels cleanly; the state still
    # outlives the source switch, but the old phase-blind 12.5 s hold is gone.
    u = run.send(message(2_000, 0.5, segment="baseline"))
    assert u.flipped == ("pulse",) and not u.gates["pulse"] and not u.moved
    u = run.send(message(12_500, 0.5, segment="baseline"))
    assert not u.flipped and u.sent == pose_inputs(message(0, 0.5, segment="baseline"))


# --- hysteresis ---


@pytest.mark.parametrize("stem", GATED)
@pytest.mark.parametrize("start_open", [False, True])
def test_jitter_inside_the_band_never_flips_a_gate(tmp_path, stem, start_open):
    threshold = GATE_THRESHOLDS[stem]
    run = Checked(open_log(tmp_path))
    # Settle the gate on one side, then let every hold run out.
    settle = threshold + 0.1 if start_open else threshold - 0.1
    if stem == "air" and not start_open:
        settle = 0.45  # pulse open, air closed
    for k in range(0, 40_000, 2_000):
        run.send(at_density(k, settle))
    assert run.updates[-1].gates[stem] == start_open
    rng = random.Random(f"{stem}{start_open}")
    t = 40_000
    for _ in range(2_000):
        t += rng.choice((100, 500, 2_000))
        d = threshold + rng.uniform(-0.999, 0.999) * MARGIN
        load = rng.choice((0.5, 0.3, 0.8))
        u = run.send(at_density(t, d, load))
        assert u.gates[stem] == start_open and not u.flipped
        edge = threshold + MARGIN if start_open else threshold - MARGIN
        assert u.params.density == pytest.approx(edge, abs=1e-12)


@pytest.mark.parametrize("stem", GATED)
def test_a_flip_can_reverse_before_its_boundary_but_holds_across_the_actual_ramp(tmp_path, stem):
    low, high = (0.2, 0.45) if stem == "pulse" else (0.45, 0.7)
    run = Checked(open_log(tmp_path))
    run.send(at_density(0, low))
    run.send(at_density(30_000, low))
    u = run.send(at_density(40_000, high))
    assert stem in u.flipped and u.gates[stem]
    start_frame, end_frame = u.ramps[stem]
    start_ms, end_ms = start_frame / 48, end_frame / 48
    assert start_ms == next(t for t in range(0, 100_000, LOOP_MS[stem]) if t > 40_000)
    assert end_ms == start_ms + FADE_MS

    # Before the boundary a reversal cancels cleanly, then another crossing can restore it.
    u = run.send(at_density(start_ms - 1_000, low))
    assert u.flipped == (stem,) and not u.gates[stem]
    u = run.send(at_density(start_ms - 500, high))
    assert u.flipped == (stem,) and u.gates[stem]

    # Once the boundary arrives, the state is held only through the 1.5 s audible ramp.
    for t in range(round(start_ms), round(end_ms), 100):
        u = run.send(at_density(t, low))
        assert u.gates[stem] and not u.flipped
        assert u.params.density == pytest.approx(GATE_THRESHOLDS[stem] + MARGIN, abs=1e-12)
    u = run.send(at_density(end_ms, low))
    assert u.flipped == (stem,) and not u.gates[stem] and not u.moved


def test_pulse_does_not_close_under_an_air_held_open(tmp_path):
    run = Checked(open_log(tmp_path))
    run.send(at_density(0, 0.2))
    u = run.send(at_density(20_000, 0.7))
    assert set(u.flipped) == {"pulse", "air"}
    # Air's actual ramp is 26 to 27.5 s. During it, pulse stays open under the audible air.
    u = run.send(at_density(26_500, 0.2))
    assert u.gates == {"pulse": True, "air": True} and u.deferred == ("pulse",)
    assert u.params.density == pytest.approx(0.56, abs=1e-12)
    u = run.send(at_density(27_499, 0.2))
    assert u.gates == {"pulse": True, "air": True}
    u = run.send(at_density(27_500, 0.2))
    assert set(u.flipped) == {"pulse", "air"} and u.gates == {"pulse": False, "air": False}


def test_air_does_not_open_over_a_pulse_held_closed(tmp_path):
    run = Checked(open_log(tmp_path))
    run.send(at_density(0, 0.45))
    u = run.send(at_density(20_000, 0.2))
    assert u.flipped == ("pulse",)
    # Pulse closes from 22 to 23.5 s, and air cannot open over it during that ramp.
    u = run.send(at_density(22_500, 0.9))
    assert u.gates == {"pulse": False, "air": False} and u.deferred == ("air",)
    assert u.params.density == pytest.approx(0.34, abs=1e-12)
    u = run.send(at_density(23_500, 0.9))
    assert set(u.flipped) == {"pulse", "air"} and u.gates == {"pulse": True, "air": True}


def test_load_moves_only_when_arousal_is_pinned(tmp_path):
    run = Checked(open_log(tmp_path), "pose")
    # Pulse opens early in load and air at 64 s. Its real ramp ends at 66.5 s, so the regulate
    # pose at 75 s is no longer held back by the old phase-blind 14.5 s timer.
    for k in range(0, 76_000, 2_000):
        u = run.send(message(k, 0.6 if k < 64_000 else 0.9, segment="load", elapsed=k))
        assert u.gates == {"pulse": True, "air": k >= 64_000}
    u = run.send(message(75_000, 0.9, segment="regulate", elapsed=0))
    assert set(u.flipped) == {"pulse", "air"} and u.gates == {"pulse": False, "air": False}
    assert u.chosen == {"arousal": 0.771, "cognitive_load": 0.948, "readiness": 0.11}
    assert u.sent == u.chosen and u.params.density < 0.34
    # During pulse's actual close ramp, an attempted air open is pinned below both gates.
    run = Checked(open_log(tmp_path))
    run.send(at_density(0, 0.45))
    assert run.send(at_density(20_000, 0.2)).flipped == ("pulse",)
    u = run.send(message(22_500, 1.0, load=0.0))
    assert u.gates == {"pulse": False, "air": False} and u.deferred == ("air",)
    assert u.sent["arousal"] == 0.0 and u.sent["cognitive_load"] == pytest.approx(0.21, abs=1e-12)


def test_random_inputs_keep_every_invariant(tmp_path):
    run = Checked(open_log(tmp_path))
    rng = random.Random(2_5)
    t = 0
    for _ in range(20_000):
        t += rng.choice((1, 100, 700, 2_000, 2_000, 13_000))
        if rng.random() < 0.5:
            arousal, load = rng.random(), rng.random()
        else:  # hover near a threshold, loads at the extremes included
            load = rng.choice((0.0, 0.5, 0.948, 1.0))
            d = rng.choice((0.35, 0.55)) + rng.uniform(-3, 3) * MARGIN
            arousal = min(1.0, max(0.0, 0.5 + (d - 0.5 + (load - 0.5)) / 0.9))
        run.send(message(t, arousal, load, rng.random()))
    assert all(len(f) > 50 for f in run.flips.values())
    assert any(u.deferred for u in run.updates)
    assert any(u.sent["cognitive_load"] != u.chosen["cognitive_load"] for u in run.updates)


# --- phase-aware session gates ------------------------------------------------------------------


def test_an_aligned_session_prearms_pulse_and_closes_air_before_pulse(tmp_path):
    log, sink = open_log(tmp_path), Recorder()
    feed, frames = feed_and_frames(sink, log, "pose", session_gates=True)
    frames.value = PULSE_ALIGNMENT_PHASE
    baseline = message(0, 0.5, segment="baseline", elapsed=0, nominal=45_000)
    assert feed.on_state(baseline).gates == {"pulse": False, "air": False}
    plan = feed.gate_plan
    assert plan is not None

    # One block before load, pulse's crossing is the only extra PSV and schedules load's exact
    # boundary. It remains the target when the load state arrives on that boundary.
    frames.value = plan.pulse_open.send_frame
    opened = feed.tick((frames.value - PULSE_ALIGNMENT_PHASE) / 48)
    assert len(opened) == 1 and opened[0].reason == "load_pulse_open"
    assert opened[0].flipped == ("pulse",)
    assert opened[0].ramps["pulse"][0] == plan.load_frame
    frames.value = plan.load_frame
    load = message(56_000, 0.9, segment="load", elapsed=0, nominal=75_000)
    assert feed.on_state(load).gates["pulse"]

    # Let air open in load, then take the planned close exactly one block early. Its ramp begins
    # before pulse's first regulate boundary and therefore also ends first.
    frames.value = plan.load_frame + 40 * SAMPLE_RATE
    feed.on_state(message(96_000, 0.9, segment="load", elapsed=40_000, nominal=75_000))
    frames.value = plan.air_close.send_frame
    closed_air = feed.tick((frames.value - PULSE_ALIGNMENT_PHASE) / 48)
    assert len(closed_air) == 1 and closed_air[0].reason == "regulate_air_close"
    assert closed_air[0].ramps["air"][0] == plan.air_close.boundary_frame

    frames.value = plan.regulate_frame
    regulate = feed.on_state(
        message(131_000, 0.9, segment="regulate", elapsed=0, nominal=75_000)
    )
    assert regulate.gates == {"pulse": False, "air": False}
    assert regulate.ramps["pulse"][0] == plan.pulse_close.boundary_frame
    assert regulate.ramps["air"][1] < regulate.ramps["pulse"][1]

    crossings = events(log, "engine_gate_crossing")
    assert [event["reason"] for event in crossings] == [
        "load_pulse_open",
        "regulate_air_close",
    ]
    assert all(event["late_frames"] == 0 and not event["missed_boundary"] for event in crossings)


def test_the_armed_start_frame_survives_a_state_callback_one_block_later(tmp_path):
    log, sink = open_log(tmp_path), Recorder()
    feed, frames = feed_and_frames(sink, log, "pose", session_gates=True)
    feed.arm_start_frame(PULSE_ALIGNMENT_PHASE)
    frames.value = PULSE_ALIGNMENT_PHASE + 480  # one real WASAPI period after start fired
    feed.on_state(message(10_000, 0.5, segment="baseline", elapsed=0, nominal=45_000))
    assert feed.gate_plan is not None
    assert feed.gate_plan.baseline_frame == PULSE_ALIGNMENT_PHASE
    assert not events(log, "engine_phase_misaligned")


def test_an_unaligned_start_frame_is_refused_before_it_can_make_a_plan(tmp_path):
    feed, _ = feed_and_frames(Recorder(), open_log(tmp_path), "pose", session_gates=True)
    with pytest.raises(ValueError, match="not pulse-aligned"):
        feed.arm_start_frame(PULSE_ALIGNMENT_PHASE + 1)


@pytest.mark.parametrize("source", ["body", "pose"])
@pytest.mark.parametrize("segment", ["idle", "reset"])
def test_both_sources_send_the_baseline_pose_between_visitors(tmp_path, source, segment):
    sink = Recorder()
    feed, frames = feed_and_frames(sink, open_log(tmp_path), source, session_gates=True)
    frames.value = PULSE_ALIGNMENT_PHASE
    msg = message(0, 1.0, load=0.0, readiness=0.0, segment=segment)
    update = feed.on_state(msg)
    assert update.chosen == pose_inputs(msg)
    assert update.sent == {"arousal": 0.486, "cognitive_load": 0.65, "readiness": 0.5}
    assert update.gates == {"pulse": False, "air": False}


# --- the session gain ---


def test_the_session_gain_through_a_whole_session(tmp_path):
    log = open_log(tmp_path)
    sink = Recorder()
    gain = SessionGain(sink, log)
    states = fixture_states()
    sent = [(m, gain.on_state(m)) for m in states]
    commands = [(m["segment"], m["t_engine"], c) for m, c in sent if c is not None]
    assert commands[0] == ("idle", 0, (0.0, 3_000.0))
    assert commands[1] == ("baseline", 10_000, (1.0, 2_000.0))
    segment, t, (target, ramp) = commands[2]
    assert (segment, target) == ("resolve", 0.0)
    assert len(commands) == 3 and sink.gains == [c for _, _, c in commands]
    resolve = [m for m in states if m["segment"] == "resolve"]
    started = resolve[0]["t_engine"] - resolve[0]["segment_elapsed_ms"]
    first = next(m for m in resolve if m["segment_elapsed_ms"] >= 45_000 - 22_000)
    assert t == first["t_engine"]
    # The fade starts at the first message from T-22 s, and ends at T-10 s whenever that was.
    assert t + ramp == pytest.approx(started + 45_000 - 10_000, abs=1)
    logged = events(log, "session_gain")
    assert [(e["target"], e["ramp_ms"]) for e in logged] == sink.gains


@pytest.mark.parametrize(
    ("elapsed", "wanted"),
    [
        (0, (1.0, 2_000.0)),
        (22_999, (1.0, 2_000.0)),
        (23_000, (0.0, 12_000.0)),
        (24_965, (0.0, 10_035.0)),
        (35_000, (0.0, 0.0)),
        (44_000, (0.0, 0.0)),
    ],
)
def test_resolve_fades_bed_sub_and_air_from_t_minus_22_to_t_minus_10(elapsed, wanted):
    msg = message(100_000 + elapsed, 0.5, segment="resolve", elapsed=elapsed, nominal=45_000)
    assert session_gain_for(msg) == wanted


@pytest.mark.parametrize(
    ("segment", "wanted"),
    [
        ("idle", (0.0, 3_000.0)),
        ("baseline", (1.0, 2_000.0)),
        ("load", (1.0, 2_000.0)),
        ("regulate", (1.0, 2_000.0)),
        ("reset", (0.0, 3_000.0)),
    ],
)
def test_the_session_gain_per_segment(segment, wanted):
    assert session_gain_for(message(0, 0.5, segment=segment, elapsed=90_000)) == wanted


def test_the_session_gain_sends_only_on_change(tmp_path):
    sink = Recorder()
    gain = SessionGain(sink, open_log(tmp_path))

    def at(t, segment, elapsed=0, nominal=45_000):
        return gain.on_state(message(t, 0.5, segment=segment, elapsed=elapsed, nominal=nominal))

    assert at(0, "idle") == (0.0, 3_000.0)
    assert at(2_000, "idle") is None
    assert at(4_000, "baseline") == (1.0, 2_000.0)
    assert at(6_000, "load", 0, 75_000) is None
    # Stopped in load: a short reset, the 3 s fade.
    assert at(8_000, "reset", 0, 3_000) == (0.0, 3_000.0)
    assert at(10_000, "reset", 2_000, 3_000) is None
    assert at(12_000, "baseline") == (1.0, 2_000.0)
    assert at(100_000, "resolve", 20_000) is None
    assert at(103_000, "resolve", 23_000) == (0.0, 12_000.0)
    assert at(105_000, "resolve", 25_000) is None  # the same ramp, still ending at T-10 s
    # Stopped during the ending: 3 s is sooner than the 10 s left, so it is sent.
    assert at(106_000, "reset", 0, 3_000) == (0.0, 3_000.0)
    assert at(108_000, "reset", 2_000, 3_000) is None
    assert at(109_000, "idle") is None
    assert sink.gains == [
        (0.0, 3_000.0),
        (1.0, 2_000.0),
        (0.0, 3_000.0),
        (1.0, 2_000.0),
        (0.0, 12_000.0),
        (0.0, 3_000.0),
    ]


def test_a_completed_resolve_leaves_nothing_for_reset_to_send(tmp_path):
    sink = Recorder()
    gain = SessionGain(sink, open_log(tmp_path))
    gain.on_state(message(0, 0.5, segment="baseline"))
    gain.on_state(message(50_000, 0.5, segment="resolve", elapsed=23_000, nominal=45_000))
    assert gain.on_state(message(72_000, 0.5, segment="reset", elapsed=0, nominal=20_000)) is None
    assert gain.on_state(message(92_000, 0.5, segment="idle")) is None
    assert sink.gains == [(1.0, 2_000.0), (0.0, 12_000.0)]


def test_a_live_session_stopped_during_resolves_ending(tmp_path):
    # The fixture's armband through the real state machine, stopped 30 s into resolve, T-15 s.
    live = run_live(FIXTURE_PROFILE, log_dir=tmp_path / "session", stop_s=(256.0,), until_s=262.0)
    segments = live.segments()
    resolve_at = next(at for segment, at in segments if segment == "resolve")
    assert segments[-1] == ("reset", 256_000.0)
    assert 45_000 - 22_000 < 256_000 - resolve_at < 45_000 - 10_000
    log = open_log(tmp_path)
    sink = Recorder()
    feed, _ = feed_and_frames(sink, log, "pose")
    gain = SessionGain(sink, log)
    commands = []
    for msg in live.states():
        u = feed.on_state(msg)
        assert not u.gates["air"] or u.gates["pulse"]
        if (sent := gain.on_state(msg)) is not None:
            commands.append((msg["t_engine"], msg["segment"], sent))
    assert len(sink.moods) == len(live.states())
    assert [(segment, c) for _, segment, c in commands[:2]] == [
        ("idle", (0.0, 3_000.0)),
        ("baseline", (1.0, 2_000.0)),
    ]
    t, segment, (target, ramp) = commands[2]
    assert (segment, target) == ("resolve", 0.0)
    assert t + ramp == pytest.approx(resolve_at + 45_000 - 10_000, abs=1)
    # The stop fades what is left in 3 s rather than the ending's remaining ramp.
    assert commands[3:] == [(256_000, "reset", (0.0, 3_000.0))]


def test_fade_out_holds_every_state_message_until_resume(tmp_path):
    """EngineHost.stop's fade, the one command while the stream stops: a reset, resolve's ending
    or a new session's baseline arriving during the wait sends nothing, and is logged as held."""
    log = open_log(tmp_path)
    sink = Recorder()
    gain = SessionGain(sink, log)
    assert gain.on_state(message(0, 0.5, segment="regulate")) == (1.0, 2_000.0)
    gain.fade_out(3_000.0, t_engine=1_000.0)
    assert gain.holding
    held = [
        message(2_000, 0.5, segment="reset", elapsed=0, nominal=3_000),
        message(2_500, 0.5, segment="resolve", elapsed=23_500, nominal=45_000),
        message(3_000, 0.5, segment="idle"),
        message(3_500, 0.5, segment="baseline", elapsed=0, nominal=56_000),
        message(4_000, 0.5, segment="load"),
    ]
    assert [gain.on_state(m) for m in held] == [None] * len(held)
    assert sink.gains == [(1.0, 2_000.0), (0.0, 3_000.0)]
    stop = events(log, "session_gain")[-1]
    assert (stop["t_engine"], stop["stop"], stop["target"], stop["ramp_ms"]) == (
        1_000.0,
        True,
        0.0,
        3_000.0,
    )
    assert [
        (e["segment"], e["target"], e["ramp_ms"]) for e in events(log, "session_gain_held")
    ] == [
        ("reset", 0.0, 3_000.0),
        ("resolve", 0.0, 11_500.0),
        ("idle", 0.0, 3_000.0),
        ("baseline", 1.0, 2_000.0),
        ("load", 1.0, 2_000.0),
    ]
    gain.resume()
    assert not gain.holding
    assert gain.on_state(message(6_000, 0.5, segment="load")) == (1.0, 2_000.0)
    assert gain.on_state(message(8_000, 0.5, segment="load")) is None
    assert sink.gains[2:] == [(1.0, 2_000.0)]


@pytest.mark.parametrize("segment", ["baseline", "load"])
def test_resume_resends_the_target_after_a_restart(tmp_path, segment):
    """The stream restarted (EngineHost.start) wherever the gain was: the next message sends its
    target even though it is the one sent last, with or without a fade before."""
    sink = Recorder()
    gain = SessionGain(sink, open_log(tmp_path))
    assert gain.on_state(message(0, 0.5, segment=segment)) == (1.0, 2_000.0)
    assert gain.on_state(message(2_000, 0.5, segment=segment)) is None
    gain.resume()
    assert gain.on_state(message(4_000, 0.5, segment=segment)) == (1.0, 2_000.0)
    gain.fade_out()
    gain.resume()
    assert gain.on_state(message(6_000, 0.5, segment=segment)) == (1.0, 2_000.0)
    gain.fade_out()
    gain.resume()
    assert gain.on_state(message(8_000, 0.5, segment="idle")) == (0.0, 3_000.0)
    assert sink.gains == [
        (1.0, 2_000.0),
        (1.0, 2_000.0),
        (0.0, 3_000.0),
        (1.0, 2_000.0),
        (0.0, 3_000.0),
        (0.0, 3_000.0),
    ]


@pytest.mark.parametrize(
    ("hr_bpm", "hr_base", "wanted"),
    [
        (68.0, 68.0, -13.0),
        (75.5, 68.0, -11.0),
        (83.0, 68.0, -9.0),
        (90.0, 68.0, -9.0),
        (55.0, 68.0, -13.0),
        (75.0, None, -13.0),
        (None, 68.0, -13.0),
        (math.nan, 68.0, -13.0),
    ],
)
def test_heartbeat_load_level_is_clamped_or_held_without_a_baseline(hr_bpm, hr_base, wanted):
    assert heartbeat_load_level_dbfs(hr_bpm, hr_base) == wanted


def test_heartbeat_level_follows_the_script_and_fades_at_resolve_t_minus_3(tmp_path):
    sink = Recorder()
    heartbeat = HeartbeatLevel(sink, open_log(tmp_path))
    assert heartbeat.on_state(
        message(10_000, 0.5, segment="baseline", nominal=45_000)
    ) == ((HEARTBEAT_BASELINE_START_DBFS, 0.0), (HEARTBEAT_BASELINE_END_DBFS, 12_000.0))
    assert heartbeat.on_state(
        message(61_040, 0.5, segment="load", elapsed=5, hr_bpm=75.6, hr_base=68.1)
    ) == ((pytest.approx(-11.0), HEARTBEAT_LEVEL_SMOOTH_MS),)
    assert heartbeat.on_state(
        message(63_040, 0.5, segment="load", elapsed=2_005, hr_bpm=90.0, hr_base=68.1)
    ) == ((HEARTBEAT_LOAD_MAX_DBFS, HEARTBEAT_LEVEL_SMOOTH_MS),)
    assert heartbeat.on_state(
        message(136_040, 0.5, segment="regulate", elapsed=5, nominal=75_000)
    ) == ((HEARTBEAT_LOAD_MAX_DBFS, HEARTBEAT_LEVEL_SMOOTH_MS),)
    assert heartbeat.tick(138_039.999) is None
    assert heartbeat.tick(138_040) == (HEARTBEAT_REGULATE_END_DBFS, 72_995.0)

    resolve = message(226_040, 0.5, segment="resolve", elapsed=5, nominal=45_000)
    assert heartbeat.on_state(resolve) == (
        (HEARTBEAT_REGULATE_END_DBFS, HEARTBEAT_LEVEL_SMOOTH_MS),
    )
    resolve_end = 226_040 - 5 + 45_000
    fade_start = resolve_end - HEARTBEAT_FINAL_FADE_MS
    command_at = fade_start - HEARTBEAT_CHAIN_LATENCY_MS
    assert heartbeat.tick(command_at - 0.001) is None
    assert heartbeat.tick(command_at) == (-math.inf, HEARTBEAT_FINAL_FADE_MS)
    assert heartbeat.tick(command_at + 1_000) is None
    assert heartbeat.on_state(
        message(resolve_end, 0.5, segment="reset", elapsed=0, nominal=20_000)
    ) is None
    assert sink.heartbeats == [
        (HEARTBEAT_BASELINE_START_DBFS, 0.0),
        (HEARTBEAT_BASELINE_END_DBFS, HEARTBEAT_BASELINE_RAMP_MS),
        (pytest.approx(-11.0), HEARTBEAT_LEVEL_SMOOTH_MS),
        (HEARTBEAT_LOAD_MAX_DBFS, HEARTBEAT_LEVEL_SMOOTH_MS),
        (HEARTBEAT_LOAD_MAX_DBFS, HEARTBEAT_LEVEL_SMOOTH_MS),
        (HEARTBEAT_REGULATE_END_DBFS, 72_995.0),
        (HEARTBEAT_REGULATE_END_DBFS, HEARTBEAT_LEVEL_SMOOTH_MS),
        (-math.inf, HEARTBEAT_FINAL_FADE_MS),
    ]


@pytest.mark.parametrize(
    ("hr_bpm", "hr_base", "load_target"),
    [(68.0, 68.0, -13.0), (75.5, 68.0, -11.0), (75.0, None, -13.0)],
)
def test_regulate_always_establishes_minus_9_then_recedes_to_minus_11(
    tmp_path, hr_bpm, hr_base, load_target
):
    sink = Recorder()
    heartbeat = HeartbeatLevel(sink, open_log(tmp_path))
    assert heartbeat.on_state(
        message(0, 0.5, segment="load", hr_bpm=hr_bpm, hr_base=hr_base)
    ) == ((load_target, HEARTBEAT_LEVEL_SMOOTH_MS),)
    assert heartbeat.on_state(
        message(75_000, 0.5, segment="regulate", elapsed=0, nominal=75_000)
    ) == ((HEARTBEAT_LOAD_MAX_DBFS, HEARTBEAT_LEVEL_SMOOTH_MS),)
    assert heartbeat.tick(77_000) == (HEARTBEAT_REGULATE_END_DBFS, 73_000.0)


def test_restart_reconstructs_mid_segment_levels_without_overwriting_staged_ramps(tmp_path):
    sink = Recorder()
    heartbeat = HeartbeatLevel(sink, open_log(tmp_path))

    heartbeat.resume()
    assert heartbeat.on_state(
        message(6_000, 0.5, segment="baseline", elapsed=6_000, nominal=56_000)
    ) == ((-15.5, HEARTBEAT_RESTART_RAMP_MS),)
    assert heartbeat.tick(6_100) == (HEARTBEAT_BASELINE_END_DBFS, 5_900.0)

    heartbeat.fade_out(0.0)
    heartbeat.resume()
    expected_regulate = -9.0 - 2.0 * (30_000.0 - 2_000.0) / (75_000.0 - 2_000.0)
    assert heartbeat.on_state(
        message(40_000, 0.5, segment="regulate", elapsed=30_000, nominal=75_000)
    ) == ((pytest.approx(expected_regulate), HEARTBEAT_RESTART_RAMP_MS),)
    assert heartbeat.tick(40_100) == (HEARTBEAT_REGULATE_END_DBFS, 44_900.0)

    heartbeat.fade_out(0.0)
    heartbeat.resume()
    resolve_end = 145_000.0
    resume_at = 143_000.0
    audible_due = resume_at + HEARTBEAT_RESTART_RAMP_MS + HEARTBEAT_CHAIN_LATENCY_MS
    progress = (audible_due - (resolve_end - HEARTBEAT_FINAL_FADE_MS)) / HEARTBEAT_FINAL_FADE_MS
    expected_resolve = -11.0 + 20.0 * math.log10(math.cos(0.5 * math.pi * progress))
    assert heartbeat.on_state(
        message(resume_at, 0.5, segment="resolve", elapsed=43_000, nominal=45_000)
    ) == ((pytest.approx(expected_resolve), HEARTBEAT_RESTART_RAMP_MS),)
    assert heartbeat.tick(resume_at + HEARTBEAT_RESTART_RAMP_MS) == (
        -math.inf,
        pytest.approx(resolve_end - HEARTBEAT_CHAIN_LATENCY_MS - resume_at - 100.0),
    )


def test_heartbeat_level_uses_the_same_stop_latch_as_session_gain(tmp_path):
    sink = Recorder()
    heartbeat = HeartbeatLevel(sink, open_log(tmp_path))
    heartbeat.on_state(
        message(0, 0.5, segment="load", hr_bpm=80.0, hr_base=68.0)
    )
    heartbeat.fade_out(t_engine=1_000.0)
    assert heartbeat.holding
    assert heartbeat.on_state(message(2_000, 0.5, segment="baseline")) is None
    assert heartbeat.tick(3_000.0) is None
    heartbeat.resume()
    assert not heartbeat.holding
    assert heartbeat.on_state(
        message(4_000, 0.5, segment="load", hr_bpm=80.0, hr_base=68.0)
    ) == ((pytest.approx(-9.8), HEARTBEAT_LEVEL_SMOOTH_MS),)
    assert sink.heartbeats == [
        (pytest.approx(-9.8), HEARTBEAT_LEVEL_SMOOTH_MS),
        (-math.inf, HEARTBEAT_FINAL_FADE_MS),
        (pytest.approx(-9.8), HEARTBEAT_LEVEL_SMOOTH_MS),
    ]


def test_heartbeat_onset_measurements_are_logged_from_the_control_thread(tmp_path):
    class TimedRecorder(Recorder):
        records = [
            {"t_play_ms": 1_500.0, "error_ms": -0.25},
            {"t_play_ms": 2_250.0, "error_ms": 0.4},
        ]
        dropped = 0

        def drain_heartbeat_onsets(self):
            records, self.records = self.records, []
            return records

        def stats(self):
            return type(
                "Stats",
                (),
                {
                    "device_anchor_error_ms": 1.5,
                    "device_anchor_slew_ms": 0.75,
                    "device_clock_samples": 42,
                    "device_clock_failures": 1,
                    "heartbeat_onset_telemetry_dropped": self.dropped,
                },
            )()

    sink = TimedRecorder()
    log = open_log(tmp_path)
    heartbeat = HeartbeatLevel(sink, log)
    heartbeat.on_state(message(2_000, 0.5, segment="idle"))
    heartbeat.on_state(message(4_000, 0.5, segment="idle"))
    sink.records = [{"t_play_ms": 5_500.0, "error_ms": 0.1}]
    sink.dropped = 1
    heartbeat.on_state(message(6_000, 0.5, segment="idle"))
    timing = events(log, "heartbeat_onset_timing")
    measured = [
        (item["measurement"], item["scheduled_t_play_ms"], item["error_ms"])
        for item in timing
    ]
    assert measured == [
        (1, 1_500.0, -0.25),
        (2, 2_250.0, 0.4),
        (3, 5_500.0, 0.1),
    ]
    expected = {
        "anchor_error_ms": 1.5,
        "anchor_slew_ms": 0.75,
        "device_clock_samples": 42,
        "device_clock_failures": 1,
    }
    assert {key: timing[-1][key] for key in expected} == expected
    dropped = events(log, "heartbeat_onset_telemetry_dropped")
    assert [(item["dropped"], item["new_dropped"]) for item in dropped] == [(1, 1)]
