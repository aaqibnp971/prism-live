"""bridge/state.py: state messages from the PSV estimate, with authority computed once."""

import dataclasses

import pytest
from conftest import run_session

from bridge.authority import authority
from bridge.contract import CEILINGS, DIMENSIONS, validate
from bridge.psv import Confidences, PsvModel
from bridge.state import StateStream

SESSION = "S-20260913-0001"
# Armband on at 0 s, baseline 20 s to 76 s (45 s capture held to the pulse boundary at 56 s),
# load 75 s, regulate 75 s, resolve 45 s, then reset.
PROFILE = "68:76,68-105:75,105-75:75,75:50"
TIMELINE = (
    ("idle", 0, 20, 0),
    ("baseline", 20, 76, 45),
    ("load", 76, 151, 75),
    ("regulate", 151, 226, 75),
    ("resolve", 226, 271, 45),
    ("reset", 271, 276, 0),
)


def segment_at(t_s):
    for segment, start, end, nominal in TIMELINE:
        if start <= t_s < end:
            return segment, start, nominal
    return "reset", 271, 0


def whole_session():
    run = run_session(PROFILE, baseline_at_s=20)
    stream = StateStream(SESSION)
    messages = []
    for t_ms, estimate in run.estimates.items():
        segment, start, nominal = segment_at(t_ms / 1000)
        baseline = run.model.baseline
        messages.append(
            stream.message(
                t_engine_ms=t_ms,
                t_session_ms=t_ms - 20_000,
                segment=segment,
                segment_elapsed_ms=0 if segment == "reset" else t_ms - start * 1000,
                segment_nominal_ms=nominal * 1000,
                estimate=estimate,
                hr_base_bpm=baseline.hr_base_bpm if baseline else None,
                baseline_quality=baseline.baseline_quality if baseline else 0.0,
            )
        )
    return messages


def with_confidence(estimate, arousal, cognitive_load=0.0, readiness=0.0):
    confidences = Confidences(arousal=arousal, cognitive_load=cognitive_load, readiness=readiness)
    return dataclasses.replace(estimate, confidence=confidences)


def sender(stream, estimate):
    def send(segment, elapsed, arousal, readiness=0.0, nominal=None):
        return stream.message(
            t_engine_ms=1000,
            t_session_ms=1000,
            segment=segment,
            segment_elapsed_ms=elapsed,
            segment_nominal_ms=nominal if nominal is not None else 45_000,
            estimate=with_confidence(estimate, arousal, readiness=readiness),
            hr_base_bpm=None,
            baseline_quality=0.0,
        )

    return send


def test_a_whole_session_gives_valid_numbered_state_messages():
    messages = whole_session()
    for k, msg in enumerate(messages, start=1):
        validate(msg, "out")
        assert msg["seq"] == k and msg["session"] == SESSION
    assert all(m["t_session"] is None for m in messages if m["segment"] == "idle")


def test_authority_is_the_rule_applied_to_what_the_message_carries():
    messages = whole_session()
    entry, previous = None, None
    for msg in messages:
        if msg["segment"] == "resolve" and previous["segment"] != "resolve":
            entry = previous["authority"]
        expected = authority(
            msg["confidence"],
            msg["segment"],
            segment_elapsed_ms=msg["segment_elapsed_ms"],
            segment_nominal_ms=msg["segment_nominal_ms"],
            resolve_entry=entry if msg["segment"] == "resolve" else None,
        )
        assert msg["authority"] == expected
        if msg["segment"] != "resolve":
            assert all(msg["authority"][d] <= CEILINGS[msg["segment"]][d] for d in DIMENSIONS)
        previous = msg


def test_load_is_capped_regulate_is_not_and_resolve_tapers_to_zero():
    messages = whole_session()
    load = [m for m in messages if m["segment"] == "load" and m["segment_elapsed_ms"] > 20_000]
    assert max(m["authority"]["arousal"] for m in load) == 0.2
    regulate = [m for m in messages if m["segment"] == "regulate"]
    assert max(m["authority"]["arousal"] for m in regulate) > 0.9
    resolve = [m for m in messages if m["segment"] == "resolve"]
    arousal = [m["authority"]["arousal"] for m in resolve]
    assert all(b <= a for a, b in zip(arousal, arousal[1:], strict=False))
    late = [m for m in resolve if m["segment_elapsed_ms"] >= 33_000]
    assert late and all(m["authority"] == dict.fromkeys(DIMENSIONS, 0.0) for m in late)


