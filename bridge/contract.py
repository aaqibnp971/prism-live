"""docs/message-contract-v1.md, as code.

The document is the source of truth and this follows it. Every message on the link goes
through ``validate``: what the laptop sends, before it goes out, and what clients send, as it
arrives. Nothing else goes over the link (contract §4, rule 1), so an unknown type or an
unknown field is an error, not a warning.

It follows contract v1.3.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

VERSION = 1
PORT = 8787
PATH = "/live"
MIN_LEAD_MS = 300  # §2: t_play is at least this far in the future when a beat is sent
STATE_INTERVAL_MS = 2000

SEGMENTS = ("idle", "baseline", "load", "regulate", "resolve", "reset")
QUALITIES = ("ok", "interpolated", "rejected")
TASK_EVENTS = ("split", "lock", "miss", "abandon")
CLIENTS = ("task-screen", "spectator", "quest")
DIMENSIONS = ("arousal", "valence", "cognitive_load", "readiness")
SESSION_ID = re.compile(r"S-\d{8}-\d{4}")  # a date and a counter, never a name

_ZERO = dict.fromkeys(DIMENSIONS, 0.0)
# §2 segment ceilings: authority = min(confidence, ceiling). Resolve's ceiling tapers from its
# entry value to 0 across T-45 s to T-12 s; the row below is only its starting bound, and the
# taper is checked from the segment clock in _state.
# idle and reset are 0: nothing acts outside a session (§2, v1.1).
CEILINGS = {
    "idle": _ZERO,
    "baseline": _ZERO,
    "load": {"arousal": 0.2, "valence": 0.0, "cognitive_load": 0.2, "readiness": 0.0},
    "regulate": {"arousal": 1.0, "valence": 0.0, "cognitive_load": 1.0, "readiness": 1.0},
    "resolve": {"arousal": 1.0, "valence": 0.0, "cognitive_load": 1.0, "readiness": 1.0},
    "reset": _ZERO,
}

RESOLVE_TAPER_MS = 33_000  # the ceiling falls from T-45 s ...
RESOLVE_SILENT_MS = 12_000  # ... to 0 at T-12 s, and stays there
AUTHORITY_ROUNDING = 0.001  # authority is sent to 3 decimals
# No integer on the link past 2**53 - 1: a JavaScript client cannot hold it exactly (§1, v1.1).
MAX_SAFE_INT = 2**53 - 1

_OUT = ("beat", "state", "clock")  # laptop to client
_IN = ("task_event", "hello", "clock")  # client to laptop


class ContractError(ValueError):
    """A message that breaks the contract."""


class VersionError(ContractError):
    """A message on a schema version this laptop does not speak (§4 rule 5)."""


class DecodeError(ContractError):
    """A frame that is not exactly one JSON object."""


def encode(msg: dict) -> str:
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def decode(text: str) -> dict:
    """Parse one frame. Raises DecodeError, and nothing else, for any frame that is not one
    JSON object a log can hold."""
    try:
        msg = json.loads(text, parse_constant=_no_constant, object_pairs_hook=_no_duplicate_keys)
    except DecodeError:
        raise
    except (ValueError, RecursionError) as error:  # bad JSON, a huge integer, deep nesting
        raise DecodeError(f"not JSON: {error}") from None
    if not isinstance(msg, dict):
        raise DecodeError("a message is one JSON object")
    try:
        encode(msg).encode("utf-8")
    except UnicodeEncodeError:
        raise DecodeError("a string holds an unpaired surrogate escape") from None
    return msg


def validate(msg: Any, direction: str) -> None:
    """Raise ContractError unless msg is a valid v1 message for its direction.

    direction is "out" for laptop to client, "in" for client to laptop.
    """
    if direction not in ("out", "in"):
        raise ValueError(f"direction is 'out' or 'in', not {direction!r}")
    if not isinstance(msg, dict):
        raise ContractError("a message is one JSON object")
    if not _is_int(msg.get("v")):
        raise ContractError("v is missing or not an integer")
    if msg["v"] != VERSION:
        raise VersionError(f"schema v{msg['v']} is not supported; the laptop speaks v{VERSION}")
    kind = msg.get("type")
    if kind not in (_OUT if direction == "out" else _IN):
        way = "laptop to client" if direction == "out" else "client to laptop"
        raise ContractError(f"type {kind!r} is not a {way} message")
    _CHECKS[kind](msg, direction)


# --- the six messages ---


def _beat(msg: dict, _direction: str) -> None:
    _fields(msg, "session", "seq", "t_play", "rr_ms", "hr_bpm", "quality")
    _session(msg)
    _count(msg, "seq")
    _count(msg, "t_play")
    _positive(msg, "rr_ms")
    _positive(msg, "hr_bpm")
    _one_of(msg, "quality", QUALITIES)


def _state(msg: dict, _direction: str) -> None:
    _fields(
        msg,
        "session", "seq", "t_engine", "t_session", "segment",
        "segment_elapsed_ms", "segment_nominal_ms", "psv", "confidence", "authority",
        "hr_bpm", "hr_base", "signal",
    )  # fmt: skip
    _session(msg)
    _count(msg, "seq")
    _count(msg, "t_engine")
    segment = _one_of(msg, "segment", SEGMENTS)
    if segment == "idle":
        if msg["t_session"] is not None:
            raise ContractError("state: t_session is null when segment is idle")
    else:
        _count(msg, "t_session")
    _count(msg, "segment_elapsed_ms")
    _count(msg, "segment_nominal_ms")
    for key in ("psv", "confidence", "authority"):
        _dimensions(msg, key)
    confidence, authority = msg["confidence"], msg["authority"]
    if confidence["valence"] != 0:
        raise ContractError("state: confidence.valence is always exactly 0.0")
    for dim in DIMENSIONS:
        if authority[dim] > confidence[dim]:
            raise ContractError(f"state: authority.{dim} exceeds confidence.{dim}")
        if authority[dim] > CEILINGS[segment][dim]:
            raise ContractError(
                f"state: authority.{dim} {authority[dim]} is over the {segment} ceiling "
                f"{CEILINGS[segment][dim]}"
            )
    if segment == "resolve":
        # The entry value is at most 1.0, so the taper bounds authority from the segment clock
        # alone: 1.0 at T-45 s, falling to 0 at T-12 s, then 0 until T.
        remaining = msg["segment_nominal_ms"] - RESOLVE_SILENT_MS - msg["segment_elapsed_ms"]
        bound = max(0.0, min(1.0, remaining / RESOLVE_TAPER_MS))
        for dim in DIMENSIONS:
            if authority[dim] > bound + AUTHORITY_ROUNDING:
                raise ContractError(
                    f"state: authority.{dim} {authority[dim]} is over the resolve taper "
                    f"{bound:.3f} at {msg['segment_elapsed_ms']} ms"
                )
    # hr_bpm is null before the first accepted interval, hr_base until baseline ends (§2, v1.1)
    # and for the rest of a degraded session (§2, v1.3).
    _positive(msg, "hr_bpm", nullable=True)
    _positive(msg, "hr_base", nullable=True)
    signal = msg["signal"]
    if not isinstance(signal, dict) or signal.keys() != {
        "contact",
        "rr_accepted_pct",
        "baseline_quality",
    }:
        raise ContractError("state: signal is {contact, rr_accepted_pct, baseline_quality}")
    if not isinstance(signal["contact"], bool):
        raise ContractError("state: signal.contact is true or false")
    for key in ("rr_accepted_pct", "baseline_quality"):
        if not _is_number(signal[key]) or not 0 <= signal[key] <= 1:
            raise ContractError(f"state: signal.{key} is a number from 0 to 1")


def _clock(msg: dict, direction: str) -> None:
    if direction == "out":
        _fields(msg, "role", "t_client_sent", "t_engine")
        _one_of(msg, "role", ("pong",))
        _count(msg, "t_engine")
    else:
        _fields(msg, "role", "t_client_sent")
        _one_of(msg, "role", ("ping",))
    # Client times may be fractional; laptop timestamps are whole milliseconds (§1, v1.1).
    _time(msg, "t_client_sent")


def _task_event(msg: dict, _direction: str) -> None:
    _fields(msg, "session", "t_client", "event", "dwell_ms", "split_interval_ms", "difficulty")
    _session(msg)
    _time(msg, "t_client")
    _one_of(msg, "event", TASK_EVENTS)
    _time(msg, "dwell_ms")
    _time(msg, "split_interval_ms")
    if not _is_number(msg["difficulty"]) or not 0 <= msg["difficulty"] <= 1:
        raise ContractError("task_event: difficulty is a number from 0 to 1")


def _hello(msg: dict, _direction: str) -> None:
    _fields(msg, "client", "build")
    _one_of(msg, "client", CLIENTS)
    if not isinstance(msg["build"], str) or not msg["build"]:
        raise ContractError("hello: build is a non-empty string")


_CHECKS = {
    "beat": _beat,
    "state": _state,
    "clock": _clock,
    "task_event": _task_event,
    "hello": _hello,
}

# --- field checks ---


def _fields(msg: dict, *names: str) -> None:
    expected = {"type", "v", *names}
    missing, extra = expected - msg.keys(), msg.keys() - expected
    if missing:
        raise ContractError(f"{msg['type']}: missing {', '.join(sorted(missing))}")
    if extra:
        # ascii(): error text reaches logs and close reasons, whatever a client put in a key.
        raise ContractError(f"{msg['type']}: unknown field {', '.join(map(ascii, sorted(extra)))}")


def _session(msg: dict) -> None:
    value = msg["session"]
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
        raise ContractError(f"{msg['type']}: session is S-YYYYMMDD-NNNN, not {value!a}")


def _count(msg: dict, key: str) -> None:
    value = msg[key]
    if not _is_int(value) or value < 0:
        raise ContractError(f"{msg['type']}: {key} is a whole number of 0 or more, not {value!a}")


def _time(msg: dict, key: str) -> None:
    value = msg[key]
    if not _is_number(value) or value < 0:
        raise ContractError(f"{msg['type']}: {key} is a number of 0 or more, not {value!a}")


def _positive(msg: dict, key: str, nullable: bool = False) -> None:
    value = msg[key]
    if value is None and nullable:
        return
    if not _is_number(value) or value <= 0:
        raise ContractError(f"{msg['type']}: {key} is a number above 0, not {value!a}")


def _one_of(msg: dict, key: str, options: tuple[str, ...]) -> str:
    value = msg[key]
    if value not in options:
        raise ContractError(f"{msg['type']}: {key} is one of {', '.join(options)}, not {value!a}")
    return value


def _dimensions(msg: dict, key: str) -> None:
    value = msg[key]
    if not isinstance(value, dict) or value.keys() != set(DIMENSIONS):
        raise ContractError(f"{msg['type']}: {key} has exactly {', '.join(DIMENSIONS)}")
    for dim in DIMENSIONS:
        if not _is_number(value[dim]) or not 0 <= value[dim] <= 1:
            raise ContractError(f"{msg['type']}: {key}.{dim} is a number from 0 to 1")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) <= MAX_SAFE_INT


def _is_number(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    return _is_int(value)  # math.isfinite would overflow on a huge int


def _no_constant(name: str) -> None:
    raise DecodeError(f"{name} is not a JSON number")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise DecodeError("a key appears twice in one object")
    return result
