"""The live bridge loop (prompt 2.9).

One asyncio event loop owns the packet source, beat scheduler, PSV model, session state machine,
engine controls and WebSocket publication.  None of those objects is called from another thread.
The default packet source is :class:`tools.synthetic_rr.SyntheticPacketSource`; prompt 2.8 swaps
that one construction line for ``BlePacketSource`` without changing this loop.

``Session`` remains the owner of state messages.  Its publish callback fans each state out to the
three engine controllers and then to ``LiveServer.publish``.  Beat events from both packet and
tick paths always pass through ``Session.beat_message`` before publication, so sequence numbers
restart with the session.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from statistics import median
from typing import Protocol

from bridge.beat_scheduler import INTERPOLATED, OK, BeatEvent, BeatScheduler
from bridge.clock import t_engine_ms
from bridge.contract import MIN_LEAD_MS, validate
from bridge.engine import PLS_BEAT_INTERPOLATED, PLS_BEAT_OK
from bridge.engine_feed import HeartbeatLevel, PsvFeed, SessionGain
from bridge.logging import SessionLog
from bridge.phase import SAMPLE_RATE, PhaseTracker, StartAlignment
from bridge.psv import PsvModel
from bridge.session import Session, Timings

MIN_TICK_MS = 20.0
MAX_TICK_MS = 100.0
# Windows rounds short asyncio waits coarsely on this machine: a nominal 20 ms lands around
# 31 ms.  Starting at the floor leaves enough room for logging/control stalls under the 100 ms cap.
DEFAULT_TICK_MS = MIN_TICK_MS
CONTROL_POLL_S = 0.005
START_SPIN_AHEAD_S = 0.050


class PacketSource(Protocol):
    """The whole live/synthetic boundary: each raw HRM packet, once, in arrival order."""

    def __aiter__(self) -> AsyncIterator[bytes]: ...


class BeatSink(Protocol):
    def push_beat(self, t_play_ms: float, rr_ms: float, quality: int = PLS_BEAT_OK) -> None: ...


Publish = Callable[[dict], bool | None]
Clock = Callable[[], float]


class LiveLoopError(RuntimeError):
    """A fault in the loop rather than an attendant refusal."""


@dataclass(frozen=True)
class Distribution:
    count: int
    minimum: float | None
    median: float | None
    p95: float | None
    maximum: float | None

    @classmethod
    def of(cls, values: list[float]) -> Distribution:
        if not values:
            return cls(0, None, None, None, None)
        ordered = sorted(values)
        return cls(
            len(ordered),
            ordered[0],
            median(ordered),
            _percentile(ordered, 0.95),
            ordered[-1],
        )

    def rounded(self) -> dict[str, int | float | None]:
        return {
            "count": self.count,
            "min": _round(self.minimum),
            "median": _round(self.median),
            "p95": _round(self.p95),
            "max": _round(self.maximum),
        }


@dataclass(frozen=True)
class SessionMetrics:
    session: str
    duration_ms: float
    tick_interval_ms: Distribution
    beat_lead_ms: Distribution

    def as_dict(self) -> dict:
        return {
            "session": self.session,
            "duration_ms": _round(self.duration_ms),
            "tick_target_ms": [MIN_TICK_MS, MAX_TICK_MS],
            "tick_interval_ms": self.tick_interval_ms.rounded(),
            "beat_lead_floor_ms": MIN_LEAD_MS,
            "beat_lead_ms": self.beat_lead_ms.rounded(),
        }


@dataclass
class _Measurement:
    session: str
    started_ms: float
    last_tick_ms: float | None = None
    tick_intervals_ms: list[float] = field(default_factory=list)
    beat_leads_ms: list[float] = field(default_factory=list)


class LiveLoop:
    """Run one clean bridge process.  Construct a new instance after a crash or restart."""

    def __init__(
        self,
        packet_source: PacketSource,
        publish: Publish,
        log: SessionLog,
        *,
        clock: Clock = t_engine_ms,
        tick_ms: float = DEFAULT_TICK_MS,
        timings: Timings | None = None,
        scheduler: BeatScheduler | None = None,
        model: PsvModel | None = None,
        psv_feed: PsvFeed | None = None,
        session_gain: SessionGain | None = None,
        heartbeat: HeartbeatLevel | None = None,
        beat_sink: BeatSink | None = None,
        phase: PhaseTracker | None = None,
    ) -> None:
        if (
            isinstance(tick_ms, bool)
            or not isinstance(tick_ms, int | float)
            or not math.isfinite(tick_ms)
            or not MIN_TICK_MS <= tick_ms <= MAX_TICK_MS
        ):
            raise ValueError(f"tick_ms must be from {MIN_TICK_MS:g} to {MAX_TICK_MS:g}")
        self.packet_source = packet_source
        self.publish = publish
        self.log = log
        self.clock = clock
        self.tick_ms = float(tick_ms)
        self.scheduler = scheduler or BeatScheduler()
        self.model = model or PsvModel()
        self.psv_feed = psv_feed
        self.session_gain = session_gain
        self.heartbeat = heartbeat
        self.beat_sink = beat_sink
        self.phase = phase
        production_timings = Timings(hold_ms=11_000, hold_to_end=True)
        self.session = Session(
            self.model,
            self.log,
            self._publish_state,
            now_ms=self.clock(),
            timings=timings or production_timings,
        )
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._start_lock: asyncio.Lock | None = None
        self.start_alignment: StartAlignment | None = None
        self._measurement: _Measurement | None = None
        self._completed: list[SessionMetrics] = []
        self._completed_event: asyncio.Event | None = None

    @property
    def completed_metrics(self) -> tuple[SessionMetrics, ...]:
        return tuple(self._completed)

    async def run(self) -> None:
        """Run until cancelled or a source/control fault occurs."""
        if self._running:
            raise RuntimeError("the live loop is already running")
        self._event_loop = asyncio.get_running_loop()
        self._start_lock = asyncio.Lock()
        self._completed_event = asyncio.Event()
        self._running = True
        tasks: list[asyncio.Task] = []
        try:
            # Establish idle (and silence in the engine controllers) before the packet task can
            # emit a beat. On restart this is the first outward description of the fresh process.
            self.session.tick(self.clock())
            tasks = [
                asyncio.create_task(self._packet_loop(), name="hrm packets"),
                asyncio.create_task(self._tick_loop(), name="session ticks"),
            ]
            if self.psv_feed is not None or self.heartbeat is not None:
                tasks.append(asyncio.create_task(self._control_loop(), name="engine controls"))
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._running = False
            self._event_loop = None

    async def attendant_start(self) -> str | None:
        """Arm on the local press, fire on the 2.7 pulse alignment, then call Session.start."""
        self._check_owner()
        assert self._start_lock is not None
        pressed_t = self.clock()
        self.log.event("attendant_start_pressed", t_engine=pressed_t)
        async with self._start_lock:
            # A second press while the first session is already running must be routed through
            # Session so its ordinary, logged ``running`` refusal is the single source of truth.
            if self.session.segment != "idle":
                return self._start_now(self.clock(), None)

            alignment = None if self.phase is None else self.phase.start_alignment()
            self.start_alignment = alignment
            wait_ms = 0.0 if alignment is None else alignment.wait_ms
            self.log.event(
                "attendant_start_armed",
                t_engine=pressed_t,
                wait_ms=wait_ms,
                pressed_frame=None if alignment is None else alignment.pressed_frame,
                fire_frame=None if alignment is None else alignment.fire_frame,
            )
            try:
                if alignment is not None:
                    await self._wait_for_frame(alignment.fire_frame)
                return self._start_now(self.clock(), alignment)
            except asyncio.CancelledError:
                self.log.event(
                    "attendant_start_cancelled",
                    t_engine=self.clock(),
                    waited_ms=max(0.0, self.clock() - pressed_t),
                )
                raise
            finally:
                self.start_alignment = None

    def attendant_stop(self) -> bool:
        """Route the local stop key straight to Session on this loop."""
        self._check_owner()
        self.log.event("attendant_stop_pressed", t_engine=self.clock())
        return self.session.stop(self.clock())

    def set_psv_source(self, source: str) -> None:
        """The local body/pose key.  There is deliberately no WebSocket equivalent."""
        self._check_owner()
        if self.psv_feed is None:
            raise RuntimeError("the engine PSV feed is not configured")
        self.psv_feed.set_source(source, t_engine=self.clock())

    def on_task_event(self, arrival_ms: float, msg: dict) -> bool:
        """Feed one validated task event to PSV, at its T_engine arrival time.

        The WebSocket server invokes this on the owning asyncio loop. Events for a previous session
        or outside LOAD are logged and ignored so a stale screen cannot change the next visitor.
        """
        if not self._running:
            self.log.event(
                "task_event_ignored",
                t_engine=arrival_ms,
                reason="loop_not_running",
            )
            return False
        self._check_owner()
        if msg["session"] != self.session.session:
            self.log.event(
                "task_event_ignored",
                t_engine=arrival_ms,
                reason="stale_session",
                message_session=msg["session"],
                current_session=self.session.session,
            )
            return False
        if self.session.segment != "load":
            self.log.event(
                "task_event_ignored",
                t_engine=arrival_ms,
                reason="not_load",
                segment=self.session.segment,
            )
            return False
        accepted = self.model.add_task_event(
            arrival_ms,
            msg["event"],
            msg["difficulty"],
            msg["dwell_ms"],
            msg["split_interval_ms"],
        )
        if not accepted:
            # The contract already checked the fields. Reaching this branch means T_engine moved
            # backwards or the model and contract have diverged, either of which is a loop fault.
            self.log.event(
                "task_event_ignored",
                t_engine=arrival_ms,
                reason="model_rejected",
                segment=self.session.segment,
            )
            raise LiveLoopError("the PSV model rejected a contract-valid task event")
        return True

    async def wait_for_signal(self, poll_s: float = 0.05) -> None:
        """Wait until Session sees a trusted beat.  Used by the one-session diagnostic."""
        self._check_owner()
        while self.session.signal_lost:
            await asyncio.sleep(poll_s)

    async def wait_for_completion(self, session: str) -> SessionMetrics:
        """Wait through reset until ``session`` has closed and return its live measurements."""
        self._check_owner()
        assert self._completed_event is not None
        while True:
            for metrics in self._completed:
                if metrics.session == session:
                    return metrics
            self._completed_event.clear()
            # Check again after clear so completion between the first check and clear is visible.
            if any(metrics.session == session for metrics in self._completed):
                continue
            await self._completed_event.wait()

    async def _packet_loop(self) -> None:
        async for payload in self.packet_source:
            if not isinstance(payload, bytes | bytearray | memoryview):
                raise TypeError("a packet source must yield raw bytes")
            now = self.clock()
            result = self.scheduler.on_packet(now, bytes(payload))
            self.model.on_packet(now, result)
            for event in result.events:
                self._publish_beat(event)

    async def _tick_loop(self) -> None:
        last_tick_ms: float | None = None
        while True:
            now = self.clock()
            if last_tick_ms is not None:
                while (remaining_ms := self.tick_ms - (now - last_tick_ms)) > 0.0:
                    # asyncio's Windows clock is coarser than T_engine's performance counter.
                    # Recheck T_engine after every wake so an early timer cannot make a short tick.
                    await asyncio.sleep(remaining_ms / 1000.0)
                    now = self.clock()
            last_tick_ms = now
            measurement = self._measurement
            if measurement is not None:
                if measurement.last_tick_ms is not None:
                    measurement.tick_intervals_ms.append(now - measurement.last_tick_ms)
                measurement.last_tick_ms = now
            for event in self.scheduler.tick(now):
                self._publish_beat(event)
            self.session.tick(now)

    async def _control_loop(self) -> None:
        while True:
            now = self.clock()
            if self.psv_feed is not None:
                self.psv_feed.tick(now)
            if self.heartbeat is not None:
                self.heartbeat.tick(now)
            await asyncio.sleep(CONTROL_POLL_S)

    def _publish_state(self, msg: dict) -> bool | None:
        validate(msg, "out")
        measurement = self._measurement
        if measurement is not None and msg["session"] != measurement.session:
            self._finish_measurement(self.clock())
        if self.psv_feed is not None:
            self.psv_feed.on_state(msg)
        if self.session_gain is not None:
            self.session_gain.on_state(msg)
        if self.heartbeat is not None:
            self.heartbeat.on_state(msg)
        return self.publish(msg)

    def _publish_beat(self, event: BeatEvent) -> None:
        msg = self.session.beat_message(event)
        validate(msg, "out")
        measurement = self._measurement
        if measurement is not None and msg["quality"] != "rejected":
            measurement.beat_leads_ms.append(float(msg["t_play"]) - self.clock())
        sent = self.publish(msg)
        if sent is False or msg["quality"] == "rejected" or self.beat_sink is None:
            return
        quality = {OK: PLS_BEAT_OK, INTERPOLATED: PLS_BEAT_INTERPOLATED}[msg["quality"]]
        self.beat_sink.push_beat(float(msg["t_play"]), float(msg["rr_ms"]), quality)

    def _start_now(self, now: float, alignment: object | None) -> str | None:
        target = getattr(alignment, "fire_frame", None)
        arm = None if self.psv_feed is None else getattr(self.psv_feed, "arm_start_frame", None)
        if target is not None and callable(arm):
            arm(target)
        candidate = _Measurement(self.session.session, now)
        refusal = self.session.start(now)
        frame = None if self.phase is None else self.phase.frame()
        self.log.event(
            "attendant_start_fired",
            t_engine=now,
            frame=frame,
            target_frame=target,
            late_frames=None if frame is None or target is None else max(0, frame - target),
            refusal=refusal,
        )
        if refusal == "bad_time":
            self.log.event(
                "live_loop_fault", t_engine=now, fault="bad_time passed to Session.start"
            )
            raise LiveLoopError("T_engine passed to Session.start is outside the link's range")
        if refusal is None:
            self._measurement = candidate
        return refusal

    async def _wait_for_frame(self, target: int) -> None:
        assert self.phase is not None
        while True:
            frame = self.phase.frame()
            if frame == target:
                return
            if frame > target:
                self.log.event(
                    "live_loop_fault",
                    t_engine=self.clock(),
                    fault="aligned start frame was missed",
                    target_frame=target,
                    frame=frame,
                )
                raise LiveLoopError(f"aligned start frame {target} was missed at frame {frame}")
            remaining = target - frame
            remaining_s = remaining / SAMPLE_RATE
            if remaining_s > START_SPIN_AHEAD_S:
                # Keep a wide margin for Windows' coarse asyncio timer before the short final
                # observation.  The final spin is bounded below the loop's 100 ms tick ceiling.
                await asyncio.sleep(remaining_s - START_SPIN_AHEAD_S)
                continue

            deadline = time.perf_counter() + remaining_s + 0.010
            while time.perf_counter() < deadline:
                # Keep local cancel/stop and session ticks responsive even in the final
                # alignment window. sleep(0) yields without a coarse Windows timer delay.
                await asyncio.sleep(0)
                frame = self.phase.frame()
                if frame == target:
                    return
                if frame > target:
                    break
            if frame > target:
                self.log.event(
                    "live_loop_fault",
                    t_engine=self.clock(),
                    fault="aligned start frame was missed",
                    target_frame=target,
                    frame=frame,
                )
                raise LiveLoopError(f"aligned start frame {target} was missed at frame {frame}")
            self.log.event(
                "live_loop_fault",
                t_engine=self.clock(),
                fault="engine frame counter stopped during aligned start",
                target_frame=target,
                frame=frame,
            )
            raise LiveLoopError("engine frame counter stopped during aligned start")

    def _finish_measurement(self, now: float) -> None:
        measurement = self._measurement
        if measurement is None:
            return
        metrics = SessionMetrics(
            measurement.session,
            now - measurement.started_ms,
            Distribution.of(measurement.tick_intervals_ms),
            Distribution.of(measurement.beat_leads_ms),
        )
        self._measurement = None
        self._completed.append(metrics)
        self.log.event("live_loop_metrics", t_engine=now, **metrics.as_dict())
        if self._completed_event is not None:
            self._completed_event.set()

    def _check_owner(self) -> None:
        if not self._running or asyncio.get_running_loop() is not self._event_loop:
            raise RuntimeError("call the live bridge from its owning asyncio loop")


def _percentile(ordered: list[float], fraction: float) -> float:
    position = (len(ordered) - 1) * fraction
    below = math.floor(position)
    above = math.ceil(position)
    if below == above:
        return ordered[below]
    weight = position - below
    return ordered[below] * (1.0 - weight) + ordered[above] * weight


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)
