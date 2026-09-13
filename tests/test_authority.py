"""bridge/authority.py: min(confidence, segment ceiling), the resolve taper, and nothing else."""

import math
import random
import re
from fractions import Fraction
from pathlib import Path

import pytest

from bridge.authority import authority, resolve_taper
from bridge.contract import CEILINGS, DIMENSIONS, SEGMENTS, validate
from bridge.psv import Confidences

ROOT = Path(__file__).resolve().parent.parent
ONES = dict.fromkeys(DIMENSIONS, 1.0)
ZERO = dict.fromkeys(DIMENSIONS, 0.0)
GARBAGE = [math.nan, math.inf, -math.inf, None, "0.5", True, 10**400, -1e308, -0.5, 1.5, [], {}]


def grant(confidence, segment, elapsed=0, nominal=45_000, entry=None):
    return authority(
        confidence,
        segment,
        segment_elapsed_ms=elapsed,
        segment_nominal_ms=nominal,
        resolve_entry=entry,
    )


def state(segment, confidence, granted, elapsed=0, nominal=45_000):
    return {
        "type": "state",
        "v": 1,
        "session": "S-20260913-0001",
        "seq": 1,
        "t_engine": 1000,
        "t_session": None if segment == "idle" else 1000,
        "segment": segment,
        "segment_elapsed_ms": elapsed,
        "segment_nominal_ms": nominal,
        "psv": {"arousal": 0.5, "valence": 0.5, "cognitive_load": 0.5, "readiness": 0.5},
        "confidence": confidence,
        "authority": granted,
        "hr_bpm": 70.0,
        "hr_base": 68.0,
        "signal": {"contact": True, "rr_accepted_pct": 1.0, "baseline_quality": 1.0},
    }


# --- the table ---


@pytest.mark.parametrize("segment", ["idle", "baseline", "load", "regulate", "reset"])
def test_full_confidence_gets_exactly_the_segment_ceiling(segment):
    assert grant(ONES, segment) == CEILINGS[segment]


def test_the_ceilings_are_claude_md_s_table():
    assert grant(ONES, "baseline") == ZERO
    assert grant(ONES, "load") == {**ZERO, "arousal": 0.2, "cognitive_load": 0.2}
    assert grant(ONES, "regulate") == {**ONES, "valence": 0.0}


@pytest.mark.parametrize("segment", ["load", "regulate"])
def test_authority_never_exceeds_confidence(segment):
    low = {"arousal": 0.15, "valence": 0.0, "cognitive_load": 0.05, "readiness": 0.3}
    granted = grant(low, segment)
    assert granted == {d: min(low[d], CEILINGS[segment][d]) for d in DIMENSIONS}


def test_valence_authority_is_exactly_zero_in_every_segment_whatever_it_is_given():
    for segment in [*SEGMENTS, "nonsense"]:
        for elapsed in (0, 10_000, 40_000):
            value = grant(ONES, segment, elapsed, 45_000, ONES)["valence"]
            assert type(value) is float and value == 0.0 and math.copysign(1.0, value) == 1.0


def test_the_segment_clock_and_the_entry_cannot_be_left_out():
    with pytest.raises(TypeError):
        authority(ONES, "resolve")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        authority(ONES, "load", 0, 75_000, None)  # type: ignore[misc]


# --- the resolve taper ---


def test_resolve_tapers_linearly_from_its_entry_to_zero_at_t_minus_12_s():
    entry = {"arousal": 0.9, "valence": 0.0, "cognitive_load": 0.6, "readiness": 0.3}
    assert grant(ONES, "resolve", 0, 45_000, entry) == entry
    assert grant(ONES, "resolve", 16_500, 45_000, entry) == {
        "arousal": 0.45,
        "valence": 0.0,
        "cognitive_load": 0.3,
        "readiness": 0.15,
    }
    for elapsed in (33_000, 40_000, 45_000, 60_000):
        assert grant(ONES, "resolve", elapsed, 45_000, entry) == ZERO
    series = [grant(ONES, "resolve", t, 45_000, entry)["arousal"] for t in range(0, 45_001, 500)]
    assert all(b <= a for a, b in zip(series, series[1:], strict=False))


def test_confidence_finer_than_thousandths_is_floored_not_rounded():
    finer = {"arousal": 0.2345, "valence": 0.0, "cognitive_load": 0.19999, "readiness": 0.9999}
    assert grant(finer, "regulate") == {
        "arousal": 0.234,
        "valence": 0.0,
        "cognitive_load": 0.199,
        "readiness": 0.999,
    }
    assert grant(finer, "load")["cognitive_load"] == 0.199


def test_the_taper_is_floored_exactly_to_thousandths():
    assert grant(ONES, "resolve", 2_000, 45_000, ONES)["arousal"] == 0.939  # 0.93939...
    assert (
        grant(ONES, "resolve", 22_000, 45_000, dict(ONES, cognitive_load=0.6))["cognitive_load"]
        == 0.2
    )  # 0.6 x 1/3, which float multiplication puts just under 0.2
    assert grant(ONES, "resolve", 3_000, 45_000, dict(ONES, readiness=0.011))["readiness"] == 0.01
    for k in range(0, 1001, 7):
        entry = dict.fromkeys(DIMENSIONS, k / 1000)
        for t in range(0, 33_001, 1_500):
            exact = math.floor(Fraction(k, 1000) * Fraction(33_000 - t, 33_000) * 1000) / 1000
            assert grant(ONES, "resolve", t, 45_000, entry)["arousal"] == exact, (k, t)


