"""The 45 s baseline: the person's own normal, measured before anything is asked of them.

hr_base and rmssd_base come from the last 30 s only. The person has just walked in off a loud
exhibition floor, and their rate is often still falling as they sit down; the first 15 s say
more about the walk than about them.

- hr_base uses every interval the scheduler accepted (not bootstrap), the set the gate trusts,
  so it exists whenever the gate passes. A stray false interval moves a 30 s mean very little.
- rmssd_base uses only HRV-clean successive differences (bridge/hrv.py), because one false
  interval moves RMSSD a lot. After a few artefacts it can be None while the gate passes; that
  is honest, and whatever reads it must cope.

A line fitted to heart rate across the whole 45 s gives baseline_quality. Flat means the person
had settled and the baseline can be trusted; steep means it was measured while they were still
coming down, or going up. The fit uses the accepted intervals too, so it spans the window, and
it is a Theil-Sen fit (the median of every pairwise slope), so a stray false interval cannot
drag an end of it.

The quality gate (experience script §2 BASELINE) is separate, and it is about the armband, not
the person: at least 35 of the 45 s of clean data and at least 30 accepted intervals. Failing it
means the attendant re-seats the armband and restarts. "Clean data" here is time covered by
intervals the scheduler accepted, not the stricter HRV-clean set: a single artefact costs HRV
about 13 intervals, and that must not send a well-seated armband back.

    baseline = BaselineCapture(start_ms=t_engine_at_baseline_start)
    baseline.add(cleaner.add(result.intervals))  # the same classified stream HRV uses
    if baseline.ready(now): baseline.result()
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from statistics import median

from bridge.hrv import MIN_DIFFERENCES, HrvInterval, covered_ms, summarise

DURATION_MS = 45_000
TAIL_MS = 30_000  # hr_base and rmssd_base come from this much at the end
MIN_CLEAN_MS = 35_000  # the gate
MIN_ACCEPTED = 30  # the gate
# Intervals are classified a guard's length late (bridge/hrv.py). ready() waits for the first
# one past the end, or this long if the armband has gone quiet.
READY_TIMEOUT_MS = 20_000
MIN_SLOPE_POINTS = 10
# baseline_quality from the slope, in bpm per minute. A settled resting rate drifts a couple
# of bpm a minute. Provisional: tune against real sessions.
FLAT_SLOPE = 2.0  # at or under: quality 1
STEEP_SLOPE = 12.0  # at or over: quality 0


@dataclass(frozen=True)
class Baseline:
    start_ms: float
    end_ms: float
    hr_base_bpm: float | None  # last TAIL_MS, accepted intervals
    rmssd_base_ms: float | None  # last TAIL_MS, HRV-clean differences; None under MIN_DIFFERENCES
    ln_rmssd_base: float | None
    slope_bpm_per_min: float | None  # across the whole window, accepted intervals, Theil-Sen
    baseline_quality: float  # 0 to 1, from the slope; 0 when there is no slope
    accepted_ms: float  # the gate's "clean data": time covered by accepted intervals
    accepted_intervals: int  # accepted by the scheduler, not bootstrap
    passed: bool  # the quality gate
    problems: tuple[str, ...]  # why it failed, for the attendant; empty when it passed


class BaselineCapture:
    def __init__(self, start_ms: float, duration_ms: float = DURATION_MS) -> None:
        self.start_ms = start_ms
        self.end_ms = start_ms + duration_ms
        self._intervals: list[HrvInterval] = []
        self._classified_past_end = False

    def add(self, intervals: Iterable[HrvInterval]) -> None:
        for interval in intervals:
            if interval.t_beat > self.end_ms:
                self._classified_past_end = True
            elif interval.t_beat > self.start_ms:
                self._intervals.append(interval)

    def ready(self, now_ms: float) -> bool:
        """True once every interval inside the window has been classified."""
        return self._classified_past_end or now_ms >= self.end_ms + READY_TIMEOUT_MS

    def result(self) -> Baseline:
        start, end = self.start_ms, self.end_ms
        tail = summarise(self._intervals, end - TAIL_MS, end, MIN_DIFFERENCES)
        trusted = [i for i in self._intervals if i.accepted and not i.bootstrap]
        accepted_ms = sum(covered_ms(i, start, end) for i in trusted)
        trusted_tail = [i for i in trusted if i.t_beat > end - TAIL_MS]
        tail_rr = sum(i.rr_ms for i in trusted_tail)
        hr_base = 60_000 * len(trusted_tail) / tail_rr if tail_rr > 0 else None
        slope = _slope_bpm_per_min(trusted)

        problems = []
        if accepted_ms < MIN_CLEAN_MS:
            problems.append(
                f"{accepted_ms / 1000:.1f} s of clean data in {(end - start) / 1000:.0f} s, "
                f"needs {MIN_CLEAN_MS / 1000:.0f}"
            )
        if len(trusted) < MIN_ACCEPTED:
            problems.append(f"{len(trusted)} accepted intervals, needs {MIN_ACCEPTED}")

        return Baseline(
            start_ms=start,
            end_ms=end,
            hr_base_bpm=hr_base,
            rmssd_base_ms=tail.rmssd_ms,
            ln_rmssd_base=tail.ln_rmssd,
            slope_bpm_per_min=slope,
            baseline_quality=_quality(slope),
            accepted_ms=accepted_ms,
            accepted_intervals=len(trusted),
            passed=not problems,
            problems=tuple(problems),
        )


def _slope_bpm_per_min(intervals: list[HrvInterval]) -> float | None:
    """Theil-Sen slope of instantaneous heart rate against beat time: the median pairwise slope."""
    if len(intervals) < MIN_SLOPE_POINTS:
        return None
    points = [(i.t_beat / 60_000, 60_000 / i.rr_ms) for i in intervals if i.rr_ms > 0]
    slopes = [
        (y2 - y1) / (x2 - x1)
        for k, (x1, y1) in enumerate(points)
        for x2, y2 in points[k + 1 :]
        if x2 != x1
    ]
    return median(slopes) if slopes else None


def _quality(slope: float | None) -> float:
    if slope is None:
        return 0.0
    return max(0.0, min(1.0, (STEEP_SLOPE - abs(slope)) / (STEEP_SLOPE - FLAT_SLOPE)))