def test_resolve_tapers_from_the_last_authority_before_it_whatever_confidence_does_next():
    send = sender(StateStream(SESSION), PsvModel().estimate(0))
    send("regulate", 60_000, arousal=1.0)
    send("regulate", 62_000, arousal=0.4, readiness=0.6)  # the last message before resolve
    first = send("resolve", 0, arousal=0.9, readiness=0.9)
    assert first["authority"]["arousal"] == 0.4 and first["authority"]["readiness"] == 0.6
    halfway = send("resolve", 16_500, arousal=0.9, readiness=0.9)
    assert halfway["authority"]["arousal"] == 0.2 and halfway["authority"]["readiness"] == 0.3


def test_a_second_resolve_takes_a_new_entry():
    send = sender(StateStream(SESSION), PsvModel().estimate(0))
    send("regulate", 0, arousal=0.4, readiness=0.6)
    assert send("resolve", 0, arousal=1.0, readiness=1.0)["authority"]["readiness"] == 0.6
    send("reset", 0, arousal=0.0, nominal=0)
    send("idle", 0, arousal=0.0, nominal=0)
    send("regulate", 0, arousal=0.2, readiness=0.2)
    again = send("resolve", 0, arousal=1.0, readiness=1.0)
    assert again["authority"]["arousal"] == 0.2 and again["authority"]["readiness"] == 0.2


def test_a_resolve_with_nothing_before_it_grants_nothing():
    send = sender(StateStream(SESSION), PsvModel().estimate(0))
    assert send("resolve", 0, arousal=1.0)["authority"] == dict.fromkeys(DIMENSIONS, 0.0)


def test_fractional_clock_values_are_rounded_before_authority_is_computed():
    send = sender(StateStream(SESSION), PsvModel().estimate(0))
    send("regulate", 0, arousal=1.0, readiness=0.6)
    msg = send("resolve", 5_500.5, arousal=1.0, readiness=1.0)
    rule = authority(
        msg["confidence"],
        "resolve",
        segment_elapsed_ms=msg["segment_elapsed_ms"],
        segment_nominal_ms=msg["segment_nominal_ms"],
        resolve_entry={"arousal": 1.0, "valence": 0.0, "cognitive_load": 0.0, "readiness": 0.6},
    )
    assert msg["segment_elapsed_ms"] == 5_500 and msg["authority"] == rule


@pytest.mark.parametrize("segment", ["idle", "baseline"])
def test_hr_base_is_null_until_baseline_has_ended_whatever_is_passed(segment):
    msg = StateStream(SESSION).message(
        t_engine_ms=74_000,
        t_session_ms=None if segment == "idle" else 54_000,
        segment=segment,
        segment_elapsed_ms=54_000 if segment == "baseline" else 0,
        segment_nominal_ms=45_000 if segment == "baseline" else 0,
        estimate=PsvModel().estimate(0),
        hr_base_bpm=68.3,
        baseline_quality=0.9,
    )
    validate(msg, "out")
    assert msg["hr_base"] is None


def test_a_degraded_session_sends_no_hr_base_and_zero_quality_whatever_is_passed():
    model = PsvModel()
    model.start_baseline(0)
    model.mark_baseline_degraded()
    msg = StateStream(SESSION).message(
        t_engine_ms=90_000,
        t_session_ms=90_000,
        segment="load",
        segment_elapsed_ms=34_000,
        segment_nominal_ms=75_000,
        estimate=model.estimate(90_000),
        hr_base_bpm=68.3,
        baseline_quality=0.9,
    )
    validate(msg, "out")
    assert msg["hr_base"] is None and msg["signal"]["baseline_quality"] == 0.0


def test_signal_and_heart_rate_fields_obey_the_contract_rules():
    msg = StateStream(SESSION).message(
        t_engine_ms=1234.6,
        t_session_ms=None,
        segment="idle",
        segment_elapsed_ms=0,
        segment_nominal_ms=0,
        estimate=PsvModel().estimate(0),
        hr_base_bpm=float("nan"),
        baseline_quality=7,
    )
    validate(msg, "out")
    assert msg["t_engine"] == 1235 and msg["t_session"] is None
    assert msg["hr_base"] is None and msg["hr_bpm"] is None
    assert msg["signal"] == {"contact": True, "rr_accepted_pct": 0.0, "baseline_quality": 1.0}
