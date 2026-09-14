"""The session state machine: idle, baseline, load, regulate, resolve, reset (prompt 2.4).

Time-driven with physiological modulation, never physiology-driven with a hopeful timer. There is
a queue of people, so every segment but idle has a hard end:

- baseline, nominal 45 s: the capture is 45 s, then the session holds for hr_base (contract v1.3)
  and enters load as soon as it is ready, at most 12 s later.
  - A result that fails the quality gate goes to reset: the attendant re-seats and restarts.
  - No result by the end of the hold: model.close_baseline judges the gate on what had been
    classified by then, and sets aside a result that came later. A failed gate goes to reset as
    above; a passed one enters load degraded, with no hr_base.
- load, 75 s.
- regulate, nominal 75 s. It ends at 75 s if regulated by then, otherwise as soon as it is, and at
  105 s whatever the body is doing. It never ends before 75 s.
  - With no threshold it runs a plain 75 s: after a degraded baseline, or when HR_load is too
    thinly covered to set one.
  - An extension also stops as "unjudged" once no beat has come for SIGNAL_LOST_MS, for the reason
    a degraded session gets no extension: it would be waiting for a threshold it cannot judge.
- resolve, 45 s.
- reset, 20 s, the trace held while it is photographed (project plan §8); 3 s after a stop or a
  failed gate, the fade at stop (prompt 2.5). Then a new session begins, in idle.

The regulate threshold (experience script §2): regulated when any 20 s window has mean heart rate at
or below HR_load - max(5 bpm, 0.5 x rise), where rise = HR_load - HR_base.

- HR_load is the mean over load's last 30 s, from the beats hr_base comes from
  (PsvModel.heart_rate). It is taken SETTLE_MS into regulate, once the beats of load's last second
  have arrived, and only when they cover at least 22.5 s of the 30.
- A window lies wholly inside regulate, ends on a WINDOW_STEP_MS grid, and counts only when its
  beats cover at least 15 s of the 20. Each window is judged once, at the first tick past its end.
- Regulated stays true once met.

Load activation and regulate's RMSSD return are recorded in the log, never gating. An RMSSD part
needs rmssd_base and at least 20 clean differences in each 30 s window, and is logged as
unavailable, with the reason, otherwise.

Boundaries are exact, never at whichever tick noticed. A segment starts at the previous one's
deadline, at the arrival of the packet that completed the baseline, at the end of the window that
met the threshold, or at the moment the signal had been lost for SIGNAL_LOST_MS. A late tick sends
one state message for each boundary it crosses, in order. Two verdicts rest on what had arrived
when the tick ran, though: a window judged late has more of its last second's beats, and a signal
that came back during a stall is not lost. State messages go out at every boundary and on a fixed
2 s lattice. The session clock never runs backwards: an earlier now is taken as the latest seen.

A session is idle before start, the run, and reset. When reset ends, the old session's last line is
logged, the model forgets it, the log takes a new id and file, and the state and beat counters start
again from 1 (contract §2). Nothing from the old session is published after that.

The caller owns the packets and the beat scheduler, and never feeds a packet through here: a
packet fed twice changes the baseline without a trace. Use from one thread, the bridge's loop.

    session = Session(model, log, server.publish, now_ms=now)
    # every packet: result = scheduler.on_packet(now, payload); model.on_packet(now, result)
    #               for event in result.events: server.publish(session.beat_message(event))
    # every tick:   for event in scheduler.tick(now): server.publish(session.beat_message(event))
    #               session.tick(now)
    # the attendant: session.start(now), session.stop(now)
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bridge.baseline import DURATION_MS as BASELINE_MS
from bridge.baseline import TAIL_MS, Baseline
from bridge.beat_scheduler import BeatEvent, Tuning
from bridge.contract import (
    BASELINE_OVERRUN_MS,
    MAX_SAFE_INT,
    REGULATE_OVERRUN_MS,
    STATE_INTERVAL_MS,
)
from bridge.logging import SessionLog
from bridge.psv import BaselinePhase, PsvModel
from bridge.state import StateStream

# The regulate success threshold.
THRESHOLD_FLOOR_BPM = 5.0
THRESHOLD_RISE_SHARE = 0.5
WINDOW_MS = 20_000
WINDOW_MIN_COVERED_MS = 15_000
WINDOW_STEP_MS = 500
LOAD_MIN_COVERED_MS = 22_500  # of load's last 30 s: the same three quarters a window needs
# Beats are reported at most max_lag_ms (1.2 s) after they happen. This long after a moment, every
# beat before it that will ever arrive has arrived.
SETTLE_MS = 2_000
# No accepted beat for this long: the scheduler has stopped scheduling (grace_ms), and the last
# report is older than the longest reporting lag.
SIGNAL_LOST_MS = Tuning.grace_ms + Tuning.max_lag_ms

# Recorded, not gating (experience script §2 LOAD and REGULATE).
ACTIVATION_BPM = 6.0
ACTIVATION_RMSSD_RATIO = 0.80
RETURN_RMSSD_RATIO = 0.95
RMSSD_MIN_DIFFERENCES = 20  # in each 30 s window (docs/known-limits.md)

RUNNING = ("baseline", "load", "regulate", "resolve")


@dataclass(frozen=True)
class Timings:
    """Segment lengths, ms. Prompt 2.7 shortens the hold to the pulse boundary and sets
    hold_to_end, so load starts on that boundary even when hr_base came sooner. A result that
    fails the quality gate still ends baseline when it arrives."""

    hold_ms: float = BASELINE_OVERRUN_MS
    hold_to_end: bool = False
    load_ms: float = 75_000
    regulate_ms: float = 75_000
    extension_ms: float = REGULATE_OVERRUN_MS
    resolve_ms: float = 45_000
    reset_ms: float = 20_000
    stopped_reset_ms: float = 3_000

    def __post_init__(self) -> None:
        longest = 600_000  # no segment runs ten minutes
        bounds = {
            "hold_ms": (0, BASELINE_OVERRUN_MS),
            "extension_ms": (0, REGULATE_OVERRUN_MS),
            "load_ms": (1, longest),
            "regulate_ms": (WINDOW_MS, longest),
            "resolve_ms": (1, longest),
            "reset_ms": (1, longest),
            "stopped_reset_ms": (1, longest),
        }
        for name, (lo, hi) in bounds.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{name} must be a number")
            if not lo <= value <= hi:  # NaN fails this too
                raise ValueError(f"{name} {value} is outside {lo} to {hi}")
        if not isinstance(self.hold_to_end, bool):
            raise ValueError("hold_to_end must be True or False")


@dataclass(frozen=True)
class Schedule:
    """Where the session is, on T_engine. Both ends are None in idle, which waits for start."""

    segment: str
    started_ms: float
    ends_at_earliest_ms: float | None
    ends_at_latest_ms: float | None


@dataclass(frozen=True)
class RegulateResult:
    """What regulate found, for the log. Never the attendant's close, which follows the visible
    fall on the trace screen (experience script §3). Times are into regulate."""

    outcome: str  # regulated, timeout, unjudged, or no_threshold
    ran_ms: float
    hr_base_bpm: float | None
    hr_load_bpm: float | None
    hr_load_covered_ms: float
    rise_bpm: float | None
    threshold_bpm: float | None
    no_threshold: str | None  # why there was none
    first_met_ms: float | None  # the end of the first window at or under the threshold
    lowest_window_bpm: float | None  # the lowest mean of any counted window
    drop_bpm: float | None  # HR_load minus that lowest mean


class _Regulate:
    """The threshold and the windows of one regulate segment."""

    def __init__(self, start_ms: float, hr_base: float | None, timings: Timings) -> None:
        self.start = start_ms
        self.hr_base = hr_base
        self.timings = timings
        self.hr_load: float | None = None
        self.covered = 0.0
        self.threshold: float | None = None
        self.rise: float | None = None
        self.no_threshold = None if hr_base is not None else "no hr_base: the baseline was degraded"
        self.load_known = False
        self.windows = 0  # judged so far
        self.first_met: float | None = None
        self.lowest: float | None = None

    @property
    def usable_hr_load(self) -> float | None:
        return self.hr_load if self.covered >= LOAD_MIN_COVERED_MS else None

    def cap(self) -> float:
        """The latest regulate can end, as far as is known."""
        t = self.timings
        if self.no_threshold is not None:
            return self.start + t.regulate_ms
        if self.first_met is not None:
            return self.start + max(t.regulate_ms, self.first_met)
        return self.start + t.regulate_ms + t.extension_ms

    def take_hr_load(self, model: PsvModel, now: float, *, force: bool = False) -> bool:
        """Once, SETTLE_MS in (or when forced). True the call it happens."""
        if self.load_known or (now < self.start + SETTLE_MS and not force):
            return False
        self.load_known = True
        window = model.heart_rate(self.start - TAIL_MS, self.start)
        self.hr_load, self.covered = window.bpm, window.covered_ms
        hr_load = self.usable_hr_load
        if self.hr_base is None:
            return True  # no threshold, and said so from the start
        if hr_load is None:
            self.no_threshold = (
                f"HR_load from {self.covered / 1000:.1f} s of load's last "
                f"{TAIL_MS / 1000:.0f} s, needs {LOAD_MIN_COVERED_MS / 1000:.1f}"
            )
        else:
            self.rise = hr_load - self.hr_base
            self.threshold = hr_load - max(THRESHOLD_FLOOR_BPM, THRESHOLD_RISE_SHARE * self.rise)
        return True

    def judge_windows(self, model: PsvModel, now: float) -> float | None:
        """Judge every window ending by now and inside regulate as far as its end is known.
        Returns the end of the first to meet the threshold, if one did in this call."""
        met = None
        while (end := self.start + WINDOW_MS + self.windows * WINDOW_STEP_MS) <= min(
            now, self.cap()
        ):
            self.windows += 1
            window = model.heart_rate(end - WINDOW_MS, end)
            if window.bpm is None or window.covered_ms < WINDOW_MIN_COVERED_MS:
                continue
            self.lowest = window.bpm if self.lowest is None else min(self.lowest, window.bpm)
            meets = self.threshold is not None and window.bpm <= self.threshold
            if meets and self.first_met is None:
                self.first_met = met = end - self.start
        return met

    def result(self, outcome: str, end_ms: float) -> RegulateResult:
        hr_load = self.usable_hr_load
        return RegulateResult(
            outcome=outcome,
            ran_ms=end_ms - self.start,
            hr_base_bpm=self.hr_base,
            hr_load_bpm=self.hr_load,
            hr_load_covered_ms=self.covered,
            rise_bpm=self.rise,
            threshold_bpm=self.threshold,
            no_threshold=self.no_threshold,
            first_met_ms=self.first_met,
            lowest_window_bpm=self.lowest,
            drop_bpm=None if hr_load is None or self.lowest is None else hr_load - self.lowest,
        )


class Session:
    """One armband's sessions, one after another. See the module docstring."""

    def __init__(
        self,
        model: PsvModel,
        log: SessionLog,
        publish: Callable[[dict], Any],
        *,
        now_ms: float,
        timings: Timings | None = None,
    ) -> None:
        self._model = model
        self._log = log
        self._publish = publish
        self.timings = timings if timings is not None else Timings()
        self._now = _time(now_ms) or 0.0
        self._next_state_ms = self._now
        self._sent_ms: float | None = None
        self._signal_lost: bool | None = None
        self._open(log.session if log.is_open else log.start_session(), self._now)

    # --- what the bridge and the attendant see ---

    @property
    def session(self) -> str:
        return self._stream.session

    @property
    def segment(self) -> str:
        return self._segment

    @property
    def schedule(self) -> Schedule:
        seg, start, t = self._segment, self._start, self.timings
        if seg == "idle":
            return Schedule(seg, start, None, None)
        if seg == "baseline":
            # A failed gate ends it at the result's arrival, from the close on, even under
            # hold_to_end; only load waits for the end of the hold.
            end = start + BASELINE_MS
            return Schedule(seg, start, end, end + t.hold_ms)
        if seg == "regulate":
            return Schedule(seg, start, start + t.regulate_ms, self._regulate.cap())
        end = start + self._length()
        return Schedule(seg, start, end, end)

    @property
    def signal_lost(self) -> bool:
        """No accepted beat for SIGNAL_LOST_MS, as of the latest tick."""
        return self._signal_lost is not False

    @property
    def regulate_result(self) -> RegulateResult | None:
        """This session's, once regulate has ended; None before, and when it was stopped."""
        return self._result

    # --- the attendant ---

    def start(self, now_ms: float) -> str | None:
        """Begin the baseline now. Returns None, or why not: running, resetting, no_signal, or
        bad_time for a time the link cannot carry, the only refusal not logged."""
        now = self._clock(now_ms)
        if now is None:
            return "bad_time"
        self._watch_signal(now)
        self._advance(now)
        self._follow(now)
        if self._segment != "idle":
            reason = "resetting" if self._segment == "reset" else "running"
        elif self._lost(now):
            reason = "no_signal"
        elif not self._model.start_baseline(now):
            reason = "bad_time"
        else:
            self._started = now
            self._move("baseline", now, "start", now)
            self._send(now, 0.0)
            return None
        self._log.event("start_refused", t_engine=now, reason=reason, segment=self._segment)
        return reason

    def stop(self, now_ms: float) -> bool:
        """End a running session now, through a short reset. False in idle and reset."""
        now = self._clock(now_ms)
        if now is None:
            return False
        self._advance(now)
        if self._segment not in RUNNING:
            return False
        self._move("reset", now, "stopped", now)
        self._send(now, 0.0)
        return True

    # --- the loop ---

    def tick(self, now_ms: float) -> None:
        """Every 20 to 100 ms: take boundaries that are due, keep the records, send state."""
        now = self._clock(now_ms)
        if now is None:
            return
        self._watch_signal(now)
        self._advance(now)
        self._follow(now)
        if now >= self._next_state_ms:
            if self._sent_ms != now:  # a boundary, a start or a stop has just sent one
                self._send(now, now - self._start)
            behind = (now - self._next_state_ms) // STATE_INTERVAL_MS + 1
            self._next_state_ms += behind * STATE_INTERVAL_MS

    def beat_message(self, event: BeatEvent) -> dict:
        """The beat message for this session, numbered from 1 in every session (contract §2)."""
        self._beat_seq += 1
        return dataclasses.replace(event, seq=self._beat_seq).message(self.session)

    # --- segments ---

    def _advance(self, now: float) -> None:
        """Take every boundary due by now, in order, sending a state message for each."""
        entered = False
        while (step := self._due(now)) is not None:
            to, at, reason = step
            if entered:
                # Entered and left within this call: its message, before anything of the next.
                self._send(now, at - self._start)
            self._move(to, at, reason, now)
            entered = True
        if entered:
            self._send(now, now - self._start)

    def _due(self, now: float) -> tuple[str, float, str] | None:
        seg, start, t = self._segment, self._start, self.timings
        if seg == "baseline":
            return self._baseline_due(now)
        if seg == "load" and now >= start + t.load_ms:
            return "regulate", start + t.load_ms, "load_done"
        if seg == "regulate":
            return self._regulate_due(now)
        if seg == "resolve" and now >= start + t.resolve_ms:
            return "reset", start + t.resolve_ms, "completed"
        if seg == "reset" and now >= start + self._length():
            return "idle", start + self._length(), "reset_done"
        return None

    def _baseline_due(self, now: float) -> tuple[str, float, str] | None:
        t = self.timings
        closes = self._start + BASELINE_MS
        cap = closes + t.hold_ms
        if now < closes:
            return None
        model = self._model
        decided = model.baseline_decided_ms
        if decided is not None and decided <= min(now, cap):
            # In by the end of the hold: when it came is the boundary.
            at = max(closes, decided)
            phase = model.estimate(now).phase
            if phase is BaselinePhase.FAILED:
                return "reset", at, "gate_failed"
            if phase is BaselinePhase.READY:
                if t.hold_to_end:
                    return ("load", cap, "baseline_ready") if now >= cap else None
                return "load", at, "baseline_ready"
        if now < cap:
            return None
        # Judged as it stood at the end of the hold, whenever this tick came.
        if model.close_baseline(cap) is BaselinePhase.FAILED:
            return "reset", cap, "gate_failed"
        model.mark_baseline_degraded()
        return "load", cap, "baseline_degraded"

    def _regulate_due(self, now: float) -> tuple[str, float, str] | None:
        r, t = self._regulate, self.timings
        self._follow_regulate(now)
        nominal_end = r.start + t.regulate_ms
        if now < nominal_end:
            return None
        if r.no_threshold is not None:
            return "resolve", nominal_end, "no_threshold"
        if r.first_met is not None:
            return "resolve", r.start + max(t.regulate_ms, r.first_met), "regulated"
        if now >= nominal_end + t.extension_ms:
            return "resolve", nominal_end + t.extension_ms, "timeout"
        if self._lost(now):
            last = self._model.last_trusted_beat_ms
            lost_at = nominal_end if last is None else last + SIGNAL_LOST_MS
            return "resolve", min(now, max(nominal_end, lost_at)), "unjudged"
        return None

    def _move(self, to: str, at: float, reason: str, now: float) -> None:
        leaving = self._segment
        if leaving == "baseline":
            self._end_baseline(to, at, reason, now)
        elif leaving == "regulate":
            self._end_regulate(to, at, reason, now)
        elif leaving == "resolve":
            self._try_return(now, final=True)
        if to == "idle":
            self._log.event("session_end", t_engine=now, at_ms=at, outcome=self._outcome)
            self._model.reset()
            self._log.start_session()
            self._open(self._log.session, at)
            return
        self._log.event(
            "segment", t_engine=now, segment=to, previous=leaving, at_ms=at, reason=reason
        )
        if to == "regulate":
            hr_base = self._baseline.hr_base_bpm if self._baseline else None
            self._regulate = _Regulate(at, hr_base, self.timings)
            self._activation_logged = False
        elif to == "reset":
            self._outcome = reason
            self._reset_ms = (
                self.timings.reset_ms if reason == "completed" else self.timings.stopped_reset_ms
            )
        self._segment, self._start = to, at

    def _end_baseline(self, to: str, at: float, reason: str, now: float) -> None:
        baseline = self._model.baseline
        fields: dict[str, Any] = {"held_ms": max(0.0, at - self._start - BASELINE_MS)}
        if reason == "baseline_degraded":
            fields["why"] = (
                f"no hr_base {self.timings.hold_ms / 1000:.0f} s after the window closed; "
                "the quality gate passed on what had been classified"
            )
        if baseline is not None:
            fields.update(
                hr_base_bpm=baseline.hr_base_bpm,
                rmssd_base_ms=baseline.rmssd_base_ms,
                rmssd_base_differences=baseline.rmssd_base_differences,
                baseline_quality=baseline.baseline_quality,
                accepted_ms=baseline.accepted_ms,
                accepted_intervals=baseline.accepted_intervals,
                problems=list(baseline.problems),
            )
        outcome = {"gate_failed": "failed", "baseline_degraded": "degraded", "stopped": "stopped"}
        fields["outcome"] = outcome.get(reason, "ready")
        if reason == "baseline_ready":
            self._baseline = baseline  # the one this session took, whatever the model does next
        self._log.event("baseline_end", t_engine=now, **fields)

    def _end_regulate(self, to: str, at: float, reason: str, now: float) -> None:
        r = self._regulate
        if r.take_hr_load(self._model, now, force=True):
            self._log_threshold(now)
        self._try_activation(now, final=True)
        if to == "resolve":
            self._result = r.result(reason, at)
            self._log.event("regulate_result", t_engine=now, **dataclasses.asdict(self._result))
            self._return_window = (at - TAIL_MS, at)

    # --- records, kept each tick ---

    def _watch_signal(self, now: float) -> None:
        lost = self._lost(now)
        if lost != self._signal_lost:
            self._log.event("signal", t_engine=now, lost=lost, segment=self._segment)
            self._signal_lost = lost

    def _follow(self, now: float) -> None:
        if self._segment == "regulate":
            self._follow_regulate(now)
        elif self._segment == "resolve":
            self._try_return(now, final=False)

    def _follow_regulate(self, now: float) -> None:
        r = self._regulate
        if r.take_hr_load(self._model, now):
            self._log_threshold(now)
        met = r.judge_windows(self._model, now)
        if met is not None:
            self._log.event("regulate_met", t_engine=now, at_ms=met, threshold_bpm=r.threshold)
        self._try_activation(now, final=False)

    def _log_threshold(self, now: float) -> None:
        r = self._regulate
        self._log.event(
            "regulate_threshold",
            t_engine=now,
            hr_base_bpm=r.hr_base,
            hr_load_bpm=r.hr_load,
            hr_load_covered_ms=r.covered,
            rise_bpm=r.rise,
            threshold_bpm=r.threshold,
            no_threshold=r.no_threshold,
        )

    def _try_activation(self, now: float, *, final: bool) -> None:
        """Load activation: HR_load >= HR_base + 6 bpm, or load's RMSSD <= 0.80 x rmssd_base."""
        r = self._regulate
        if self._activation_logged or not r.load_known:
            return
        window = (r.start - TAIL_MS, r.start)
        rmssd, why = self._rmssd_part(window, final, "load", at_most=ACTIVATION_RMSSD_RATIO)
        if rmssd is None and why is None:
            return  # waiting for classification to pass the end of load
        hr_load, base = r.usable_hr_load, self._baseline
        by_hr = None
        if hr_load is not None and r.hr_base is not None:
            by_hr = hr_load >= r.hr_base + ACTIVATION_BPM
        parts = [p for p in (by_hr, rmssd) if p is not None]
        self._log.event(
            "load_activation",
            t_engine=now,
            activated=any(parts) if parts else None,
            by_hr=by_hr,
            hr_unavailable=r.no_threshold if by_hr is None else None,
            hr_base_bpm=r.hr_base,
            hr_load_bpm=r.hr_load,
            hr_load_covered_ms=r.covered,
            by_rmssd=rmssd,
            rmssd_unavailable=why,
            **self._rmssd_fields(window, base),
        )
        self._activation_logged = True

    def _try_return(self, now: float, *, final: bool) -> None:
        """Regulate's RMSSD return: RMSSD over its last 30 s >= 0.95 x rmssd_base. Recorded."""
        window = self._return_window
        if window is None:
            return
        returned, why = self._rmssd_part(window, final, "regulate", at_least=RETURN_RMSSD_RATIO)
        if returned is None and why is None:
            return
        self._log.event(
            "rmssd_return",
            t_engine=now,
            returned=returned,
            rmssd_unavailable=why,
            **self._rmssd_fields(window, self._baseline),
        )
        self._return_window = None

    def _rmssd_part(
        self,
        window: tuple[float, float],
        final: bool,
        segment: str,
        *,
        at_most: float | None = None,
        at_least: float | None = None,
    ) -> tuple[bool | None, str | None]:
        """(verdict, None), (None, why it is unavailable), or (None, None) to wait."""
        base = self._baseline
        if base is None:
            return None, "no baseline: it was degraded"
        if base.rmssd_base_ms is None or base.rmssd_base_differences < RMSSD_MIN_DIFFERENCES:
            return None, (
                f"rmssd_base from {base.rmssd_base_differences} clean differences, "
                f"needs {RMSSD_MIN_DIFFERENCES}"
            )
        reading = self._model.rmssd(*window)
        if reading is None:
            if not final:
                return None, None
            return None, f"classification had not passed the end of {segment}"
        if reading.rmssd_ms is None or reading.differences < RMSSD_MIN_DIFFERENCES:
            return None, (
                f"{reading.differences} clean differences in {segment}'s last "
                f"{TAIL_MS / 1000:.0f} s, needs {RMSSD_MIN_DIFFERENCES}"
            )
        if at_most is not None:
            return reading.rmssd_ms <= at_most * base.rmssd_base_ms, None
        return reading.rmssd_ms >= at_least * base.rmssd_base_ms, None

    def _rmssd_fields(self, window: tuple[float, float], base: Baseline | None) -> dict:
        reading = self._model.rmssd(*window)
        return {
            "rmssd_ms": None if reading is None else reading.rmssd_ms,
            "rmssd_differences": None if reading is None else reading.differences,
            "rmssd_base_ms": None if base is None else base.rmssd_base_ms,
            "rmssd_base_differences": None if base is None else base.rmssd_base_differences,
        }

    # --- sessions and messages ---

    def _open(self, session: str, at: float) -> None:
        self._stream = StateStream(session)
        self._beat_seq = 0
        self._segment, self._start = "idle", at
        self._started: float | None = None
        self._outcome: str | None = None
        self._reset_ms = self.timings.reset_ms
        self._baseline: Baseline | None = None
        self._regulate: _Regulate | None = None
        self._activation_logged = False
        self._return_window: tuple[float, float] | None = None
        self._result: RegulateResult | None = None
        self._log.event(
            "session_start",
            t_engine=self._now,
            session=session,
            at_ms=at,
            signal_lost=self._signal_lost,
            timings=dataclasses.asdict(self.timings),
        )

    def _length(self) -> float:
        t = self.timings
        return {
            "baseline": BASELINE_MS,
            "load": t.load_ms,
            "regulate": t.regulate_ms,
            "resolve": t.resolve_ms,
            "reset": self._reset_ms,
        }.get(self._segment, 0)

    def _send(self, now: float, elapsed: float) -> None:
        estimate = self._model.estimate(now)
        baseline = self._baseline
        idle = self._segment == "idle"
        self._publish(
            self._stream.message(
                t_engine_ms=now,
                t_session_ms=None if self._started is None else now - self._started,
                segment=self._segment,
                segment_elapsed_ms=0.0 if idle else max(0.0, elapsed),  # idle has no length
                segment_nominal_ms=self._length(),
                estimate=estimate,
                hr_base_bpm=baseline.hr_base_bpm if baseline else None,
                baseline_quality=baseline.baseline_quality if baseline else 0.0,
            )
        )
        self._sent_ms = now

    def _lost(self, now: float) -> bool:
        last = self._model.last_trusted_beat_ms
        return last is None or last <= now - SIGNAL_LOST_MS

    def _clock(self, now_ms: float) -> float | None:
        now = _time(now_ms)
        if now is None:
            return None
        self._now = max(now, self._now)
        return self._now


def _time(x: object) -> float | None:
    """T_engine as the link can carry it, or None."""
    if isinstance(x, bool) or not isinstance(x, int | float) or not 0 <= x <= MAX_SAFE_INT:
        return None
    return float(x)
