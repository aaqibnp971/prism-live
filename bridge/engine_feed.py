"""What the engine is fed on every state message: the PSV (prompt 2.5) and the session gain.

PsvFeed. One mood override per state message, from the bridge's loop, the engine's only PSV
writer (its inference thread is never started). State messages go out every 2 s and at every
segment boundary, which is the cadence prompt 2.5 asks for, so call on_state with every one the
session publishes. Each call sends arousal, cognitive_load and readiness through
sink.set_mood_override; the sink sends valence 0.5, confidence 1.0 and mode_hint NULL with them
(bridge/engine.py). Two sources, switched at runtime by set_source from the bridge console, taking
effect on the next message:

- body: 0.5 + (psv - 0.5) x authority per dimension, both from the message. At confidence 1.0 the
  engine reads exactly that, which is per-dimension authority, reproduced. Authority is never
  recomputed: nothing here imports bridge/authority.py, not even through bridge/state.py.
- pose: bridge/poses.py.

The message is never modified. It keeps reporting the body's psv whatever the engine is fed.

Hysteresis comes after the source, on density d = 0.5 + 0.9 (a - 0.5) - (l - 0.5) of the values
about to be sent. The engine has none, and a gate that flips back inside the 1.5 s ramp its flip
scheduled parks the stem at a partial level until its next loop boundary (docs/engine-findings.md,
"Gate timing"). Per gate, pulse at T = 0.35 and air at T = 0.55, a Schmitt state:

- It opens when d >= T + MARGIN and closes when d <= T - MARGIN.
- What is sent never sits inside that band. A gate held open gets d >= T + MARGIN, held closed
  d <= T - MARGIN: arousal moves to the band's edge, and cognitive_load as well when arousal is
  pinned at 0 or 1. Readiness plays no part in density and never moves.
- After a flip the gate holds for its stem's loop + 1.5 s (pulse 12.5 s, air 14.5 s) of the
  messages' t_engine. The flip's ramp starts at the stem's next boundary, at most one loop away,
  and lasts 1.5 s, so no reversal is sent before that ramp is over.
- Nested: air open implies pulse open. Pulse does not close under an air held open, and air does
  not open over a pulse held closed.

The gate state mirrors the engine's, so it outlives a source switch and a session (the engine
keeps rendering between visitors), and it starts closed, as the engine's gates do.

SessionGain. The shim's session gain, which carries what no PSV can: silence before and after a
session, and resolve's ending, bed, sub and air to silence from T-22 s to T-10 s (findings Q1).
Per message: idle and reset, 0 over 3 s; baseline, load and regulate, 1 over 2 s; resolve, 1 until
T-22 s, then 0 with the ramp ending at T-10 s. Only a new target is sent, or the same target
arriving sooner than the ramp in flight: a stop during resolve's ending then fades in 3 s.

It is the session gain's only writer: once a SessionGain exists, nothing else calls
set_session_gain. Stopping the stream goes through it too. EngineHost.stop(gain) calls fade_out,
which sends 0 over 3 s and holds: every state message after it sends nothing, so nothing
re-targets the gain while the fade leaves the chain. EngineHost.start(gain) calls resume, and the
next message sends its target whatever was sent before.

Both log every update to the session log and are used from one thread, the bridge's loop.

    feed, gain = PsvFeed(engine, log), SessionGain(shim, log)
    # every state message: server.publish(msg); feed.on_state(msg); gain.on_state(msg)
    # the console: feed.set_source("pose")
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from bridge.engine_mapping import (
    CONFIDENCE_SENT,
    DIMENSIONS,
    GATE_THRESHOLDS,
    VALENCE_SENT,
    EngineParams,
    density,
    effective,
    map_effective,
)
from bridge.poses import pose_inputs

SOURCES = ("body", "pose")

MARGIN = 0.01  # each side of a gate threshold
FADE_MS = 1_500  # the engine's gate ramp, PgaeOptions::crossfade_s
LOOP_MS = {"pulse": 11_000, "air": 13_000}  # the stems' loops (CLAUDE.md, "Audio facts")
GATED = ("pulse", "air")  # outer to inner: air is only ever open inside an open pulse

# The session gain.
BETWEEN_VISITORS_RAMP_MS = 3_000.0  # idle and reset, and the fade at stop
SESSION_RAMP_MS = 2_000.0  # up to 1 as baseline begins
RESOLVE_FADE_FROM_END_MS = 22_000  # T-22 s: bed, sub and air start to leave
RESOLVE_SILENT_FROM_END_MS = 10_000  # T-10 s: the heartbeat layer alone
SOONER_MS = 100.0  # a same-target command resent only if it ends at least this much sooner


class MoodSink(Protocol):
    def set_mood_override(self, arousal: float, cognitive_load: float, readiness: float) -> Any: ...


class GainSink(Protocol):
    def set_session_gain(self, target: float, ramp_ms: float) -> Any: ...


class EventLog(Protocol):
    def event(self, name: str, *, t_engine: float | None = None, **fields: Any) -> None: ...


def body_inputs(msg: Mapping) -> dict[str, float]:
    """The body source: each value pre-blended with the authority the message carries."""
    return {d: effective(msg["psv"][d], msg["authority"][d]) for d in DIMENSIONS}


@dataclass
class GateState:
    open: bool = False
    held_until_ms: float | None = None  # no flip back before this t_engine

    def held(self, t: float) -> bool:
        return self.held_until_ms is not None and t < self.held_until_ms


@dataclass(frozen=True)
class Update:
    """One override, as sent, and why."""

    t_engine: float
    source: str
    body: dict[str, float]  # what the body source gives for this message
    chosen: dict[str, float]  # what the active source gave, before hysteresis
    sent: dict[str, float]  # arousal, cognitive_load and readiness, as sent
    params: EngineParams  # the mirror's reading of what was sent
    gates: dict[str, bool]  # pulse and air after this message
    flipped: tuple[str, ...]  # gates this message flipped
    deferred: tuple[str, ...]  # gates that would have flipped, held back by nesting
    held_until_ms: dict[str, float | None]  # per gate, no flip back before this t_engine

    @property
    def moved(self) -> bool:
        return self.sent != self.chosen


class GateHysteresis:
    """The Schmitt states of pulse and air, and the inputs that respect them."""

    def __init__(self) -> None:
        self.gates = {stem: GateState() for stem in GATED}

    def decide(
        self, values: Mapping[str, float], t: float
    ) -> tuple[dict[str, float], dict[str, bool], tuple[str, ...]]:
        """(values to send, gate states after them, deferred). Changes nothing."""
        d = density(values["arousal"], values["cognitive_load"])
        after: dict[str, bool] = {}
        for stem in GATED:
            gate, threshold = self.gates[stem], GATE_THRESHOLDS[stem]
            if gate.held(t):
                after[stem] = gate.open
            elif d >= threshold + MARGIN:
                after[stem] = True
            elif d <= threshold - MARGIN:
                after[stem] = False
            else:
                after[stem] = gate.open
        deferred: tuple[str, ...] = ()
        if after["air"] and not after["pulse"]:
            if self.gates["pulse"].open:
                # Air is held open, so pulse stays open under it until air may close.
                after["pulse"], deferred = True, ("pulse",)
            else:
                # Pulse is held closed, so air may not open over it.
                after["air"], deferred = False, ("air",)
        lo, hi = _band(after)
        arousal, load = _into_band(values["arousal"], values["cognitive_load"], lo, hi)
        sent = {"arousal": arousal, "cognitive_load": load, "readiness": values["readiness"]}
        return sent, after, deferred

    def commit(self, after: Mapping[str, bool], t: float) -> tuple[str, ...]:
        """Take the states decide gave, starting a hold on every flip. Returns the flips."""
        flipped = []
        for stem in GATED:
            gate = self.gates[stem]
            if after[stem] != gate.open:
                gate.open = after[stem]
                gate.held_until_ms = t + LOOP_MS[stem] + FADE_MS
                flipped.append(stem)
        return tuple(flipped)


class PsvFeed:
    """The engine's PSV, one override per state message. See the module docstring."""

    def __init__(self, sink: MoodSink, log: EventLog, source: str = "body") -> None:
        self._sink = sink
        self._log = log
        self._source = _check_source(source)
        self.hysteresis = GateHysteresis()

    @property
    def source(self) -> str:
        return self._source

    def set_source(self, source: str, *, t_engine: float | None = None) -> None:
        """Feed the engine from `source` from the next state message on. Logged."""
        previous, self._source = self._source, _check_source(source)
        self._log.event("engine_psv_source", t_engine=t_engine, source=source, previous=previous)

    def on_state(self, msg: Mapping) -> Update:
        """Send the override for one state message, and log it. msg is read, never changed."""
        t, source = msg["t_engine"], self._source
        body = body_inputs(msg)
        chosen = body if source == "body" else pose_inputs(msg)
        chosen = {d: _unit(chosen[d]) for d in DIMENSIONS}
        sent, after, deferred = self.hysteresis.decide(chosen, t)
        self._sink.set_mood_override(sent["arousal"], sent["cognitive_load"], sent["readiness"])
        flipped = self.hysteresis.commit(after, t)
        params = map_effective(sent["arousal"], sent["cognitive_load"], sent["readiness"])
        update = Update(
            t_engine=t,
            source=source,
            body=body,
            chosen=chosen,
            sent=sent,
            params=params,
            gates=dict(after),
            flipped=flipped,
            deferred=deferred,
            held_until_ms={s: g.held_until_ms for s, g in self.hysteresis.gates.items()},
        )
        self._log.event(
            "engine_psv",
            t_engine=t,
            segment=msg["segment"],
            source=source,
            sent={
                "arousal": sent["arousal"],
                "valence": VALENCE_SENT,
                "cognitive_load": sent["cognitive_load"],
                "readiness": sent["readiness"],
                "confidence": CONFIDENCE_SENT,
                "mode_hint": None,
            },
            body=body,
            input=chosen,
            hysteresis={
                "density_in": density(chosen["arousal"], chosen["cognitive_load"]),
                "density_sent": params.density,
                "moved": {d: [chosen[d], sent[d]] for d in DIMENSIONS if sent[d] != chosen[d]},
                "gates": dict(after),
                "flipped": list(flipped),
                "deferred": list(deferred),
                "held_until_ms": update.held_until_ms,
            },
            mapping=params.to_log(),
        )
        return update


