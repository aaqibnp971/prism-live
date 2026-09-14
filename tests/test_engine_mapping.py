"""bridge/engine_mapping.py and bridge/poses.py against docs/engine-findings.md, and the pre-blend.

The mirror is for logging and tests only, but every number the feed logs comes from it, so it is
pinned here to the findings' tables. The pre-blend test is prompt 2.5's first: sending
0.5 + (v - 0.5) x authority at confidence 1.0 makes the engine read exactly what it would read
from v at a per-dimension confidence equal to that authority, which no ABI call can send.
"""

import math
import random

import pytest

from bridge.engine_feed import body_inputs
from bridge.engine_mapping import (
    CONFIDENCE_SENT,
    DIMENSIONS,
    brightness_to_hz,
    clamp01,
    density,
    effective,
    gain_change_db,
    hz_to_brightness,
    level_amplitude,
    level_db,
    map_effective,
)
from bridge.poses import (
    BASELINE,
    BODY_SPAN,
    POSES,
    BodyRange,
    Fixed,
    Timed,
    body_level,
    pose_inputs,
)


def engine_effective(value, confidence):
    """psv/include/psv/rt.h:36, written out again here rather than imported."""
    return 0.5 + (value - 0.5) * confidence


def state(segment="load", elapsed=0, nominal=75_000, arousal=0.5, confidence=1.0, authority=0.2):
    return {
        "t_engine": 0,
        "segment": segment,
        "segment_elapsed_ms": elapsed,
        "segment_nominal_ms": nominal,
        "psv": {"arousal": arousal, "valence": 0.5, "cognitive_load": 0.5, "readiness": 0.5},
        "confidence": {
            "arousal": confidence,
            "valence": 0.0,
            "cognitive_load": 0.0,
            "readiness": 0.0,
        },
        "authority": {
            "arousal": authority,
            "valence": 0.0,
            "cognitive_load": 0.0,
            "readiness": 0.0,
        },
    }


def at(values):
    return map_effective(values["arousal"], values["cognitive_load"], values["readiness"])


# --- the pre-blend ---


def pre_blend_cases():
    rng = random.Random(25)
    cases = [(rng.random(), rng.random()) for _ in range(20_000)]
    # What the link carries: psv to 3 decimals, authority floored to 3.
    cases += [(v / 1000, a / 1000) for v in range(0, 1001, 7) for a in range(0, 1001, 11)]
    tiny, below, above = 5e-324, math.nextafter(0.5, 0.0), math.nextafter(0.5, 1.0)
    edges = (0.0, tiny, 0.25, below, 0.5, above, 0.75, math.nextafter(1.0, 0.0), 1.0)
    cases += [(v, a) for v in edges for a in edges]
    return cases


def test_the_pre_blend_reproduces_per_dimension_authority_exactly():
    for v, auth in pre_blend_cases():
        msg = {
            "psv": {"arousal": v, "cognitive_load": 1.0 - v, "readiness": auth},
            "authority": {"arousal": auth, "cognitive_load": auth, "readiness": v},
        }
        sent = body_inputs(msg)
        for d in DIMENSIONS:
            # The engine rejects any value outside [0, 1] (prism_core.cpp:461-466).
            assert 0.0 <= sent[d] <= 1.0
            wanted = engine_effective(msg["psv"][d], msg["authority"][d])
            assert engine_effective(sent[d], CONFIDENCE_SENT) == wanted, (d, v, auth)
        per_dimension = {
            d: engine_effective(msg["psv"][d], msg["authority"][d]) for d in DIMENSIONS
        }
        read = {d: engine_effective(sent[d], CONFIDENCE_SENT) for d in DIMENSIONS}
        assert at(read) == at(per_dimension)


def test_the_mirror_reads_any_value_sent_at_confidence_1_as_the_engine_does():
    # Poses and hysteresis edges are not of the form 0.5 + (v - 0.5) x authority, and the log
    # maps what was sent, not what the engine read from it. At confidence 1.0 the two agree to
    # the bit: the engine's a = (0.5 + (s - 0.5)) - 0.5 is the mirror's s - 0.5.
    rng = random.Random(1)
    values = [rng.random() for _ in range(30_000)] + [0.0, 5e-324, 0.11, 0.486, 0.948, 1.0]
    values += [math.nextafter(x, y) for x in (0.25, 0.5, 1.0) for y in (0.0, 1.0)]
    for k, s in enumerate(values):
        assert engine_effective(s, CONFIDENCE_SENT) - 0.5 == s - 0.5, s
        other = values[k - 1]
        read = map_effective(*(engine_effective(x, CONFIDENCE_SENT) for x in (s, other, s)))
        assert read == map_effective(s, other, s)


