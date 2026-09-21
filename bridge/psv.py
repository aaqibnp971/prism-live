"""The four PSV values and their confidences, read from one person's heartbeat.

What each value is, in this module and nowhere else:

- arousal: heart rate against the person's own baseline, plus RMSSD against its baseline once
  the part heart rate already explains is taken out. A z-score for heart rate: the change over
  the person's baseline hr_base, in a unit taken from their own baseline spread but held between
  4 and 6 bpm, because below 4 the spread of single beats is breathing, not a scale, and above 6
  it would quietly make high-variability people read calmer. RMSSD falls whenever heart rate
  rises, for arithmetic reasons alone, so the RMSSD term uses ln RMSSD + 2 ln HR, which moves only
  when variability changes for another reason. It carries 20 % of the weight until it has been
  checked on a real recording. 0.5 is the person at their own baseline, the engine's neutral.
- readiness: how much of the rise in heart rate has come back down, and HRV against the person's
  own baseline level (the same rate-corrected term, sign reversed). No comparison with other
  people, so nothing here scores anyone against a norm. "Recovery" is the share of the rise that
  has come back, not a slope: a 30 s slope follows the breath and steps on and off. Confidence
  never goes above 0.6; the experience script expects readiness to act weakly.
- cognitive_load: from task events first and heart rate second. Events are grouped into the
  opportunities opened by ``split``. A lock, an abandon, or a miss before 85 % of the advertised
  split interval says the person is still pursuing the target; a deadline miss without an abandon
  says disengagement, not overload. The gap since the last participant-driven event fades
  engagement after half an expected interval and to zero by one and a half. A gap in the whole
  event stream instead fades confidence, because a dead task screen is not evidence of
  disengagement. Raw dwell duration is deliberately not mapped: pointer and head control have
  different dwell distributions.
  Heart rate is only a 20 % secondary term after task evidence exists. On its own it is arousal
  again and leaves cognitive_load at 0.5 with confidence exactly 0.0.
- valence: 0.5 with confidence exactly 0.0, always. It cannot be read from a pulse. It is not a
  field of anything here: Values and Confidences answer it from module constants, and no
  constructor accepts it.

Confidence is per dimension, 0 to 1, a product of factors:

- the signal: the share of reported intervals the scheduler accepted over the last 60 s; silence
  on the link (held through the scheduler's interpolate_after_ms, falling to 0 at grace_ms); a
  recovery ramp after any gap, as long as twice the gap and at most 15 s, starting from where the
  silence had brought it; and the sensor-contact bit, falling to 0 over 2 s of false and ramping
  back the same way.
  - A new gap or contact loss during a recovery never raises the factor: it starts from where
    the recovery had got to. A silence that cost nothing starts no ramp at all.
  - A contact loss lasts until the last packet that reported it; a silence after that is the gap
    ramp's alone.
  - One lost packet leaves this factor alone; it costs only the window fill its beats took up.
  - The accepted share counts the scheduler's own verdicts. Intervals sent without contact are
    kept out of heart rate, HRV and the baseline, and the contact factor alone answers for them.
- window fill: heart rate over the 15 s before the newest accepted beat, and RMSSD differences
  over its 60 s window, the RMSSD part fading when classification falls 10 to 20 s behind.
- the baseline: while it is being captured, the capture's own progress p towards its quality
  gate, with the quality q its slope so far implies trusted more as the window fills:
  p x (1 - p^2 x (1 - q)). The bars climb from zero as the baseline fills, however long the
  armband was on before, and meet their final value without a step. A baseline still falling
  steeply climbs to about 0.38 and comes back down as its slope shows. After the capture, the
  factor is baseline_quality. Nothing relative to a baseline is trusted without one: before it
  starts, when its gate fails, and for the rest of a session marked degraded, those confidences
  are 0.

Values never jump to neutral when an input goes missing. Each part holds its last value while its
confidence falls. Every input is checked where it comes in; anything that is not a finite number
in range is treated as missing. Nothing raises on bad input and no NaN reaches an output.

Use one PsvModel per armband, from one thread (the bridge's asyncio loop). Estimates are
immutable and can be handed to any thread.

    model = PsvModel()
    model.on_packet(now, scheduler.on_packet(now, payload))  # every packet, in order
    model.start_baseline(t)  # session.py, when the baseline segment begins
    model.baseline  # the result once it is ready: hr_base for the end-of-baseline hold
    model.close_baseline(cap)  # session.py, when hr_base had not come by the end of the hold
    model.heart_rate(start, end), model.rmssd(start, end)  # session.py's windows
    model.estimate(now)  # for every state message
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import Enum

from bridge.baseline import Baseline, BaselineCapture
from bridge.beat_scheduler import Interval, PacketResult, Tuning
from bridge.hrv import (
    MIN_DIFFERENCES,
    WINDOW_MS,
    HrvInterval,
    HrvReading,
    IntervalCleaner,
    summarise,
)

# --- valence: not derivable from a pulse ---
VALENCE = 0.5
VALENCE_CONFIDENCE = 0.0
NEUTRAL = 0.5

# --- inputs ---
RR_RANGE_MS = (300.0, 2000.0)  # the scheduler's plausible range
TIME_RANGE_MS = (0.0, float(2**53))  # T_engine: milliseconds since the bridge started
HR_RANGE_BPM = (25.0, 250.0)
LN_RMSSD_RANGE = (0.0, 7.0)  # 1 ms to about 1,100 ms
TASK_EVENTS = ("split", "lock", "miss", "abandon")  # docs/message-contract-v1.md §3

# --- heart rate now ---
HR_WINDOW_MS = 15_000  # short enough to follow a 75 s load segment
HR_MIN_INTERVALS = 5
HR_MIN_COVERED_MS = 8_000
HR_STALE_FROM_MS = 5_000  # the scheduler's grace_ms: after this the newest beat is old news
HR_STALE_UNTIL_MS = 15_000
WIRE_HR_INTERVALS = 4  # hr_bpm for the state message until the 15 s rate exists

# --- arousal ---
HR_UNIT_BPM = (4.0, 6.0)  # the z unit: the person's baseline spread, held inside this range
AROUSAL_HR_WEIGHT = 0.8
AROUSAL_RMSSD_WEIGHT = 0.2  # small until checked against a real recording
AROUSAL_Z_SCALE = 3.5  # at the 4 bpm unit, +6 bpm reads 0.67, +15 reads 0.85, +37 reads 0.99
RATE_EXPONENT = 2.0  # ln RMSSD + 2 ln HR stays put when only heart rate moves
RMSSD_UNIT = math.log(1 / 0.8)  # a 20 % change: the experience script's load criterion
# ln RMSSD from n successive differences wanders by about 1/sqrt(2n), and neighbouring
# differences share a beat, which widens that by about 1.3. A thin baseline widens the unit.
RMSSD_SAMPLING_WIDENING = 1.3
RMSSD_BASE_FULL_DIFFERENCES = 20  # below this rmssd_base is often more than 20 % off
Z_LIMIT = 10.0

# --- readiness ---
READINESS_CAP = 0.6
READINESS_RECOVERY_SPAN = 0.35  # full recovery of a clear rise reads 0.85
READINESS_LEVEL_SPAN = 0.15
READINESS_RECOVERY_SHARE = 0.7  # of readiness confidence; the rest is the RMSSD part
RISE_MIN_BPM = 5.0  # recovery is measured against a rise of at least this
RISE_WEIGHT_BPM = (3.0, 8.0)  # a rise under 3 bpm says nothing about recovery; 8 is a clear one

# --- cognitive load ---
TASK_WEIGHT = 0.8  # task events first, heart rate second
TASK_WINDOW_MS = 30_000
TASK_FULL_OPPORTUNITIES = 8  # distinct rounds, not raw abandons from one input device
TASK_DEFAULT_INTERVAL_MS = 3_500
TASK_INTERVAL_RANGE_MS = (1_000.0, 10_000.0)
TASK_EARLY_MISS_FRACTION = 0.85
TASK_INTERACTION_FRESH_INTERVALS = (0.5, 1.5)
TASK_STREAM_FRESH_INTERVALS = (1.5, 4.0)
TASK_OPPORTUNITY_LIMIT = 128  # over three times the physical maximum in the 75 s task
TASK_DISENGAGED_VALUE = 0.20
TASK_ENGAGED_BASE = 0.25
TASK_DIFFICULTY_WEIGHT = 0.50
TASK_STRAIN_WEIGHT = 0.25
TASK_ABANDON_THEN_LOCK_STRAIN = 0.60
TASK_ACTIVE_ABANDON_STRAIN = 0.50

# --- confidence ---
ACCEPTED_WINDOW_MS = 60_000
ACCEPTED_RANGE = (0.6, 0.95)  # accepted share: 0 at or under the first, 1 at or over the second
RMSSD_FULL_DIFFERENCES = 40
RMSSD_STALE_MS = (10_000, 20_000)
RECOVERY_MAX_MS = 15_000
RECOVERY_PER_GAP = 2.0  # ramp back over twice the gap
CONTACT_FALL_MS = 2_000
CONFIDENCE_DECIMALS = 3  # rounded down, so an authority rounded later never exceeds it

# The most the mean confidence of arousal, cognitive_load and readiness reaches in baseline:
# arousal can reach 1, cognitive_load has no task events yet, and readiness has no recovery part
# yet. Valence is left out, its confidence being 0.0 by design. A settled baseline usually reaches
# this by its 45 s; docs/vr-handoff.md divides by it to drive the baseline world.
BASELINE_CONFIDENCE_MAX = (1.0 + 0.0 + READINESS_CAP * (1.0 - READINESS_RECOVERY_SHARE)) / 3


class BaselinePhase(Enum):
    IDLE = "idle"  # no baseline started in this session
    CAPTURING = "capturing"  # inside the 45 s window
    AWAITING = "awaiting"  # the window has closed; its result is not ready yet
    READY = "ready"  # the gate passed and hr_base exists
    FAILED = "failed"  # the gate failed: the attendant re-seats the armband and restarts
    DEGRADED = "degraded"  # session.py gave up waiting; nothing relative to a baseline is used


@dataclass(frozen=True)
class Dims:
    arousal: float
    cognitive_load: float
    readiness: float


@dataclass(frozen=True)
class Values(Dims):
    """The PSV. Valence is not stored: it is VALENCE, whatever anyone does."""

    @property
    def valence(self) -> float:
        return VALENCE

    def to_wire(self) -> dict[str, float]:
        return _wire(self, VALENCE)


@dataclass(frozen=True)
class Confidences(Dims):
    """Confidence per dimension. Valence is not stored: it is VALENCE_CONFIDENCE, exactly 0.0."""

    @property
    def valence(self) -> float:
        return VALENCE_CONFIDENCE

    def to_wire(self) -> dict[str, float]:
        return _wire(self, VALENCE_CONFIDENCE)


def _wire(dims: Dims, valence: float) -> dict[str, float]:
    return {
        "arousal": dims.arousal,
        "valence": valence,
        "cognitive_load": dims.cognitive_load,
        "readiness": dims.readiness,
    }


@dataclass(frozen=True)
class SignalQuality:
    accepted_fraction: float | None  # of intervals reported in the last 60 s; None if none were
    contact: bool | None  # the latest packet's contact bit; None when the sensor reports none
    silence_ms: float | None  # since the latest packet; None before the first
    factor: float  # the four below, multiplied
    accepted_factor: float
    silence_factor: float
    recovery_factor: float  # after a gap on the link
    contact_factor: float  # includes the ramp back after contact returns


@dataclass(frozen=True)
class HeartRateWindow:
    """Mean heart rate over a stretch of time, from the beats the baseline trusts."""

    bpm: float | None  # None when no beat fell in the window
    covered_ms: float  # how much of the window those beats' intervals cover
    intervals: int


@dataclass(frozen=True)
class TaskLoad:
    """What task events say about cognitive load, plus diagnostics written to the session log."""

    value: float
    confidence: float
    engagement: float = 0.0
    difficulty: float = 0.0
    strain: float = 0.0
    stream_freshness: float = 0.0
    opportunities: int = 0
    interaction_gap_ms: float | None = None


@dataclass
class _TaskOpportunity:
    """One split and what happened before the next split."""

    start_ms: float
    difficulty: float
    interval_ms: float
    abandoned: bool = False
    outcome: str | None = None  # lock, engaged_miss, or timeout


@dataclass(frozen=True)
class PsvEstimate:
    t_ms: float
    psv: Values
    confidence: Confidences
    phase: BaselinePhase
    hr_bpm: float | None  # the latest rate, held; None only before the first accepted interval
    signal: SignalQuality
    components: tuple[tuple[str, float | None], ...]  # for the session log, never the wire

    def log_fields(self) -> dict[str, float | str | None]:
        """Everything behind this estimate, as JSON-safe values."""
        return {"phase": self.phase.value, **dict(self.components)}


@dataclass(frozen=True)
class _Ramp:
    """A factor climbing linearly from start to 1 over duration_ms, from from_ms on."""

    from_ms: float
    start: float
    duration_ms: float

    def at(self, now_ms: float) -> float:
        if self.duration_ms <= 0:
            return 1.0
        done = min(1.0, max(0.0, (now_ms - self.from_ms) / self.duration_ms))
        return self.start + (1.0 - self.start) * done

    @staticmethod
    def after(previous: _Ramp | None, now_ms: float, start: float, duration_ms: float) -> _Ramp:
        """A new ramp that never starts above, or finishes before, the one still running."""
        if previous is not None:
            start = min(start, previous.at(now_ms))
            duration_ms = max(duration_ms, previous.from_ms + previous.duration_ms - now_ms)
        return _Ramp(now_ms, start, duration_ms)


class PsvModel:
    """One armband's heartbeat, turned into a PSV with confidences. Feed every packet."""

    def __init__(self, tuning: Tuning | None = None) -> None:
        self._tuning = tuning if isinstance(tuning, Tuning) else Tuning()
        # The sensor's history. It survives a new session: the armband stays on between them.
        self._cleaner = IntervalCleaner()
        self._clean: deque[HrvInterval] = deque()  # HRV-clean intervals, for RMSSD
        self._trusted: deque[tuple[float, float]] = deque()  # (t_beat, rr_ms), for heart rate
        self._reported: deque[tuple[float, bool]] = deque()  # (arrival, accepted)
        self._last_arrival: float | None = None
        self._last_t_beat = -math.inf
        self._contact: bool | None = None
        self._contact_false_since: float | None = None
        self._contact_fall_from = 1.0  # the contact factor when the current loss began
        self._contact_false_last = 0.0  # the latest packet that reported no contact
        self._contact_ramp: _Ramp | None = None
        self._gap_ramp: _Ramp | None = None
        self._hr_now: float | None = None  # held through gaps
        self._wire_rr: deque[float] = deque(maxlen=WIRE_HR_INTERVALS)  # bootstrap included
        self._rmssd_differences = 0
        self._rmssd_end_ms: float | None = None
        self._start_session(None)

    # --- control, from session.py ---

    def start_baseline(self, t_ms: float) -> bool:
        """Begin a session's 45 s baseline at t_ms (T_engine). Forgets the previous session."""
        t = _num(t_ms, *TIME_RANGE_MS)
        if t is None:
            return False
        self._start_session(BaselineCapture(t))
        return True

    def reset(self) -> None:
        """Between visitors: forget the session, keep the armband's history."""
        self._start_session(None)

    def mark_baseline_degraded(self) -> bool:
        """No hr_base in time. Terminal for the session: a result that arrives later is ignored."""
        if self._phase in (BaselinePhase.READY, BaselinePhase.FAILED):
            return False
        self._phase = BaselinePhase.DEGRADED
        return True

    def close_baseline(self, as_of_ms: float) -> BaselinePhase:
        """The end-of-baseline hold ended at as_of_ms, T_engine, with no result by then
        (session.py). The quality gate is judged on what had been classified by as_of_ms: when that
        fails it, the baseline FAILED, which is the re-seat path, with the problems in `baseline`;
        when it passes, only hr_base was late, and the session is DEGRADED. A result that came
        after as_of_ms is set aside, so the verdict does not depend on when this is called. A result
        in by as_of_ms, a window still open at it, and a degraded or absent baseline are left alone.
        Returns the phase."""
        as_of, capture = _num(as_of_ms, *TIME_RANGE_MS), self._capture
        decided = self._baseline_decided_ms
        if (
            as_of is None
            or capture is None
            or as_of < capture.end_ms
            or self._phase is BaselinePhase.DEGRADED
            or (decided is not None and decided <= as_of)
        ):
            return self._phase
        so_far = capture.result(as_of)
        if so_far.passed:
            self._baseline, self._baseline_decided_ms = None, None
            self._phase = BaselinePhase.DEGRADED
        else:
            self._baseline, self._baseline_decided_ms = so_far, as_of
            self._phase = BaselinePhase.FAILED
        return self._phase

    @property
    def baseline_decided_ms(self) -> float | None:
        """When the baseline became READY or FAILED, T_engine: the arrival of the packet that
        completed it, or the end of the hold that failed it. None before, and when degraded."""
        return self._baseline_decided_ms

    @property
    def baseline(self) -> Baseline | None:
        """The baseline result once it is ready, passed or not; None before, and when degraded."""
        return self._baseline if self._phase is not BaselinePhase.DEGRADED else None

    def add_task_event(
        self,
        t_engine_ms: float,
        event: str,
        difficulty: float,
        dwell_ms: float | None = None,
        split_interval_ms: float | None = None,
    ) -> bool:
        """Add one contract task event, stamped with its bridge-arrival time on T_engine.

        Arrival order is part of the evidence: a timeout ``miss`` immediately before the next
        ``split`` is different from an early wrong-target miss. WebSocket preserves order, so a
        backwards arrival time is rejected rather than silently reordering the stream.
        """
        t, level = _num(t_engine_ms, *TIME_RANGE_MS), _num(difficulty, 0.0, 1.0)
        if t is None or level is None or not isinstance(event, str) or event not in TASK_EVENTS:
            return False
        if self._task_last_arrival_ms is not None and t < self._task_last_arrival_ms:
            return False
        if dwell_ms is not None and _num(dwell_ms, 0.0, 60_000.0) is None:
            return False
        if split_interval_ms is not None and _num(split_interval_ms, 0.0, 60_000.0) is None:
            return False

        current = self._task_current
        if event == "split":
            # A missing timeout immediately before a split must not leave the preceding round
            # unresolved forever. Preserve any real abandon time; the automatic boundary is not
            # participant activity.
            if current is not None and current.outcome is None:
                current.outcome = "engaged_miss" if current.abandoned else "timeout"
            current = _TaskOpportunity(t, level, _task_interval(split_interval_ms))
            self._task_opportunities.append(current)
            self._task_current = current
            self._task_opportunity_count += 1
            self._task_latest_interval_ms = current.interval_ms
        elif current is None or current.outcome is not None:
            # Orphan and post-resolution events cannot refresh engagement or confidence.
            return False
        elif event == "abandon":
            current.abandoned = True
            self._task_last_interaction_ms = t
        elif event == "lock":
            current.outcome = "lock"
            self._task_last_interaction_ms = t
        else:  # miss
            early = t - current.start_ms < TASK_EARLY_MISS_FRACTION * current.interval_ms
            current.outcome = "engaged_miss" if current.abandoned or early else "timeout"
            # A wrong-target dwell is participant activity. An automatic deadline following an
            # earlier abandon is not: retaining the abandon's timestamp lets the gap distinguish
            # continued pursuit from somebody who stopped trying.
            if early:
                self._task_last_interaction_ms = t

        # Dwell is deliberately not part of the mapping. The validation above only keeps direct
        # callers as strict as the live path, which has already passed the message contract.
        self._task_last_arrival_ms = t
        self._task_last_event_ms = t
        if self._task_first_event_ms is None:
            self._task_first_event_ms = t
        self._task_latest_difficulty = level
        self._task_event_count += 1
        return True

    # --- windows, for the session state machine ---

    def heart_rate(self, start_ms: float, end_ms: float) -> HeartRateWindow:
        """Mean heart rate over (start_ms, end_ms]: accepted, non-bootstrap beats sent with
        contact, the same set hr_base comes from. Known as soon as the beats arrive. Covers the
        two minutes before the newest beat."""
        start, end = _num(start_ms), _num(end_ms)
        if start is None or end is None or end <= start:
            return HeartRateWindow(None, 0.0, 0)
        count, total, covered = 0, 0.0, 0.0
        for t_beat, rr in self._trusted:
            if start < t_beat <= end:
                count += 1
                total += rr
                covered += max(0.0, t_beat - max(t_beat - rr, start))
        bpm = _num(60_000 * count / total, *HR_RANGE_BPM) if count and total > 0 else None
        return HeartRateWindow(bpm, covered, count)

    @property
    def last_trusted_beat_ms(self) -> float | None:
        """The newest accepted, non-bootstrap beat sent with contact, T_engine; None before one."""
        return self._trusted[-1][0] if self._trusted else None

    def rmssd(self, start_ms: float, end_ms: float) -> HrvReading | None:
        """RMSSD from HRV-clean successive differences over (start_ms, end_ms]. None until
        classification has passed end_ms, 3 to 7.5 s after it. Covers about the last two minutes."""
        start, end = _num(start_ms), _num(end_ms)
        horizon = self._cleaner.horizon_ms
        if start is None or end is None or horizon is None or horizon < end:
            return None
        return summarise(self._clean, start, end, MIN_DIFFERENCES)

    # --- the armband ---

    def on_packet(self, now_ms: float, result: PacketResult) -> None:
        """Take one packet's result from the beat scheduler, at its arrival time."""
        now = _num(now_ms, *TIME_RANGE_MS)
        if now is None or not isinstance(result, PacketResult):
            return
        tuning = self._tuning
        if self._last_arrival is None:
            self._gap_ramp = _Ramp(now, 0.0, RECOVERY_MAX_MS)
        else:
            now = max(now, self._last_arrival)
            gap = now - self._last_arrival
            silence = self._silence_factor(gap)
            if gap > tuning.link_timeout_ms and silence < 1.0:
                # Where the silence left the factor: what it cost, times any recovery under way.
                under_way = self._gap_ramp.at(now) if self._gap_ramp else 1.0
                length = min(RECOVERY_MAX_MS, RECOVERY_PER_GAP * gap)
                self._gap_ramp = _Ramp.after(self._gap_ramp, now, silence * under_way, length)

        contact = result.contact if isinstance(result.contact, bool) else None
        if contact is False:
            if self._contact_false_since is None:
                self._contact_false_since = now
                self._contact_fall_from = self._contact_ramp.at(now) if self._contact_ramp else 1.0
            self._contact_false_last = now
        elif self._contact_false_since is not None:
            # Known only up to the last packet that said so; a silence after it is the gap ramp's.
            lasted = self._contact_false_last - self._contact_false_since
            fallen = self._contact_fall_from * _contact_fall(lasted)
            length = min(RECOVERY_MAX_MS, RECOVERY_PER_GAP * lasted)
            self._contact_ramp = _Ramp.after(self._contact_ramp, now, fallen, length)
            self._contact_false_since = None
        self._contact = contact

        fed: list[Interval] = []
        intervals = result.intervals if isinstance(result.intervals, tuple | list) else ()
        for raw in intervals:
            interval, scheduler_accepted = self._checked(raw, now, contact)
            self._reported.append((now, scheduler_accepted))
            if interval is None:
                continue
            fed.append(interval)
            self._last_t_beat = interval.t_beat
            if interval.accepted:
                self._wire_rr.append(interval.rr_ms)
                if not interval.bootstrap:
                    self._trusted.append((interval.t_beat, interval.rr_ms))
        self._last_arrival = now

        classified = self._cleaner.add(fed)
        self._clean.extend(c for c in classified if c.clean)
        if self._capture is not None:
            self._capture.add(classified, now)
        self._prune(now)
        self._update_heart_rate()
        self._update_baseline(now)
        self._update_rmssd(now)

    # --- the estimate ---

    def estimate(self, now_ms: float) -> PsvEstimate:
        """The PSV at now_ms. Reads state and changes none, so reading never alters the result."""
        now = _num(now_ms, *TIME_RANGE_MS)
        if self._last_arrival is not None:
            now = self._last_arrival if now is None else max(now, self._last_arrival)
        elif now is None:
            now = 0.0
        phase = self._phase_at(now)
        signal = self._signal(now)
        baseline = self._baseline if phase is BaselinePhase.READY else None
        hr_base = _num(baseline.hr_base_bpm, *HR_RANGE_BPM) if baseline else None
        ready = hr_base is not None  # a baseline to measure against, and one that makes sense

        if phase in _CAPTURE_PHASES:
            # A slope over the first 20 or 30 s is mostly noise: trust it as the window fills,
            # fully only at the end, where it meets baseline_quality.
            progress = self._capture.progress()
            quality = self._capture.provisional_quality()
            quality = 1.0 if quality is None else quality
            baseline_factor = progress * (1.0 - progress**2 * (1.0 - quality))
        elif ready:
            baseline_factor = _unit(_num(baseline.baseline_quality, 0.0, 1.0), 0.0)
        else:
            baseline_factor = 0.0

        hr_fill = self._hr_fill(now)
        rmssd_fill = min(1.0, self._rmssd_differences / RMSSD_FULL_DIFFERENCES)
        if self._rmssd_end_ms is not None:
            rmssd_fill *= 1.0 - _ramp(now - self._rmssd_end_ms, *RMSSD_STALE_MS)
        else:
            rmssd_fill = 0.0

        z_hr = rise = rise_weight = recovery = hr_unit = None
        if ready:
            hr_unit = min(max(_num(baseline.hr_sd_bpm) or 0.0, HR_UNIT_BPM[0]), HR_UNIT_BPM[1])
            if self._hr_now is not None:
                z_hr = _clip((self._hr_now - hr_base) / hr_unit, Z_LIMIT)
            if self._peak is not None and self._hr_now is not None:
                rise = self._peak - hr_base
                rise_weight = _ramp(rise, *RISE_WEIGHT_BPM)
                recovery = min(1.0, max(0.0, (self._peak - self._hr_now) / max(RISE_MIN_BPM, rise)))
            base_differences = _num(baseline.rmssd_base_differences, 0.0) or 0.0
            base_trust = min(1.0, base_differences / RMSSD_BASE_FULL_DIFFERENCES)
            rmssd_part = rmssd_fill * base_trust if self._z_rmssd_now else 0.0
        elif phase in _CAPTURE_PHASES:
            # The same trust the result will give rmssd_base, from the tail captured so far.
            tail_differences = self._capture.provisional_rmssd_differences()
            enough = tail_differences >= MIN_DIFFERENCES
            base_trust = min(1.0, tail_differences / RMSSD_BASE_FULL_DIFFERENCES) if enough else 0.0
            rmssd_part = rmssd_fill * base_trust
        else:
            rmssd_part = 0.0
        z_rmssd = self._z_rmssd if ready else None

        arousal = readiness = NEUTRAL
        if ready:
            z = AROUSAL_HR_WEIGHT * (z_hr or 0.0) + AROUSAL_RMSSD_WEIGHT * (z_rmssd or 0.0)
            arousal = NEUTRAL + 0.5 * math.tanh(z / AROUSAL_Z_SCALE)
            readiness = (
                NEUTRAL
                + READINESS_RECOVERY_SPAN * (rise_weight or 0.0) * (recovery or 0.0)
                + READINESS_LEVEL_SPAN * math.tanh(-(z_rmssd or 0.0))
            )
        scale = signal.factor * baseline_factor
        task_load = self._task_load(now)
        load, load_confidence = blend_cognitive_load(
            task_load,
            NEUTRAL + 0.5 * math.tanh((z_hr or 0.0) / AROUSAL_Z_SCALE),
            scale * hr_fill,
        )
        arousal_confidence = scale * (
            AROUSAL_HR_WEIGHT * hr_fill + AROUSAL_RMSSD_WEIGHT * rmssd_part
        )
        readiness_confidence = (
            scale
            * READINESS_CAP
            * (
                READINESS_RECOVERY_SHARE * (rise_weight or 0.0) * hr_fill
                + (1.0 - READINESS_RECOVERY_SHARE) * rmssd_part
            )
        )

        components = (
            ("hr_now_bpm", self._hr_now),
            ("hr_base_bpm", hr_base),
            ("hr_unit_bpm", hr_unit),
            ("z_hr", z_hr),
            ("z_rmssd", z_rmssd),
            ("peak_bpm", self._peak if ready else None),
            ("rise_bpm", rise),
            ("rise_weight", rise_weight),
            ("recovery", recovery),
            ("baseline_factor", baseline_factor),
            ("hr_fill", hr_fill),
            ("rmssd_fill", rmssd_fill),
            ("rmssd_differences", float(self._rmssd_differences)),
            ("signal_factor", signal.factor),
            ("accepted_factor", signal.accepted_factor),
            ("silence_factor", signal.silence_factor),
            ("recovery_factor", signal.recovery_factor),
            ("contact_factor", signal.contact_factor),
            ("task_events", float(self._task_event_count)),
            ("task_load", task_load.value if task_load else None),
            ("task_load_confidence", task_load.confidence if task_load else None),
            ("task_engagement", task_load.engagement if task_load else None),
            ("task_difficulty", task_load.difficulty if task_load else None),
            ("task_strain", task_load.strain if task_load else None),
            ("task_stream_freshness", task_load.stream_freshness if task_load else None),
            ("task_opportunities", float(task_load.opportunities) if task_load else None),
            ("task_interaction_gap_ms", task_load.interaction_gap_ms if task_load else None),
        )
        return PsvEstimate(
            t_ms=now,
            psv=Values(
                arousal=_unit(arousal, NEUTRAL),
                cognitive_load=_unit(load, NEUTRAL),
                readiness=_unit(readiness, NEUTRAL),
            ),
            confidence=Confidences(
                arousal=_confidence(arousal_confidence),
                cognitive_load=_confidence(load_confidence),
                readiness=_confidence(readiness_confidence),
            ),
            phase=phase,
            hr_bpm=self._wire_heart_rate(),
            signal=signal,
            components=tuple((name, _finite(value)) for name, value in components),
        )

    # --- internals ---

    def _start_session(self, capture: BaselineCapture | None) -> None:
        self._capture = capture
        self._baseline: Baseline | None = None
        self._baseline_decided_ms: float | None = None
        self._phase = BaselinePhase.IDLE if capture is None else BaselinePhase.CAPTURING
        self._peak: float | None = None  # the highest heart rate since the baseline was ready
        self._z_rmssd: float | None = None  # held
        self._z_rmssd_now = False  # whether the latest packet could compute it
        # Store the event grammar, not every raw event. Head pose can emit far more abandons than
        # a mouse; coalescing them into their opportunity prevents input frequency from evicting
        # split boundaries or buying confidence.
        self._task_opportunities: deque[_TaskOpportunity] = deque(maxlen=TASK_OPPORTUNITY_LIMIT)
        self._task_current: _TaskOpportunity | None = None
        self._task_event_count = 0
        self._task_opportunity_count = 0
        self._task_first_event_ms: float | None = None
        self._task_last_event_ms: float | None = None
        self._task_last_arrival_ms: float | None = None
        self._task_last_interaction_ms: float | None = None
        self._task_latest_difficulty = 0.0
        self._task_latest_interval_ms = TASK_DEFAULT_INTERVAL_MS

    def _checked(
        self, raw: object, now: float, contact: bool | None
    ) -> tuple[Interval | None, bool]:
        """A clean copy of one interval, or None when it cannot be placed on the timeline, and
        whether the scheduler accepted it."""
        if not isinstance(raw, Interval):
            return None, False
        # The scheduler never places a beat after its packet arrived.
        t_beat, rr = _num(raw.t_beat, TIME_RANGE_MS[0], now), _num(raw.rr_ms)
        if t_beat is None or rr is None:
            return None, False
        # A zero or backwards interval is still a rejection the cleaner must guard around.
        t_beat = max(t_beat, self._last_t_beat + 1.0)
        rr = min(max(rr, 0.0), 60_000.0)
        scheduler_accepted = raw.accepted is True and RR_RANGE_MS[0] <= rr <= RR_RANGE_MS[1]
        accepted = scheduler_accepted and contact is not False
        interval = Interval(
            t_beat,
            rr,
            accepted,
            now,
            contiguous=raw.contiguous is True,
            bootstrap=accepted and raw.bootstrap is True,
        )
        return interval, scheduler_accepted

    def _prune(self, now: float) -> None:
        while self._reported and self._reported[0][0] <= now - ACCEPTED_WINDOW_MS:
            self._reported.popleft()
        if self._trusted:
            # Two minutes, like the clean intervals. session.py looks back up to 32 s, and a stalled
            # loop that then delivers a minute of packets at once must not prune what it reads.
            newest = self._trusted[-1][0]
            while self._trusted and self._trusted[0][0] <= newest - 2 * WINDOW_MS:
                self._trusted.popleft()
        horizon = self._cleaner.horizon_ms
        if horizon is not None:
            while self._clean and self._clean[0].t_beat <= horizon - 2 * WINDOW_MS:
                self._clean.popleft()

    def _heart_rate_window(self) -> tuple[float | None, float]:
        """Rate and fill over the HR_WINDOW_MS before the newest accepted beat."""
        if not self._trusted:
            return None, 0.0
        newest = self._trusted[-1][0]
        start = newest - HR_WINDOW_MS
        count, total, covered = 0, 0.0, 0.0
        for t_beat, rr in reversed(self._trusted):
            if t_beat <= start:
                break
            count += 1
            total += rr
            covered += max(0.0, t_beat - max(t_beat - rr, start))
        enough = count >= HR_MIN_INTERVALS and covered >= HR_MIN_COVERED_MS and total > 0
        rate = _num(60_000 * count / total, *HR_RANGE_BPM) if enough else None
        return rate, min(1.0, covered / HR_WINDOW_MS)

    def _wire_heart_rate(self) -> float | None:
        """The state message's hr_bpm: the 15 s rate, or before there is one, the latest few
        accepted beats. A burst accepted after a scheduler window reset cannot drag the 15 s rate
        the way it drags four beats."""
        if self._hr_now is not None:
            return self._hr_now
        if not self._wire_rr:
            return None
        return _num(60_000 * len(self._wire_rr) / sum(self._wire_rr), *HR_RANGE_BPM)

    def _update_heart_rate(self) -> None:
        rate, _ = self._heart_rate_window()
        if rate is not None:
            self._hr_now = rate
            if self._phase is BaselinePhase.READY:
                self._peak = rate if self._peak is None else max(self._peak, rate)

    def _hr_fill(self, now: float) -> float:
        _, fill = self._heart_rate_window()
        if not self._trusted:
            return 0.0
        return fill * (1.0 - _ramp(now - self._trusted[-1][0], HR_STALE_FROM_MS, HR_STALE_UNTIL_MS))

    def _update_baseline(self, now: float) -> None:
        if self._phase not in _CAPTURE_PHASES or not self._capture.ready(now):
            return
        result = self._capture.result()
        self._baseline, self._baseline_decided_ms = result, now
        usable = (
            result.passed
            and _num(result.hr_base_bpm, *HR_RANGE_BPM) is not None
            and _num(result.baseline_quality, 0.0, 1.0) is not None
        )
        self._phase = BaselinePhase.READY if usable else BaselinePhase.FAILED
        if usable and self._hr_now is not None:
            self._peak = self._hr_now

    def _update_rmssd(self, now: float) -> None:
        horizon = self._cleaner.horizon_ms
        if horizon is None:
            return
        end = min(now, horizon)
        reading = summarise(self._clean, end - WINDOW_MS, end, MIN_DIFFERENCES, lag_ms=now - end)
        self._rmssd_differences = reading.differences
        self._rmssd_end_ms = end
        self._z_rmssd_now = False
        if self._phase is not BaselinePhase.READY:
            return
        base = self._baseline
        ln_base, hr_base = _num(base.ln_rmssd_base, *LN_RMSSD_RANGE), _num(base.hr_base_bpm)
        ln_now, hr_now = _num(reading.ln_rmssd, *LN_RMSSD_RANGE), _num(reading.mean_hr_bpm)
        if None in (ln_base, hr_base, ln_now, hr_now) or hr_base <= 0 or hr_now <= 0:
            return
        corrected_base = ln_base + RATE_EXPONENT * math.log(hr_base)
        corrected_now = ln_now + RATE_EXPONENT * math.log(hr_now)
        n_base = max(1.0, _num(base.rmssd_base_differences, 0.0) or 0.0)
        n_now = max(1.0, float(reading.differences))
        sampling = RMSSD_SAMPLING_WIDENING * math.sqrt(1 / (2 * n_base) + 1 / (2 * n_now))
        unit = max(RMSSD_UNIT, sampling)
        self._z_rmssd = _clip((corrected_base - corrected_now) / unit, Z_LIMIT)
        self._z_rmssd_now = True

    def _phase_at(self, now: float) -> BaselinePhase:
        if self._phase is BaselinePhase.CAPTURING and now > self._capture.end_ms:
            return BaselinePhase.AWAITING
        return self._phase

    def _signal(self, now: float) -> SignalQuality:
        if self._last_arrival is None:
            return SignalQuality(None, None, None, 0.0, 0.0, 0.0, 0.0, 1.0)
        reported = [ok for arrival, ok in self._reported if arrival > now - ACCEPTED_WINDOW_MS]
        fraction = sum(reported) / len(reported) if reported else None
        accepted = _ramp(fraction, *ACCEPTED_RANGE) if fraction is not None else 0.0
        silence_ms = now - self._last_arrival
        silence = self._silence_factor(silence_ms)
        recovery = self._gap_ramp.at(now) if self._gap_ramp else 1.0
        contact = self._contact_ramp.at(now) if self._contact_ramp else 1.0
        if self._contact_false_since is not None:
            contact = self._contact_fall_from * _contact_fall(now - self._contact_false_since)
        return SignalQuality(
            accepted_fraction=fraction,
            contact=self._contact,
            silence_ms=silence_ms,
            factor=accepted * silence * recovery * contact,
            accepted_factor=accepted,
            silence_factor=silence,
            recovery_factor=recovery,
            contact_factor=contact,
        )

    def _silence_factor(self, silence_ms: float) -> float:
        tuning = self._tuning
        return 1.0 - _ramp(silence_ms, tuning.interpolate_after_ms, tuning.grace_ms)

    def _task_load(self, now: float) -> TaskLoad | None:
        """Infer load without turning every miss into overload.

        ``split`` events define opportunities. A lock is engaged; an abandon on either half is
        engaged but struggling; and a miss is engaged only when an abandon preceded it or it
        arrived before
        85 % of the round's advertised interval. A no-abandon miss at the deadline is the browser's
        automatic timeout and therefore disengagement. Repeated abandons inside one opportunity do
        not buy more confidence, because mouse and head input generate different counts.

        The task value when engaged is ``.25 + .50*difficulty + .25*strain``. Strain is 1 for an
        engaged miss, .60 for a lock after an abandon, and .50 while an abandoned round remains
        unresolved. Disengaged value is .20. Engagement blends between those values and fades from
        half to one and a half expected intervals since the last participant-driven event.
        Confidence rises over eight distinct opportunities and is independently faded when the
        *entire* event stream goes quiet from 1.5 to 4 expected intervals. Thus a running stream of
        timeout misses means low load with confidence; a dead task screen means no confidence. With
        no event at all this returns None, preserving cognitive-load confidence at exactly zero even
        if heart rate rises.
        """
        if self._task_first_event_ms is None or now < self._task_first_event_ms:
            return None
        interval_ms = self._task_latest_interval_ms
        stream_gap_ms = max(0.0, now - self._task_last_event_ms)
        stream_freshness = 1.0 - _ramp(
            stream_gap_ms / interval_ms, *TASK_STREAM_FRESH_INTERVALS
        )

        interaction_anchor = (
            self._task_last_interaction_ms
            if self._task_last_interaction_ms is not None
            else self._task_first_event_ms
        )
        interaction_gap_ms = max(0.0, now - interaction_anchor)
        interaction_freshness = 1.0 - _ramp(
            interaction_gap_ms / interval_ms, *TASK_INTERACTION_FRESH_INTERVALS
        )

        recent = [
            opportunity
            for opportunity in self._task_opportunities
            if now - TASK_WINDOW_MS < opportunity.start_ms <= now
        ]
        engagement_samples: list[float] = []
        strain_samples: list[float] = []
        for opportunity in recent:
            if opportunity.outcome == "timeout":
                engagement_samples.append(0.0)
            elif opportunity.outcome == "engaged_miss":
                engagement_samples.append(1.0)
                strain_samples.append(1.0)
            elif opportunity.outcome == "lock":
                engagement_samples.append(1.0)
                strain_samples.append(
                    TASK_ABANDON_THEN_LOCK_STRAIN if opportunity.abandoned else 0.0
                )
            elif opportunity.abandoned:
                engagement_samples.append(1.0)
                strain_samples.append(TASK_ACTIVE_ABANDON_STRAIN)

        observed_engagement = (
            sum(engagement_samples) / len(engagement_samples) if engagement_samples else 0.5
        )
        engagement = _unit(observed_engagement * interaction_freshness, 0.0)
        strain = _unit(
            sum(strain_samples) / len(strain_samples) if strain_samples else 0.0,
            0.0,
        )
        difficulty = self._task_latest_difficulty
        engaged_value = (
            TASK_ENGAGED_BASE
            + TASK_DIFFICULTY_WEIGHT * difficulty
            + TASK_STRAIN_WEIGHT * strain
        )
        value = TASK_DISENGAGED_VALUE + engagement * (engaged_value - TASK_DISENGAGED_VALUE)

        opportunity_count = self._task_opportunity_count
        sample_factor = min(1.0, opportunity_count / TASK_FULL_OPPORTUNITIES)
        confidence = sample_factor * stream_freshness
        return TaskLoad(
            value=_unit(value, NEUTRAL),
            confidence=_unit(confidence, 0.0),
            engagement=engagement,
            difficulty=difficulty,
            strain=strain,
            stream_freshness=stream_freshness,
            opportunities=opportunity_count,
            interaction_gap_ms=interaction_gap_ms,
        )


