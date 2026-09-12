"""bridge/contract.py against docs/message-contract-v1.md."""

import copy
import math

import pytest

from bridge.contract import ContractError, DecodeError, VersionError, decode, encode, validate

# The examples in the contract document, verbatim.
BEAT = {
    "type": "beat",
    "v": 1,
    "session": "S-20260915-0042",
    "seq": 318,
    "t_play": 184320,
    "rr_ms": 812,
    "hr_bpm": 73.9,
    "quality": "ok",
}
STATE = {
    "type": "state",
    "v": 1,
    "session": "S-20260915-0042",
    "seq": 92,
    "t_engine": 184000,
    "t_session": 121400,
    "segment": "regulate",
    "segment_elapsed_ms": 1400,
    "segment_nominal_ms": 75000,
    "psv": {"arousal": 0.61, "valence": 0.50, "cognitive_load": 0.44, "readiness": 0.55},
    "confidence": {"arousal": 0.88, "valence": 0.00, "cognitive_load": 0.71, "readiness": 0.63},
    "authority": {"arousal": 0.88, "valence": 0.00, "cognitive_load": 0.71, "readiness": 0.63},
    "hr_bpm": 96.2,
    "hr_base": 71.0,
    "signal": {"contact": True, "rr_accepted_pct": 0.94, "baseline_quality": 0.81},
}
PONG = {"type": "clock", "v": 1, "role": "pong", "t_client_sent": 40219, "t_engine": 184001}
PING = {"type": "clock", "v": 1, "role": "ping", "t_client_sent": 40219}
TASK_EVENT = {
    "type": "task_event",
    "v": 1,
    "session": "S-20260915-0042",
    "t_client": 40530,
    "event": "miss",
    "dwell_ms": 480,
    "split_interval_ms": 3100,
    "difficulty": 0.62,
}
HELLO = {"type": "hello", "v": 1, "client": "task-screen", "build": "0.4.2"}


def with_(msg, **changes):
    out = copy.deepcopy(msg)
    for path, value in changes.items():
        *parents, leaf = path.split("__")
        target = out
        for key in parents:
            target = target[key]
        if value is ...:
            del target[leaf]
        else:
            target[leaf] = value
    return out


@pytest.mark.parametrize(
    ("msg", "direction"),
    [(BEAT, "out"), (STATE, "out"), (PONG, "out"), (PING, "in"), (TASK_EVENT, "in"), (HELLO, "in")],
    ids=["beat", "state", "pong", "ping", "task_event", "hello"],
)
def test_the_documents_examples_are_valid(msg, direction):
    validate(msg, direction)
    assert decode(encode(msg)) == msg


@pytest.mark.parametrize(
    ("msg", "direction"),
    [(BEAT, "in"), (STATE, "in"), (PONG, "in"), (PING, "out"), (TASK_EVENT, "out"), (HELLO, "out")],
    ids=["beat", "state", "pong", "ping", "task_event", "hello"],
)
def test_each_message_travels_one_way(msg, direction):
    with pytest.raises(ContractError):
        validate(msg, direction)


def test_nothing_else_goes_over_the_link():
    with pytest.raises(ContractError, match="unknown field 'extra'"):
        validate(with_(BEAT, extra=1), "out")
    with pytest.raises(ContractError, match="missing quality"):
        validate(with_(BEAT, quality=...), "out")
    with pytest.raises(ContractError, match="not a laptop to client"):
        validate(with_(BEAT, type="heartbeat"), "out")


def test_another_version_is_refused_by_name():
    with pytest.raises(VersionError, match="v2"):
        validate(with_(HELLO, v=2), "in")
    with pytest.raises(ContractError):
        validate(with_(HELLO, v=...), "in")


def test_valence_confidence_is_exactly_zero():
    with pytest.raises(ContractError, match="valence"):
        validate(with_(STATE, confidence__valence=0.01), "out")