def test_one_shared_confidence_could_not_have_done_it():
    # Load's ceilings: authority 0.2 on arousal, 0 on readiness. Any single confidence gets one
    # of the two wrong, which is why the bridge pre-blends.
    v, authority = {"arousal": 0.9, "readiness": 0.9}, {"arousal": 0.2, "readiness": 0.0}
    wanted = {d: engine_effective(v[d], authority[d]) for d in v}
    for c in (0.0, 0.2, 1.0):
        assert {d: engine_effective(v[d], c) for d in v} != wanted
    assert effective(0.9, 0.2) == wanted["arousal"]


# --- the mapping, against the findings' tables ---


def test_a_neutral_psv_plays_at_2282_hz_with_pulse_open():
    p = map_effective(0.5, 0.5, 0.5)
    assert p.brightness == 0.55
    assert round(p.cutoff_hz) == 2282
    assert p.density == 0.5
    assert p.gates == {"bed": True, "sub": True, "pulse": True, "lead": False, "air": False}
    assert p.gains == {"bed": 0.6, "sub": 0.5, "pulse": 0.5, "lead": 0.5, "air": 0.5}


@pytest.mark.parametrize(
    ("hz", "brightness"), [(1_400, 0.418), (3_600, 0.674), (620, 0.197), (900, 0.298)]
)
def test_the_filter_points_the_script_uses(hz, brightness):
    assert round(hz_to_brightness(hz), 3) == brightness
    assert brightness_to_hz(brightness) == pytest.approx(hz, rel=2e-3)


def test_the_cutoff_runs_300_hz_to_12_khz_on_a_log_scale():
    assert brightness_to_hz(0.0) == 300.0
    assert brightness_to_hz(1.0) == pytest.approx(12_000.0, rel=1e-12)
    assert brightness_to_hz(-3.0) == 300.0
    assert brightness_to_hz(7.0) == brightness_to_hz(1.0)
    assert clamp01(math.nan) == 1.0  # std::min(1.0, NaN) is 1.0


def test_the_gates_are_nested_and_follow_the_cutoff():
    # findings, "The gates are nested, and tied to the filter": below 907 Hz both forced off,
    # 907 to 1,897 air forced off, 1,897 to 3,968 pulse forced on, above 3,968 both forced on.
    steps = 200
    for i in range(steps + 1):
        for j in range(steps + 1):
            p = map_effective(i / steps, j / steps, 0.5)
            g = p.gates
            assert not g["air"] or g["pulse"]
            if p.cutoff_hz < 906:
                assert not g["pulse"] and not g["air"]
            if p.cutoff_hz < 1_896:
                assert not g["air"]
            if p.cutoff_hz > 1_898:
                assert g["pulse"]
            if p.cutoff_hz > 3_969:
                assert g["air"]
            # No open stem can be faded to nothing through its gain.
            if g["pulse"]:
                assert p.gains["pulse"] >= 0.33 - 1e-12
            if g["air"]:
                assert p.gains["air"] >= 0.51 - 1e-12
            assert p.gains["bed"] >= 0.3 - 1e-12 and p.gains["sub"] >= 0.2 - 1e-12


def test_every_output_is_mapping_cpps_expression_to_the_bit():
    # mapping.cpp:39-76, written out again here rather than imported, lead included.
    def c(x):
        return max(0.0, min(1.0, x))

    rng = random.Random(39)
    points = [(rng.random(), rng.random(), rng.random()) for _ in range(5_000)]
    points += [(i / 20, j / 20, k / 20) for i in range(21) for j in range(21) for k in (0, 7, 20)]
    for arousal, load, readiness in points:
        a, l, r = arousal - 0.5, load - 0.5, readiness - 0.5  # noqa: E741
        d = c(0.5 + 0.9 * a - 1.0 * l)
        p = map_effective(arousal, load, readiness)
        assert p.brightness == c(0.55 + 0.9 * a - 0.8 * l)
        assert p.density == d
        assert p.cutoff_hz == 300.0 * math.pow(40.0, p.brightness)
        assert p.gains == {
            "bed": c(0.6 + 0.6 * l),
            "sub": c(0.5 - 0.6 * r),
            "pulse": c(0.5 + 0.7 * a - 0.6 * l),
            "lead": c(0.5 + 0.4 * a - 0.9 * l),
            "air": c(0.5 + 0.5 * a - 0.6 * l),
        }
        assert p.gates == {
            "bed": True,
            "sub": True,
            "pulse": d >= 0.35,
            "lead": d >= 0.72,
            "air": d >= 0.55,
        }