class SessionGain:
    """The shim's session gain, from the segment clock, and its only writer: once one exists,
    nothing else calls set_session_gain. See the module docstring."""

    def __init__(self, sink: GainSink, log: EventLog) -> None:
        self._sink = sink
        self._log = log
        self._target: float | None = None
        self._ends_ms = math.inf
        self._holding = False

    @property
    def holding(self) -> bool:
        """Whether a fade_out is holding every state message back."""
        return self._holding

    def fade_out(
        self, ramp_ms: float = BETWEEN_VISITORS_RAMP_MS, *, t_engine: float | None = None
    ) -> None:
        """Send 0 over ramp_ms, log it, and hold: on_state sends nothing until resume. For
        EngineHost.stop, so no state message re-targets the gain before the stream stops."""
        self._sink.set_session_gain(0.0, ramp_ms)
        self._holding, self._target, self._ends_ms = True, 0.0, math.inf
        self._log.event("session_gain", t_engine=t_engine, stop=True, target=0.0, ramp_ms=ramp_ms)

    def resume(self) -> None:
        """Stop holding, and forget what was sent: the next state message sends its target.
        For EngineHost.start, whose stream may have stopped anywhere in a ramp."""
        self._holding, self._target, self._ends_ms = False, None, math.inf

    def on_state(self, msg: Mapping) -> tuple[float, float] | None:
        """Send (target, ramp_ms) if this message calls for a new command; None otherwise, and
        always None while a fade_out holds (logged as session_gain_held)."""
        target, ramp_ms = session_gain_for(msg)
        t = msg["t_engine"]
        if self._holding:
            self._log.event(
                "session_gain_held",
                t_engine=t,
                segment=msg["segment"],
                segment_elapsed_ms=msg["segment_elapsed_ms"],
                target=target,
                ramp_ms=ramp_ms,
            )
            return None
        if target == self._target and t + ramp_ms > self._ends_ms - SOONER_MS:
            return None
        self._sink.set_session_gain(target, ramp_ms)
        self._target, self._ends_ms = target, t + ramp_ms
        self._log.event(
            "session_gain",
            t_engine=t,
            segment=msg["segment"],
            segment_elapsed_ms=msg["segment_elapsed_ms"],
            target=target,
            ramp_ms=ramp_ms,
        )
        return target, ramp_ms