def test_authority_is_never_above_confidence_or_the_segment_ceiling():
    with pytest.raises(ContractError, match="exceeds confidence"):
        validate(with_(STATE, authority__arousal=0.9), "out")
    load = with_(STATE, segment="load")
    with pytest.raises(ContractError, match="load ceiling"):
        validate(load, "out")
    validate(
        with_(
            load, authority__arousal=0.2, authority__cognitive_load=0.2, authority__readiness=0.0
        ),
        "out",
    )
    with pytest.raises(ContractError, match="baseline ceiling"):
        validate(with_(STATE, segment="baseline"), "out")


def test_resolve_authority_tapers_to_zero_by_t_minus_12_s():
    resolve = with_(STATE, segment="resolve", segment_nominal_ms=45000)
    validate(with_(resolve, segment_elapsed_ms=0), "out")  # full authority at T-45 s
    halfway = with_(resolve, segment_elapsed_ms=16500)  # the bound is 0.5
    validate(
        with_(
            halfway, authority__arousal=0.5, authority__cognitive_load=0.5, authority__readiness=0.5
        ),
        "out",
    )
    with pytest.raises(ContractError, match="resolve taper"):
        validate(halfway, "out")
    zero = {"arousal": 0.0, "valence": 0.0, "cognitive_load": 0.0, "readiness": 0.0}
    validate(with_(resolve, segment_elapsed_ms=44000, authority=zero), "out")
    with pytest.raises(ContractError, match="resolve taper"):
        validate(with_(resolve, segment_elapsed_ms=44000), "out")


def test_t_session_is_null_exactly_when_idle():
    idle = with_(
        STATE,
        segment="idle",
        t_session=None,
        authority={"arousal": 0, "valence": 0, "cognitive_load": 0, "readiness": 0},
    )
    validate(idle, "out")
    with pytest.raises(ContractError, match="null when segment is idle"):
        validate(with_(idle, t_session=5), "out")
    with pytest.raises(ContractError, match="t_session"):
        validate(with_(STATE, t_session=None), "out")


def test_heart_rates_may_be_unknown():
    validate(with_(STATE, hr_bpm=None, hr_base=None), "out")
    with pytest.raises(ContractError):
        validate(with_(STATE, hr_bpm=0), "out")


@pytest.mark.parametrize(
    "change",
    [
        {"seq": True},
        {"t_play": 184320.5},
        {"t_play": -1},
        {"rr_ms": math.nan},
        {"session": "Alice"},
        {"session": "S-2026-0042"},
        {"quality": "good"},
        {"t_play": 2**60},
        {"rr_ms": 10**400},
    ],
    ids=[
        "bool seq",
        "fractional t_play",
        "negative t_play",
        "nan",
        "a name",
        "bad id",
        "quality",
        "past 2**53",
        "huge int",
    ],
)
def test_beat_field_types(change):
    with pytest.raises(ContractError):
        validate(with_(BEAT, **change), "out")


def test_client_clocks_may_be_fractional_laptop_clocks_may_not():
    validate(with_(PING, t_client_sent=40219.37), "in")
    validate(with_(TASK_EVENT, t_client=40530.2, dwell_ms=480.5), "in")
    with pytest.raises(ContractError):
        validate(with_(PONG, t_engine=184001.5), "out")


def test_hello_names_a_known_client():
    with pytest.raises(ContractError):
        validate(with_(HELLO, client="phone"), "in")
    with pytest.raises(ContractError):
        validate(with_(HELLO, build=""), "in")


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[1, 2]",
        '{"v": NaN}',
        '{"type": "hello", "type": "beat"}',
        '"hello"',
        "[" * 3000 + "]" * 3000,
        '{"v": ' + "1" * 5000 + "}",
        '{"type": "hello", "v": 1, "client": "spectator", "build": "\\ud800"}',
    ],
    ids=["garbage", "array", "NaN", "duplicate key", "string", "deep", "long int", "surrogate"],
)
def test_frames_that_are_not_one_json_object_raise_only_decode_error(text):
    with pytest.raises(DecodeError):
        decode(text)


def test_error_text_is_ascii_whatever_the_client_sent():
    with pytest.raises(ContractError) as error:
        validate(with_(HELLO, client="名" * 80), "in")
    assert str(error.value).isascii()


def test_encode_refuses_nan():
    with pytest.raises(ValueError):
        encode(with_(BEAT, rr_ms=math.inf))