@pytest.mark.parametrize(
    ("stem", "threshold", "arousal"),
    [
        ("pulse", 0.35, 0.3333333333333333),
        ("air", 0.55, 0.5555555555555556),
        ("lead", 0.72, 0.7444444444444444),
    ],
)
def test_a_gate_is_open_at_its_threshold_exactly(stem, threshold, arousal):
    # With load 0.5, each of these arousal inputs gives the threshold itself as a double, and the
    # next representable arousal below it gives a density under the threshold.
    at_threshold = map_effective(arousal, 0.5, 0.5)
    assert at_threshold.density == threshold and at_threshold.gates[stem]
    below = map_effective(math.nextafter(arousal, 0.0), 0.5, 0.5)
    assert below.density < threshold and not below.gates[stem]


def test_level_amplitude_is_0_4_gain_to_the_1_5():
    assert level_amplitude(1.0) == 0.4
    assert level_amplitude(0.25) == pytest.approx(0.05)
    assert level_db(0.0) == -math.inf
    assert gain_change_db(0.5, 0.8) == pytest.approx(30 * math.log10(0.8 / 0.5))


def test_readiness_moves_only_the_sub():
    low, high = map_effective(0.6, 0.4, 0.0), map_effective(0.6, 0.4, 1.0)
    assert (low.cutoff_hz, low.density, low.gates) == (high.cutoff_hz, high.density, high.gates)
    assert {s: g for s, g in low.gains.items() if s != "sub"} == {
        s: g for s, g in high.gains.items() if s != "sub"
    }
    assert (low.gains["sub"], high.gains["sub"]) == (pytest.approx(0.8), pytest.approx(0.2))


# --- the poses, against the findings' starting poses ---


def test_the_baseline_pose_closes_both_gates_at_1400_hz():
    for segment in ("idle", "baseline", "reset"):
        assert POSES[segment] is BASELINE
    sent = pose_inputs(state("baseline", arousal=0.95, confidence=1.0))
    assert sent == {"arousal": 0.486, "cognitive_load": 0.65, "readiness": 0.5}
    p = at(sent)
    assert p.cutoff_hz == pytest.approx(1_400, rel=2e-3)
    # The findings round 0.3374 to 0.338; it is 0.012 under the pulse gate either way.
    assert p.density == pytest.approx(0.338, abs=1e-3)
    assert p.density == pytest.approx(0.3374, abs=1e-12)
    assert not p.gates["pulse"] and not p.gates["air"]
    assert p.gains["bed"] == pytest.approx(0.69)


def test_the_load_pose_sweeps_1400_to_3600_hz_as_the_body_rises():
    bottom = at(pose_inputs(state("load", arousal=0.5)))
    top = at(pose_inputs(state("load", arousal=0.5 + BODY_SPAN)))
    assert pose_inputs(state("load", arousal=0.5 + BODY_SPAN))["arousal"] == pytest.approx(0.771)
    assert bottom.cutoff_hz == pytest.approx(1_400, rel=2e-3)
    assert top.cutoff_hz == pytest.approx(3_600, rel=2e-3)
    assert not bottom.gates["pulse"] and not bottom.gates["air"]
    assert top.gates["pulse"] and top.gates["air"]
    assert bottom.gains["bed"] == top.gains["bed"] and bottom.gains["sub"] == top.gains["sub"]
    # Pulse opens at an arousal input of 0.50 (about 1,470 Hz), air at 0.722 (about 3,070 Hz).
    pulse_edge = 0.5 + (0.35 - 0.5 + 0.15) / 0.9
    air_edge = 0.5 + (0.55 - 0.5 + 0.15) / 0.9
    assert pulse_edge == pytest.approx(0.50, abs=1e-12)
    assert air_edge == pytest.approx(0.722, abs=1e-3)
    assert brightness_to_hz(0.55 + 0.9 * (pulse_edge - 0.5) - 0.12) == pytest.approx(
        1_470, rel=5e-3
    )
    assert brightness_to_hz(0.55 + 0.9 * (air_edge - 0.5) - 0.12) == pytest.approx(3_070, rel=5e-3)