def session_gain_for(msg: Mapping) -> tuple[float, float]:
    """(target, ramp_ms) for one state message."""
    segment = msg["segment"]
    if segment in ("idle", "reset"):
        return 0.0, BETWEEN_VISITORS_RAMP_MS
    if segment == "resolve":
        nominal, elapsed = msg["segment_nominal_ms"], msg["segment_elapsed_ms"]
        if elapsed >= nominal - RESOLVE_FADE_FROM_END_MS:
            return 0.0, float(max(0, nominal - RESOLVE_SILENT_FROM_END_MS - elapsed))
    return 1.0, SESSION_RAMP_MS


def _band(gates: Mapping[str, bool]) -> tuple[float, float]:
    """The densities the gate states allow, both ends included."""
    if not gates["pulse"]:
        return -math.inf, GATE_THRESHOLDS["pulse"] - MARGIN
    if not gates["air"]:
        return GATE_THRESHOLDS["pulse"] + MARGIN, GATE_THRESHOLDS["air"] - MARGIN
    return GATE_THRESHOLDS["air"] + MARGIN, math.inf


def _into_band(arousal: float, load: float, lo: float, hi: float) -> tuple[float, float]:
    """Arousal, then load if arousal runs out, moved just far enough that density is in [lo, hi]."""
    d = density(arousal, load)
    if lo <= d <= hi:
        return arousal, load
    edge = lo if d < lo else hi
    # Before its clamp, density = 0.5 + 0.9 (a - 0.5) - (l - 0.5). Every edge is inside (0, 1).
    arousal = _step_into(
        lambda a: density(a, load), _unit(0.5 + (edge - 0.5 + (load - 0.5)) / 0.9), lo, hi, True
    )
    if lo <= density(arousal, load) <= hi:
        return arousal, load
    load = _step_into(
        lambda c: density(arousal, c),
        _unit(0.5 + (0.5 + 0.9 * (arousal - 0.5) - edge)),
        lo,
        hi,
        False,
    )
    if not lo <= density(arousal, load) <= hi:  # unreachable: density spans 0 to 1 over load
        raise AssertionError(f"no inputs give a density in [{lo}, {hi}]")
    return arousal, load


def _step_into(f, x: float, lo: float, hi: float, rising: bool) -> float:
    """x, stepped a few representable values toward [lo, hi] if rounding left f(x) just outside.
    Stops at 0 and 1."""
    for _ in range(64):
        y = f(x)
        if lo <= y <= hi:
            return x
        up = (y < lo) == rising
        step = math.nextafter(x, 1.0 if up else 0.0)
        if step == x:
            return x
        x = step
    return x


def _unit(x: float) -> float:
    """Into [0, 1], which the engine requires of every value; not a number is neutral."""
    return min(1.0, max(0.0, x)) if math.isfinite(x) else 0.5


def _check_source(source: str) -> str:
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, not {source!r}")
    return source