_CAPTURE_PHASES = (BaselinePhase.CAPTURING, BaselinePhase.AWAITING)


def _task_interval(value: object) -> float:
    """A plausible advertised round interval, or the middle of the authored ramp."""
    return _num(value, *TASK_INTERVAL_RANGE_MS) or TASK_DEFAULT_INTERVAL_MS


def blend_cognitive_load(
    task: TaskLoad | None, hr_part: float, hr_confidence: float
) -> tuple[float, float]:
    """Task events first, heart rate second, and heart rate never adds confidence on its own."""
    if not isinstance(task, TaskLoad):
        return NEUTRAL, 0.0
    value, confidence = _num(task.value, 0.0, 1.0), _num(task.confidence, 0.0, 1.0)
    if value is None or confidence is None:
        return NEUTRAL, 0.0
    hr_value, hr_trust = _unit(hr_part, NEUTRAL), _unit(hr_confidence, 0.0)
    return (
        TASK_WEIGHT * value + (1.0 - TASK_WEIGHT) * hr_value,
        confidence * (TASK_WEIGHT + (1.0 - TASK_WEIGHT) * hr_trust),
    )


def _num(x: object, lo: float = -math.inf, hi: float = math.inf) -> float | None:
    """x as a float when it is a real, finite number in [lo, hi]; otherwise None. Never raises."""
    if isinstance(x, bool) or not isinstance(x, int | float):
        return None
    if isinstance(x, int) and abs(x) > 2**53:
        return None
    x = float(x)
    if not math.isfinite(x) or not lo <= x <= hi:
        return None
    return x


def _unit(x: object, fallback: float) -> float:
    """x clamped to [0, 1], with anything that is not a finite number replaced by fallback."""
    value = _num(x)
    if value is None:
        return fallback
    return 0.0 if value <= 0.0 else 1.0 if value >= 1.0 else value


def _confidence(x: object) -> float:
    step = 10**CONFIDENCE_DECIMALS
    return math.floor(_unit(x, 0.0) * step) / step


def _finite(x: object) -> float | None:
    return _num(x)


def _ramp(x: float | None, lo: float, hi: float) -> float:
    """0 at or below lo, 1 at or above hi, linear between."""
    if x is None or x <= lo:
        return 0.0
    if x >= hi:
        return 1.0
    return (x - lo) / (hi - lo)


def _clip(x: float, limit: float) -> float:
    if not math.isfinite(x):
        return 0.0
    return max(-limit, min(limit, x))


def _contact_fall(lasted_ms: float) -> float:
    return 1.0 - _ramp(lasted_ms, 0.0, CONTACT_FALL_MS)