def test_the_regulate_pose_closes_both_gates_and_thickens_bed_and_sub():
    load_end = at(pose_inputs(state("load", arousal=1.0)))
    for arousal in (0.0, 0.5, 0.7, 1.0):
        sent = pose_inputs(state("regulate", elapsed=0, arousal=arousal))
        assert (sent["cognitive_load"], sent["readiness"]) == (0.948, 0.11)  # from the first PSV
        p = at(sent)
        assert not p.gates["pulse"] and not p.gates["air"]
    low = at(pose_inputs(state("regulate", arousal=0.5)))
    assert low.cutoff_hz == pytest.approx(620, rel=2e-3)
    assert at(pose_inputs(state("regulate", arousal=1.0))).cutoff_hz == pytest.approx(1_496, abs=1)
    assert gain_change_db(load_end.gains["bed"], low.gains["bed"]) == pytest.approx(3.0, abs=0.01)
    assert gain_change_db(load_end.gains["sub"], low.gains["sub"]) == pytest.approx(5.0, abs=0.01)


def test_the_resolve_pose_lifts_620_to_900_hz_across_t_minus_20_to_t_minus_12():
    def cutoff(elapsed):
        return at(pose_inputs(state("resolve", elapsed=elapsed, nominal=45_000, arousal=1.0)))

    assert cutoff(0).cutoff_hz == pytest.approx(620, rel=2e-3)
    assert cutoff(25_000).cutoff_hz == cutoff(0).cutoff_hz
    assert cutoff(33_000).cutoff_hz == pytest.approx(900, rel=2e-3)
    assert cutoff(45_000).cutoff_hz == cutoff(33_000).cutoff_hz
    sweep = [cutoff(e) for e in range(0, 45_001, 500)]
    hz = [p.cutoff_hz for p in sweep]
    assert hz == sorted(hz)
    assert all(not p.gates["pulse"] and not p.gates["air"] for p in sweep)
    assert len({(p.gains["bed"], p.gains["sub"]) for p in sweep}) == 1


def test_a_body_range_reads_confidence_not_authority_and_no_ceiling():
    shape = BodyRange(0.2, 0.6)
    # Load's ceiling is 0.2, and it is not applied: the pose is the segment's design.
    assert shape.at(state(arousal=0.5 + BODY_SPAN, confidence=1.0, authority=0.2)) == 0.6
    assert shape.at(state(arousal=0.5 + BODY_SPAN, confidence=0.0, authority=0.2)) == 0.2
    assert shape.at(state(arousal=0.5 + BODY_SPAN / 2, confidence=0.5)) == pytest.approx(0.3)
    assert shape.at(state(arousal=0.1, confidence=1.0)) == 0.2
    assert shape.at(state(arousal=1.0, confidence=1.0)) == 0.6
    assert body_level(state(arousal=0.5 + BODY_SPAN / 4, confidence=1.0)) == pytest.approx(0.25)


def test_timed_follows_the_segment_clock_not_a_local_one():
    shape = Timed(0.0, 1.0, 20, 10)
    assert shape.at(state(elapsed=0, nominal=45_000)) == 0.0
    assert shape.at(state(elapsed=25_000, nominal=45_000)) == 0.0
    assert shape.at(state(elapsed=30_000, nominal=45_000)) == 0.5
    assert shape.at(state(elapsed=35_000, nominal=45_000)) == 1.0
    assert shape.at(state(elapsed=90_000, nominal=45_000)) == 1.0
    # The same elapsed time under a longer nominal is earlier in the segment.
    assert shape.at(state(elapsed=35_000, nominal=60_000)) == 0.0
    assert Fixed(0.3).at(state()) == 0.3


def test_every_pose_stays_inside_what_the_engine_accepts():
    for segment, pose in POSES.items():
        for arousal in (0.0, 0.5, 0.85, 1.0):
            for elapsed in (0, 20_000, 30_000, 45_000, 105_000):
                sent = pose.at(state(segment, elapsed=elapsed, nominal=45_000, arousal=arousal))
                assert all(0.0 <= sent[d] <= 1.0 for d in DIMENSIONS), (segment, sent)
                assert density(sent["arousal"], sent["cognitive_load"]) == at(sent).density
