"""What audio is fed on state: the PSV, session gain and heartbeat level (prompts 2.5-2.6).

PsvFeed. One mood override per state message, plus the two phase-timed gate crossings planned for
an aligned session, from the bridge's loop; it is the engine's only PSV writer (its inference
thread is never started). State messages go out every 2 s and at every segment boundary. Call
on_state with every one the session publishes, and tick at the send frames supplied by its gate
plan. Each call sends arousal, cognitive_load and readiness through
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
- A flip is scheduled at the stem's next boundary. A reversal before that boundary cancels
  cleanly, so only the actual 1.5 s ramp is held. Phase comes from the shim's rendered-frame
  counter, never from message time.
- Nested: air open implies pulse open. Pulse does not close under an air held open, and air does
  not open over a pulse held closed.

An aligned baseline plans pulse's opening one block before the load boundary. It also plans air's
close on the last air boundary before pulse's first regulate boundary, so their equal-length fades
always finish in the same order. Idle and reset send the baseline pose under either source; an
authority-zero body message would otherwise be neutral and open pulse between visitors.

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

HeartbeatLevel. The heartbeat layer's only level writer. Baseline starts at -18 dBFS and reaches
-13 over 12 s; load follows instantaneous HR from -13 at HR_base to -9 at HR_base + 15 bpm (or
holds -13 without a baseline); regulate first reaches the script's -9 start, then recedes to -11
over the nominal 75 s; resolve holds -11 and starts the shim's equal-power fade so its delayed
output runs from T-3 s to T. on_state stores resolve's exact end and tick issues the command at
T-3 s minus the limiter latency even when the 2 s state cadence misses it. It has the same
stop/restart latch as SessionGain and reconstructs a mid-segment envelope in two rendered stages.
On each state it drains every native onset record to the session log; the audio callback never
logs.

    phase = PhaseTracker.for_shim(shim)
    feed = PsvFeed(engine, log, phase=phase)
    gain, heartbeat = SessionGain(shim, log), HeartbeatLevel(shim, log)
    # every state message: publish(msg); feed.on_state(msg); gain.on_state(msg)
    #                      heartbeat.on_state(msg)
    # every bridge tick: heartbeat.tick(now)
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
from bridge.phase import (
    PULSE_ALIGNMENT_PHASE,
    SAMPLE_RATE,
    GateTiming,
    PhaseTracker,
    SessionGatePlan,
    frames_from_ms,
)
from bridge.poses import pose_inputs

SOURCES = ("body", "pose")

MARGIN = 0.01  # each side of a gate threshold
FADE_MS = 1_500  # the engine's gate ramp, PgaeOptions::crossfade_s
FADE_FRAMES = round(FADE_MS * SAMPLE_RATE / 1000.0)
LOOP_MS = {"pulse": 11_000, "air": 13_000}  # the stems' loops (CLAUDE.md, "Audio facts")
GATED = ("pulse", "air")  # outer to inner: air is only ever open inside an open pulse

# The session gain.
BETWEEN_VISITORS_RAMP_MS = 3_000.0  # idle and reset, and the fade at stop
SESSION_RAMP_MS = 2_000.0  # up to 1 as baseline begins
RESOLVE_FADE_FROM_END_MS = 22_000  # T-22 s: bed, sub and air start to leave
RESOLVE_SILENT_FROM_END_MS = 10_000  # T-10 s: the heartbeat layer alone
SOONER_MS = 100.0  # a same-target command resent only if it ends at least this much sooner

# The heartbeat level (experience script section 2).
HEARTBEAT_BASELINE_START_DBFS = -18.0
HEARTBEAT_BASELINE_END_DBFS = -13.0
HEARTBEAT_BASELINE_RAMP_MS = 12 * 1000.0
HEARTBEAT_LOAD_MAX_DBFS = -9.0
HEARTBEAT_LOAD_RISE_BPM = 15.0
HEARTBEAT_REGULATE_END_DBFS = -11.0
HEARTBEAT_LEVEL_SMOOTH_MS = 2_000.0
HEARTBEAT_FINAL_FADE_MS = 3_000.0
# The level is multiplied before the limiter. Fixed-timeline commands are issued this much early,
# so the audible result—not merely the mix input—lands on T_engine. Kept explicit here because
# importing/loading a native DLL at module import would make this pure controller environment-bound.
HEARTBEAT_CHAIN_LATENCY_FRAMES = 1_639
HEARTBEAT_CHAIN_LATENCY_MS = HEARTBEAT_CHAIN_LATENCY_FRAMES / 48.0
HEARTBEAT_RESTART_RAMP_MS = 100.0


class MoodSink(Protocol):
    def set_mood_override(self, arousal: float, cognitive_load: float, readiness: float) -> Any: ...


class GainSink(Protocol):
    def set_session_gain(self, target: float, ramp_ms: float) -> Any: ...


class HeartbeatSink(Protocol):
    def set_heartbeat_level(self, target_dbfs: float, ramp_ms: float) -> Any: ...


class EventLog(Protocol):
    def event(self, name: str, *, t_engine: float | None = None, **fields: Any) -> None: ...


def body_inputs(msg: Mapping) -> dict[str, float]:
    """The body source: each value pre-blended with the authority the message carries."""
    return {d: effective(msg["psv"][d], msg["authority"][d]) for d in DIMENSIONS}


@dataclass
class GateState:
    open: bool = False
    ramp_start_frame: int | None = None
    ramp_end_frame: int | None = None

    def held(self, frame: int) -> bool:
        """Only the audible 1.5 s ramp is protected; pending time before its boundary is not."""
        return (
            self.ramp_start_frame is not None
            and self.ramp_end_frame is not None
            and self.ramp_start_frame <= frame < self.ramp_end_frame
        )

    def ramp(self) -> tuple[int, int] | None:
        if self.ramp_start_frame is None or self.ramp_end_frame is None:
            return None
        return self.ramp_start_frame, self.ramp_end_frame


@dataclass(frozen=True)
class Update:
    """One override, as sent, and why."""

    t_engine: float
    frame: int
    reason: str
    source: str
    body: dict[str, float]  # what the body source gives for this message
    chosen: dict[str, float]  # what the active source gave, before hysteresis
    sent: dict[str, float]  # arousal, cognitive_load and readiness, as sent
    params: EngineParams  # the mirror's reading of what was sent
    gates: dict[str, bool]  # pulse and air after this message
    flipped: tuple[str, ...]  # gates this message flipped
    deferred: tuple[str, ...]  # gates that would have flipped, held back by nesting
    ramps: dict[str, tuple[int, int] | None]  # scheduled boundary and end, in scene frames

    @property
    def moved(self) -> bool:
        return self.sent != self.chosen


class GateHysteresis:
    """The Schmitt states of pulse and air, and the inputs that respect them."""

    def __init__(self, phase: PhaseTracker) -> None:
        self._phase = phase
        self.gates = {stem: GateState() for stem in GATED}

    def decide(
        self,
        values: Mapping[str, float],
        frame: int,
        forced: Mapping[str, bool] | None = None,
    ) -> tuple[dict[str, float], dict[str, bool], tuple[str, ...]]:
        """(values to send, gate states after them, deferred). Changes nothing."""
        d = density(values["arousal"], values["cognitive_load"])
        after: dict[str, bool] = {}
        for stem in GATED:
            gate, threshold = self.gates[stem], GATE_THRESHOLDS[stem]
            if gate.held(frame):
                after[stem] = gate.open
            elif forced is not None and stem in forced:
                after[stem] = forced[stem]
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

    def commit(self, after: Mapping[str, bool], frame: int) -> tuple[str, ...]:
        """Take the states from decide and record the engine's actual scheduled ramp."""
        flipped = []
        for stem in GATED:
            gate = self.gates[stem]
            if after[stem] != gate.open:
                gate.open = after[stem]
                gate.ramp_start_frame = self._phase.next_boundary(stem, frame)
                gate.ramp_end_frame = gate.ramp_start_frame + FADE_FRAMES
                flipped.append(stem)
        return tuple(flipped)


