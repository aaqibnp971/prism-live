"""bridge/engine_feed.py: the PSV feed, its hysteresis, and the session gain (prompt 2.5).

The sink is a recording fake with the two calls the feed makes. A whole session comes from
tools/fixtures/synthetic-clean.jsonl, every state message the bridge sent, in order.
"""

import ast
import copy
import json
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from conftest import run_live

from bridge.engine_feed import (
    FADE_MS,
    GATED,
    LOOP_MS,
    MARGIN,
    PsvFeed,
    SessionGain,
    session_gain_for,
)
from bridge.engine_mapping import DIMENSIONS, GATE_THRESHOLDS, density, map_effective
from bridge.logging import SessionLog
from bridge.poses import pose_inputs

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tools" / "fixtures" / "synthetic-clean.jsonl"
FIXTURE_PROFILE = "68:61,68-100:75,100-72:115,72:150"  # tools/record_fixture.py
HOLD_MS = {stem: LOOP_MS[stem] + FADE_MS for stem in GATED}


class Recorder:
    def __init__(self):
        self.moods = []
        self.gains = []

    def set_mood_override(self, arousal, cognitive_load, readiness):
        self.moods.append((arousal, cognitive_load, readiness))

    def set_session_gain(self, target, ramp_ms):
        self.gains.append((target, ramp_ms))


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


def message(t, arousal, load=0.5, readiness=0.5, segment="regulate", elapsed=0, nominal=75_000):
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
    }


def at_density(t, d, load=0.5):
    """A message whose inputs have density d, by arousal."""
    return message(t, 0.5 + (d - 0.5 + (load - 0.5)) / 0.9, load)


class Checked:
    """A feed, and the invariants every update must keep."""

    def __init__(self, log, source="body"):
        self.sink = Recorder()
        self.feed = PsvFeed(self.sink, log, source)
        self.updates = []
        self.flips = {stem: [] for stem in GATED}

    def send(self, msg):
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
                if self.flips[stem]:
                    assert u.t_engine - self.flips[stem][-1] >= HOLD_MS[stem]
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
    PsvFeed(sink, open_log(tmp_path)).on_state(msg)
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
    feed, gain = PsvFeed(Recorder(), log), SessionGain(Recorder(), log)
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
    feed = PsvFeed(sink, log)
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
        PsvFeed(Recorder(), log, source="script")


def test_the_gate_state_outlives_a_source_switch(tmp_path):
    run = Checked(open_log(tmp_path))
    run.send(message(0, 0.5))  # neutral body: pulse opens, held until 12.5 s
    run.feed.set_source("pose")
    u = run.send(message(2_000, 0.5, segment="baseline"))  # the pose would close it
    assert u.gates["pulse"] and u.moved and u.params.density == pytest.approx(0.36)
    u = run.send(message(12_500, 0.5, segment="baseline"))
    assert u.flipped == ("pulse",) and u.sent == pose_inputs(message(0, 0.5, segment="baseline"))


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
def test_a_flip_holds_for_its_loop_and_the_fade(tmp_path, stem):
    hold = LOOP_MS[stem] + FADE_MS
    assert hold == {"pulse": 12_500, "air": 14_500}[stem]
    low, high = (0.2, 0.45) if stem == "pulse" else (0.45, 0.7)
    run = Checked(open_log(tmp_path))
    run.send(at_density(0, low))
    run.send(at_density(30_000, low))
    u = run.send(at_density(40_000, high))
    assert stem in u.flipped and u.gates[stem]
    assert u.held_until_ms[stem] == 40_000 + hold
    # The body turns straight back. The gate holds open, at its band edge, until the hold ends.
    for t in range(40_100, 40_000 + hold, 100):
        u = run.send(at_density(t, low))
        assert u.gates[stem] and not u.flipped
        assert u.params.density == pytest.approx(GATE_THRESHOLDS[stem] + MARGIN, abs=1e-12)
    u = run.send(at_density(40_000 + hold, low))
    assert u.flipped == (stem,) and not u.gates[stem] and not u.moved
    # And the close holds as long.
    closed_at = 40_000 + hold
    for t in range(closed_at + 100, closed_at + hold, 700):
        u = run.send(at_density(t, high))
        assert not u.gates[stem]
        assert u.params.density == pytest.approx(GATE_THRESHOLDS[stem] - MARGIN, abs=1e-12)
    assert stem in run.send(at_density(closed_at + hold, high)).flipped


def test_pulse_does_not_close_under_an_air_held_open(tmp_path):
    run = Checked(open_log(tmp_path))
    run.send(at_density(0, 0.2))
    u = run.send(at_density(20_000, 0.7))
    assert set(u.flipped) == {"pulse", "air"}
    # At 12.5 s pulse's hold is over but air's is not: pulse stays open under it.
    u = run.send(at_density(32_500, 0.2))
    assert u.gates == {"pulse": True, "air": True} and u.deferred == ("pulse",)
    assert u.params.density == pytest.approx(0.56, abs=1e-12)
    u = run.send(at_density(34_499, 0.2))
    assert u.gates == {"pulse": True, "air": True}
    u = run.send(at_density(34_500, 0.2))
    assert set(u.flipped) == {"pulse", "air"} and u.gates == {"pulse": False, "air": False}


def test_air_does_not_open_over_a_pulse_held_closed(tmp_path):
    run = Checked(open_log(tmp_path))
    run.send(at_density(0, 0.45))
    u = run.send(at_density(20_000, 0.2))
    assert u.flipped == ("pulse",)
    u = run.send(at_density(22_000, 0.9))
    assert u.gates == {"pulse": False, "air": False} and u.deferred == ("air",)
    assert u.params.density == pytest.approx(0.34, abs=1e-12)
    u = run.send(at_density(32_500, 0.9))
    assert set(u.flipped) == {"pulse", "air"} and u.gates == {"pulse": True, "air": True}


def test_load_moves_only_when_arousal_is_pinned(tmp_path):
    run = Checked(open_log(tmp_path), "pose")
    # Pulse opens early in load, air only at 64 s, so air is still held when regulate begins
    # at 75 s: the regulate pose is held back, and only arousal 1.0 with a lower load input
    # keeps air open.
    for k in range(0, 76_000, 2_000):
        u = run.send(message(k, 0.6 if k < 64_000 else 0.9, segment="load", elapsed=k))
        assert u.gates == {"pulse": True, "air": k >= 64_000}
    u = run.send(message(75_000, 0.9, segment="regulate", elapsed=0))
    assert u.deferred == ("pulse",) and u.gates == {"pulse": True, "air": True}
    assert u.chosen == {"arousal": 0.771, "cognitive_load": 0.948, "readiness": 0.11}
    assert u.sent["arousal"] == 1.0
    assert u.sent["cognitive_load"] == pytest.approx(0.89, abs=1e-12)
    assert u.params.density >= 0.56 and u.params.density == pytest.approx(0.56, abs=1e-12)
    # Held closed with density pinned high: arousal 0, then load up.
    run = Checked(open_log(tmp_path))
    run.send(at_density(0, 0.45))
    assert run.send(at_density(20_000, 0.2)).flipped == ("pulse",)
    u = run.send(message(21_000, 1.0, load=0.0))
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
    feed, gain = PsvFeed(sink, log, "pose"), SessionGain(sink, log)
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