def test_resolve_never_exceeds_confidence_either():
    lower = {"arousal": 0.2, "valence": 0.0, "cognitive_load": 0.9, "readiness": 0.0}
    assert grant(lower, "resolve", 0, 45_000, ONES) == lower
    assert grant(lower, "resolve", 16_500, 45_000, ONES)["cognitive_load"] == 0.5


def test_the_taper_is_measured_from_the_nominal_end():
    entry = {"arousal": 0.8, "valence": 0.0, "cognitive_load": 0.4, "readiness": 0.2}
    assert resolve_taper(0, 45_000) == 1.0 and resolve_taper(33_000, 45_000) == 0.0
    # Resolve running 50 s: T-45 s is 5 s in, and T-12 s is 38 s in.
    assert grant(ONES, "resolve", 5_000, 50_000, entry) == entry
    assert grant(ONES, "resolve", 21_500, 50_000, entry) == {
        "arousal": 0.4,
        "valence": 0.0,
        "cognitive_load": 0.2,
        "readiness": 0.1,
    }
    assert grant(ONES, "resolve", 38_000, 50_000, entry) == ZERO


def test_resolve_without_an_entry_grants_nothing():
    assert grant(ONES, "resolve", 0, 45_000, None) == ZERO


def test_psv_confidences_can_be_passed_directly():
    confidences = Confidences(arousal=0.7, cognitive_load=0.1, readiness=0.4)
    assert grant(confidences, "regulate") == {
        "arousal": 0.7,
        "valence": 0.0,
        "cognitive_load": 0.1,
        "readiness": 0.4,
    }


# --- garbage and the contract ---


def test_garbage_never_raises_and_never_grants_more_than_the_confidence():
    rng = random.Random(7)
    for _ in range(2000):
        confidence = {d: rng.choice([*GARBAGE, rng.random()]) for d in DIMENSIONS}
        segment = rng.choice([*SEGMENTS, None, 3, "Resolve"])
        elapsed = rng.choice([*GARBAGE, rng.uniform(0, 60_000)])
        nominal = rng.choice([*GARBAGE, 45_000])
        entry = rng.choice(
            [None, "x", {d: rng.choice([*GARBAGE, rng.random()]) for d in DIMENSIONS}]
        )
        granted = grant(confidence, segment, elapsed, nominal, entry)
        assert set(granted) == set(DIMENSIONS)
        for d in DIMENSIONS:
            value, given = granted[d], confidence[d]
            assert type(value) is float and math.isfinite(value) and 0.0 <= value <= 1.0
            if isinstance(given, float) and math.isfinite(given):
                assert value <= max(0.0, min(1.0, given))
            else:
                assert value == 0.0


def test_every_granted_authority_passes_the_contract():
    rng = random.Random(11)
    for _ in range(3000):
        segment = rng.choice(SEGMENTS)
        confidence = {d: math.floor(rng.random() * 1000) / 1000 for d in DIMENSIONS}
        confidence["valence"] = 0.0
        entry = {d: math.floor(rng.random() * 1000) / 1000 for d in DIMENSIONS}
        elapsed = rng.randrange(0, 60_000) if segment != "reset" else 0
        nominal = 45_000 if segment in ("baseline", "resolve") else 75_000
        granted = grant(confidence, segment, elapsed, nominal, entry)
        validate(state(segment, confidence, granted, elapsed, nominal), "out")


# --- computed in one place ---

CODE = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".cs", ".cpp", ".h", ".hpp"}


def code_files():
    for folder in ("bridge", "tools", "web", "native", "unity"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and path.suffix in CODE and "__pycache__" not in path.parts:
                yield path


def test_nothing_but_authority_py_holds_the_ceilings_or_the_taper():
    """The rule lives in one place. Its table and its timings are not to be found anywhere else,
    by name or as the taper's numbers."""
    allowed = {ROOT / "bridge" / "contract.py", ROOT / "bridge" / "authority.py"}
    names = re.compile(r"\b(CEILINGS|RESOLVE_TAPER_MS|RESOLVE_SILENT_MS)\b")
    numbers = re.compile(r"\b(33_?000|12_?000)\b")
    offenders = [
        str(path.relative_to(ROOT))
        for path in code_files()
        if path not in allowed
        and (names.search(text := path.read_text(encoding="utf-8")) or numbers.search(text))
    ]
    assert offenders == []


def test_only_the_state_message_builder_calls_authority():
    """Everything else reads authority from the state message."""
    allowed = {ROOT / "bridge" / "state.py"}
    calls = re.compile(r"bridge\.authority|from bridge import .*\bauthority\b|authority\.js")
    offenders = [
        str(path.relative_to(ROOT))
        for path in code_files()
        if path not in allowed
        and path != ROOT / "bridge" / "authority.py"
        and calls.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