@dataclass(frozen=True)
class ScheduledGate:
    timing: GateTiming
    open: bool
    reason: str


class PsvFeed:
    """The engine's phase-aware PSV writer. See the module docstring."""

    def __init__(
        self,
        sink: MoodSink,
        log: EventLog,
        source: str = "body",
        *,
        phase: PhaseTracker,
        session_gates: bool = True,
    ) -> None:
        self._sink = sink
        self._log = log
        self._source = _check_source(source)
        self._phase = phase
        self._session_gates = session_gates
        self.hysteresis = GateHysteresis(phase)
        self._segment: str | None = None
        self._forced: dict[str, bool] = {}
        self._pending: list[ScheduledGate] = []
        self._plan: SessionGatePlan | None = None
        self._last: tuple[float, int, str, dict[str, float], dict[str, float]] | None = None
        self._armed_baseline_frame: int | None = None

    @property
    def source(self) -> str:
        return self._source

    def set_source(self, source: str, *, t_engine: float | None = None) -> None:
        """Feed the engine from `source` from the next state message on. Logged."""
        previous, self._source = self._source, _check_source(source)
        self._log.event("engine_psv_source", t_engine=t_engine, source=source, previous=previous)

    def arm_start_frame(self, baseline_frame: int) -> None:
        """Pin the next baseline's scene frame before Session.start does any file logging.

        The attendant countdown observes this exact frame.  Its state callback can run one audio
        block later under Windows scheduling, but the whole gate plan still belongs to the frame
        at which start fired.
        """
        if self._phase.phase_frames("pulse", baseline_frame) != PULSE_ALIGNMENT_PHASE:
            raise ValueError("the armed baseline frame is not pulse-aligned")
        self._armed_baseline_frame = baseline_frame

    def on_state(self, msg: Mapping) -> Update:
        """Send the override for one state message, and log it. msg is read, never changed."""
        t, source, frame = float(msg["t_engine"]), self._source, self._phase.frame()
        segment = msg["segment"]
        if self._session_gates and segment != self._segment:
            self._enter_segment(msg, frame)
            self._segment = segment
        body = body_inputs(msg)
        # Between visitors both source choices send the baseline pose.  In particular, a body
        # message with authority zero is neutral, and neutral would open pulse.
        baseline_pose = self._session_gates and segment in ("idle", "reset")
        chosen = pose_inputs(msg) if baseline_pose or source == "pose" else body
        chosen = {d: _unit(chosen[d]) for d in DIMENSIONS}
        update = self._send(
            t,
            frame,
            segment,
            body,
            chosen,
            reason="state",
            baseline_pose=baseline_pose,
        )
        self._last = t, frame, segment, body, chosen
        return update

    def tick(self, t_engine: float | None = None) -> tuple[Update, ...]:
        """Send any planned crossing whose one-block-early frame has arrived.

        The bridge loop schedules its wake-up from ``bridge.phase`` and calls this before the
        audio callback that begins at the returned send frame.  State messages remain on their
        ordinary cadence; only these two session crossings need a phase-timed call.
        """
        frame = self._phase.frame()
        if t_engine is None and self._last is not None:
            last_t, last_frame, _, _, _ = self._last
            t_engine = last_t + (frame - last_frame) * 1000.0 / SAMPLE_RATE
        return self._send_due(frame, t_engine)

    @property
    def gate_plan(self) -> SessionGatePlan | None:
        return self._plan

    def _enter_segment(self, msg: Mapping, frame: int) -> None:
        segment = msg["segment"]
        if segment in ("idle", "reset"):
            self._pending.clear()
            self._plan = None
            self._forced = {"pulse": False, "air": False}
        elif segment == "baseline":
            self._forced = {"pulse": False, "air": False}
            baseline = self._armed_baseline_frame
            self._armed_baseline_frame = None
            if baseline is None:
                baseline = frame - frames_from_ms(float(msg["segment_elapsed_ms"]))
            try:
                self._plan = self._phase.session_gate_plan(baseline)
            except ValueError:
                self._plan = None
                self._pending.clear()
                self._log.event(
                    "engine_phase_misaligned",
                    t_engine=msg["t_engine"],
                    frame=frame,
                    baseline_frame=baseline,
                    pulse_phase=self._phase.phase_frames("pulse", baseline),
                )
            else:
                self._pending = sorted(
                    [
                        ScheduledGate(self._plan.pulse_open, True, "load_pulse_open"),
                        ScheduledGate(self._plan.air_close, False, "regulate_air_close"),
                    ],
                    key=lambda item: item.timing.send_frame,
                )
                self._log.event(
                    "engine_gate_plan",
                    t_engine=msg["t_engine"],
                    baseline_frame=baseline,
                    load_frame=self._plan.load_frame,
                    regulate_frame=self._plan.regulate_frame,
                    pulse_open=_timing_log(self._plan.pulse_open),
                    air_close=_timing_log(self._plan.air_close),
                    pulse_close=_timing_log(self._plan.pulse_close),
                )
        elif segment == "load":
            self._forced["pulse"] = True
            # Baseline kept air closed.  Load may open it naturally until its planned pre-close.
            if not any(item.reason == "regulate_air_close" for item in self._pending):
                self._forced["air"] = False
            else:
                self._forced.pop("air", None)
        elif segment in ("regulate", "resolve"):
            self._forced = {"pulse": False, "air": False}

    def _send_due(self, frame: int, t_engine: float | None) -> tuple[Update, ...]:
        updates = []
        while self._pending and self._pending[0].timing.send_frame <= frame:
            event = self._pending.pop(0)
            self._forced[event.timing.stem] = event.open
            update = None
            event_t = t_engine
            if self._last is not None:
                last_t, last_frame, segment, body, chosen = self._last
                t = (
                    last_t + (frame - last_frame) * 1000.0 / SAMPLE_RATE
                    if t_engine is None
                    else float(t_engine)
                )
                event_t = t
                update = self._send(t, frame, segment, body, chosen, reason=event.reason)
                updates.append(update)
            ramp = self.hysteresis.gates[event.timing.stem].ramp()
            self._log.event(
                "engine_gate_crossing",
                t_engine=event_t,
                reason=event.reason,
                stem=event.timing.stem,
                open=event.open,
                frame=frame,
                send_frame=event.timing.send_frame,
                boundary_frame=event.timing.boundary_frame,
                late_frames=max(0, frame - event.timing.send_frame),
                missed_boundary=frame >= event.timing.boundary_frame,
                scheduled_ramp=None if ramp is None else list(ramp),
                sent=update is not None,
            )
        return tuple(updates)

    def _send(
        self,
        t: float,
        frame: int,
        segment: str,
        body: dict[str, float],
        chosen: dict[str, float],
        *,
        reason: str,
        baseline_pose: bool = False,
    ) -> Update:
        sent, after, deferred = self.hysteresis.decide(chosen, frame, self._forced)
        self._sink.set_mood_override(sent["arousal"], sent["cognitive_load"], sent["readiness"])
        flipped = self.hysteresis.commit(after, frame)
        params = map_effective(sent["arousal"], sent["cognitive_load"], sent["readiness"])
        update = Update(
            t_engine=t,
            frame=frame,
            reason=reason,
            source=self._source,
            body=body,
            chosen=chosen,
            sent=sent,
            params=params,
            gates=dict(after),
            flipped=flipped,
            deferred=deferred,
            ramps={stem: gate.ramp() for stem, gate in self.hysteresis.gates.items()},
        )
        self._log.event(
            "engine_psv",
            t_engine=t,
            frame=frame,
            reason=reason,
            segment=segment,
            source=self._source,
            baseline_pose=baseline_pose,
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
                "forced": dict(self._forced),
                "ramps": {
                    stem: None if ramp is None else list(ramp)
                    for stem, ramp in update.ramps.items()
                },
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


class HeartbeatLevel:
    """The heartbeat layer's scripted level and its only writer. See the module docstring."""

    def __init__(self, sink: HeartbeatSink, log: EventLog) -> None:
        self._sink = sink
        self._log = log
        self._holding = False
        self._segment: str | None = None
        self._target_dbfs: float | None = None
        self._resolve_end_ms: float | None = None
        self._resolve_fade_sent = False
        self._pending: tuple[float, float, float, str] | None = None
        self._restarting = False
        self._onset_measurements = 0
        self._telemetry_dropped = 0

    @property
    def holding(self) -> bool:
        return self._holding

    def fade_out(
        self, ramp_ms: float = HEARTBEAT_FINAL_FADE_MS, *, t_engine: float | None = None
    ) -> None:
        """Send the equal-power silence boundary and hold all state/tick targets until resume."""
        self._sink.set_heartbeat_level(-math.inf, ramp_ms)
        self._holding = True
        self._target_dbfs = -math.inf
        self._pending = None
        self._log.event(
            "heartbeat_level",
            t_engine=t_engine,
            stop=True,
            target_dbfs=None,
            silence=True,
            ramp_ms=ramp_ms,
        )

    def resume(self) -> None:
        """Release the stop latch; the next state reconstructs the live segment envelope."""
        self._holding = False
        self._segment = None
        self._target_dbfs = None
        self._resolve_end_ms = None
        self._resolve_fade_sent = False
        self._pending = None
        self._restarting = True

    def on_state(self, msg: Mapping) -> tuple[tuple[float, float], ...] | None:
        """Apply one state message. tick() handles resolve's exact T-3 s boundary."""
        self._report_onset_timing(msg)
        segment = msg["segment"]
        t = float(msg["t_engine"])
        elapsed = float(msg["segment_elapsed_ms"])
        nominal = float(msg["segment_nominal_ms"])
        if self._holding:
            self._log.event(
                "heartbeat_level_held",
                t_engine=t,
                segment=segment,
                segment_elapsed_ms=elapsed,
            )
            return None

        entered = segment != self._segment
        if entered:
            self._segment = segment
            self._resolve_end_ms = None
            self._resolve_fade_sent = False
            self._pending = None

        commands: list[tuple[float, float]] = []
        if not entered:
            self._send_pending_if_due(t, commands)
        restoring = self._restarting or (entered and elapsed > SOONER_MS)
        self._restarting = False
        if segment in ("idle", "reset"):
            self._send_if_changed(-math.inf, HEARTBEAT_FINAL_FADE_MS, t, segment, commands)
        elif segment == "baseline":
            if entered:
                self._enter_baseline(t, elapsed, restoring, commands)
        elif segment == "load":
            target = heartbeat_load_level_dbfs(msg.get("hr_bpm"), msg.get("hr_base"))
            self._send_if_changed(
                target, HEARTBEAT_LEVEL_SMOOTH_MS, t, segment, commands, tolerance=1e-6
            )
        elif segment == "regulate":
            if entered:
                self._enter_regulate(t, elapsed, nominal, restoring, commands)
        elif segment == "resolve":
            self._resolve_end_ms = t - elapsed + nominal
            if entered:
                if restoring and t >= self._resolve_fade_trigger_ms():
                    self._resume_resolve_fade(t, commands)
                else:
                    # Resolve holds -11 even if regulate ended early while its -11 target was
                    # still in flight. Reissuing it makes the actual level settle, not just the
                    # controller's last target value.
                    ramp = (
                        HEARTBEAT_RESTART_RAMP_MS if restoring else HEARTBEAT_LEVEL_SMOOTH_MS
                    )
                    self._send(HEARTBEAT_REGULATE_END_DBFS, ramp, t, segment, commands)
            self._fade_if_due(t, commands)
        else:
            raise ValueError(f"unknown segment {segment!r}")
        return tuple(commands) or None

    def tick(self, t_engine_ms: float) -> tuple[float, float] | None:
        """Run staged ramps and issue resolve's fade early enough to sound exactly at T-3 s."""
        if self._holding:
            return None
        commands: list[tuple[float, float]] = []
        t = float(t_engine_ms)
        self._send_pending_if_due(t, commands)
        self._fade_if_due(t, commands)
        return commands[0] if commands else None

    def _enter_baseline(
        self,
        t: float,
        elapsed: float,
        restoring: bool,
        commands: list[tuple[float, float]],
    ) -> None:
        remaining = max(0.0, HEARTBEAT_BASELINE_RAMP_MS - elapsed)
        if not restoring:
            self._send(HEARTBEAT_BASELINE_START_DBFS, 0.0, t, "baseline", commands)
            self._send(
                HEARTBEAT_BASELINE_END_DBFS,
                remaining if remaining > 0.0 else HEARTBEAT_LEVEL_SMOOTH_MS,
                t,
                "baseline",
                commands,
            )
            return
        if remaining <= HEARTBEAT_RESTART_RAMP_MS:
            self._send(HEARTBEAT_BASELINE_END_DBFS, remaining, t, "baseline", commands)
            return
        fraction = min(1.0, max(0.0, elapsed / HEARTBEAT_BASELINE_RAMP_MS))
        current = HEARTBEAT_BASELINE_START_DBFS + fraction * (
            HEARTBEAT_BASELINE_END_DBFS - HEARTBEAT_BASELINE_START_DBFS
        )
        self._send(current, HEARTBEAT_RESTART_RAMP_MS, t, "baseline", commands)
        end = t - elapsed + HEARTBEAT_BASELINE_RAMP_MS
        self._schedule(t + HEARTBEAT_RESTART_RAMP_MS, HEARTBEAT_BASELINE_END_DBFS, end, "baseline")

    def _enter_regulate(
        self,
        t: float,
        elapsed: float,
        nominal: float,
        restoring: bool,
        commands: list[tuple[float, float]],
    ) -> None:
        end = t - elapsed + nominal
        remaining = max(0.0, end - t)
        if remaining <= 0.0:
            self._send(HEARTBEAT_REGULATE_END_DBFS, 0.0, t, "regulate", commands)
            return
        if not restoring:
            transition = min(HEARTBEAT_LEVEL_SMOOTH_MS, remaining)
            if transition == remaining:
                self._send(HEARTBEAT_REGULATE_END_DBFS, remaining, t, "regulate", commands)
                return
            # The script says -9 -> -11, irrespective of the last load HR or degraded baseline.
            # First reach -9 smoothly; only after samples have run through that ramp may -11 be
            # queued, otherwise the second command would replace the first before it sounded.
            self._send(HEARTBEAT_LOAD_MAX_DBFS, transition, t, "regulate", commands)
            self._schedule(t + transition, HEARTBEAT_REGULATE_END_DBFS, end, "regulate")
            return

        if remaining <= HEARTBEAT_RESTART_RAMP_MS:
            self._send(HEARTBEAT_REGULATE_END_DBFS, remaining, t, "regulate", commands)
            return
        transition = min(HEARTBEAT_LEVEL_SMOOTH_MS, nominal)
        if elapsed <= transition or nominal <= transition:
            current = HEARTBEAT_LOAD_MAX_DBFS
        else:
            fraction = min(1.0, (elapsed - transition) / (nominal - transition))
            current = HEARTBEAT_LOAD_MAX_DBFS + fraction * (
                HEARTBEAT_REGULATE_END_DBFS - HEARTBEAT_LOAD_MAX_DBFS
            )
        self._send(current, HEARTBEAT_RESTART_RAMP_MS, t, "regulate", commands)
        self._schedule(t + HEARTBEAT_RESTART_RAMP_MS, HEARTBEAT_REGULATE_END_DBFS, end, "regulate")

    def _schedule(self, due: float, target_dbfs: float, end: float, segment: str) -> None:
        self._pending = (due, target_dbfs, end, segment)

    def _send_pending_if_due(
        self, t: float, commands: list[tuple[float, float]]
    ) -> None:
        pending = self._pending
        if pending is None or t < pending[0]:
            return
        self._pending = None
        _, target, end, segment = pending
        self._send(target, max(0.0, end - t), t, segment, commands)

    def _resolve_fade_trigger_ms(self) -> float:
        assert self._resolve_end_ms is not None
        return (
            self._resolve_end_ms
            - HEARTBEAT_FINAL_FADE_MS
            - HEARTBEAT_CHAIN_LATENCY_MS
        )

    def _fade_if_due(self, t: float, commands: list[tuple[float, float]]) -> None:
        end = self._resolve_end_ms
        if end is None or self._resolve_fade_sent or t < self._resolve_fade_trigger_ms():
            return
        self._resolve_fade_sent = True
        input_end = end - HEARTBEAT_CHAIN_LATENCY_MS
        self._send(-math.inf, max(0.0, input_end - t), t, "resolve", commands)

    def _resume_resolve_fade(
        self, t: float, commands: list[tuple[float, float]]
    ) -> None:
        """Restore the level already due at restart, then continue to silence without overwriting
        that restoration in the same audio block."""
        assert self._resolve_end_ms is not None
        input_end = self._resolve_end_ms - HEARTBEAT_CHAIN_LATENCY_MS
        remaining = max(0.0, input_end - t)
        self._resolve_fade_sent = True
        if remaining <= 0.0:
            self._send(-math.inf, 0.0, t, "resolve", commands)
            return
        restore = min(HEARTBEAT_RESTART_RAMP_MS, remaining / 2.0)
        due = t + restore
        fade_start = self._resolve_end_ms - HEARTBEAT_FINAL_FADE_MS
        audible_due = due + HEARTBEAT_CHAIN_LATENCY_MS
        progress = min(1.0, max(0.0, (audible_due - fade_start) / HEARTBEAT_FINAL_FADE_MS))
        amplitude = math.cos(0.5 * math.pi * progress)
        if amplitude <= 0.0:
            self._send(-math.inf, 0.0, t, "resolve", commands)
            return
        current_dbfs = HEARTBEAT_REGULATE_END_DBFS + 20.0 * math.log10(amplitude)
        self._send(current_dbfs, restore, t, "resolve", commands)
        self._schedule(due, -math.inf, input_end, "resolve")

    def _send_if_changed(
        self,
        target_dbfs: float,
        ramp_ms: float,
        t: float,
        segment: str,
        commands: list[tuple[float, float]],
        *,
        tolerance: float = 0.0,
    ) -> None:
        previous = self._target_dbfs
        same = (
            previous is not None
            and (
                (math.isinf(previous) and math.isinf(target_dbfs))
                or abs(previous - target_dbfs) <= tolerance
            )
        )
        if not same:
            self._send(target_dbfs, ramp_ms, t, segment, commands)

    def _send(
        self,
        target_dbfs: float,
        ramp_ms: float,
        t: float,
        segment: str,
        commands: list[tuple[float, float]],
    ) -> None:
        self._sink.set_heartbeat_level(target_dbfs, ramp_ms)
        self._target_dbfs = target_dbfs
        command = (target_dbfs, ramp_ms)
        commands.append(command)
        self._log.event(
            "heartbeat_level",
            t_engine=t,
            segment=segment,
            target_dbfs=None if math.isinf(target_dbfs) else target_dbfs,
            silence=math.isinf(target_dbfs),
            ramp_ms=ramp_ms,
        )

    def _report_onset_timing(self, msg: Mapping) -> None:
        drain_fn = getattr(self._sink, "drain_heartbeat_onsets", None)
        stats_fn = getattr(self._sink, "stats", None)
        if not callable(drain_fn):
            return
        records = tuple(drain_fn())
        stats = stats_fn() if callable(stats_fn) else None
        for record in records:
            self._onset_measurements += 1
            if isinstance(record, Mapping):
                t_play_ms, error_ms = record["t_play_ms"], record["error_ms"]
            else:
                t_play_ms, error_ms = record.t_play_ms, record.error_ms
            fields = {
                "measurement": self._onset_measurements,
                "scheduled_t_play_ms": t_play_ms,
                "error_ms": error_ms,
            }
            if stats is not None:
                fields.update(
                    anchor_error_ms=stats.device_anchor_error_ms,
                    anchor_slew_ms=stats.device_anchor_slew_ms,
                    device_clock_samples=stats.device_clock_samples,
                    device_clock_failures=stats.device_clock_failures,
                )
            self._log.event(
                "heartbeat_onset_timing",
                t_engine=msg["t_engine"],
                segment=msg["segment"],
                **fields,
            )
        if stats is not None:
            dropped = int(stats.heartbeat_onset_telemetry_dropped)
            if dropped > self._telemetry_dropped:
                self._log.event(
                    "heartbeat_onset_telemetry_dropped",
                    t_engine=msg["t_engine"],
                    segment=msg["segment"],
                    dropped=dropped,
                    new_dropped=dropped - self._telemetry_dropped,
                )
                self._telemetry_dropped = dropped


def heartbeat_load_level_dbfs(hr_bpm: object, hr_base: object) -> float:
    """Load's -13 to -9 dBFS mapping, held at -13 when either heart-rate value is absent."""
    if (
        isinstance(hr_bpm, bool)
        or isinstance(hr_base, bool)
        or not isinstance(hr_bpm, int | float)
        or not isinstance(hr_base, int | float)
    ):
        return HEARTBEAT_BASELINE_END_DBFS
    if not math.isfinite(hr_bpm) or not math.isfinite(hr_base):
        return HEARTBEAT_BASELINE_END_DBFS
    fraction = min(1.0, max(0.0, (float(hr_bpm) - float(hr_base)) / HEARTBEAT_LOAD_RISE_BPM))
    return HEARTBEAT_BASELINE_END_DBFS + fraction * (
        HEARTBEAT_LOAD_MAX_DBFS - HEARTBEAT_BASELINE_END_DBFS
    )


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


def _timing_log(timing: GateTiming) -> dict[str, int | str]:
    return {
        "stem": timing.stem,
        "send_frame": timing.send_frame,
        "boundary_frame": timing.boundary_frame,
    }
