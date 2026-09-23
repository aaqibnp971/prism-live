"""The 45 s baseline: the person's own normal, measured before anything is asked of them.

hr_base and rmssd_base come from the last 30 s only. The person has just walked in off a loud
exhibition floor, and their rate is often still falling as they sit down; the first 15 s say
more about the walk than about them.

- hr_base uses every interval the scheduler accepted (not bootstrap), the set the gate trusts,
  so it exists whenever the gate passes. A stray false interval moves a 30 s mean very little.
  For legacy HRM, "accepted" also means sent with sensor contact. Optical PPI deliberately does
  not use contact/HR as wear evidence; the scheduler applies blocker/error quality first.
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
about 12 intervals, and that must not send a well-seated armband back.

    baseline = BaselineCapture(start_ms=t_engine_at_baseline_start)
    baseline.add(cleaner.add(result.intervals), now)  # the classified stream HRV uses, and when
    if baseline.ready(now): baseline.result()
    baseline.result(as_of_ms=t)  # from only what had been classified by t

PPI mode records scheduler-accepted evidence at arrival for the gate and HR independently of
HRV's lookahead. It still waits for full classification when possible. At the session's 11 s
hold cap, an accepted-data gate that passes yields HR_base even when HRV is incomplete; that
immutable snapshot explicitly carries hrv_complete=False and no baseline RMSSD or HR spread.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from statistics import median

from bridge.beat_scheduler import Interval
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
    # For bridge/psv.py. The spread of instantaneous heart rate over the last TAIL_MS, robust to
    # the odd false interval (1.4826 x the median absolute deviation, HRV-clean intervals only),
    # and how many differences rmssd_base rests on.
    hr_sd_bpm: float | None = None
    rmssd_base_differences: int = 0
    hrv_complete: bool = True  # PPI cap may freeze HR/gate before HRV lookahead is complete


class BaselineCapture:
    def __init__(
        self, start_ms: float, duration_ms: float = DURATION_MS, *, measured_ppi: bool = False
    ) -> None:
        self.start_ms = start_ms
        self.end_ms = start_ms + duration_ms
        self.measured_ppi = measured_ppi
        self._intervals: list[HrvInterval] = []
        self._classified_ms: list[float] = []  # when each was classified, if the caller said
        self._classified_past_end = False
        self._classified_past_end_ms = math.inf
        self._received_ms: list[float] = []
        self._reported_index: dict[float, int] = {}
        self.reported_past_end_ms: float | None = None

    def add_reported(self, intervals: Iterable[Interval], received_ms: float) -> None:
        """PPI gate/HR evidence on arrival, independently of HRV's guarded lookahead.

        A placeholder is deliberately not HRV-clean. Later classification replaces only its
        clean/difference fields; accepted intervals cannot be counted a second time. The reported
        watermark says the gate's whole time window is available, not that HRV is complete.
        """
        if not self.measured_ppi:
            raise ValueError("reported baseline evidence requires measured PPI mode")
        for interval in intervals:
            if interval.t_beat > self.end_ms:
                if self.reported_past_end_ms is None:
                    self.reported_past_end_ms = received_ms
            elif self.start_ms < interval.t_beat and interval.t_beat not in self._reported_index:
                self._reported_index[interval.t_beat] = len(self._intervals)
                self._intervals.append(
                    HrvInterval(
                        interval.t_beat,
                        interval.rr_ms,
                        interval.accepted,
                        interval.bootstrap,
                        False,
                        None,
                    )
                )
                self._received_ms.append(received_ms)
                self._classified_ms.append(math.inf)

    def add(self, intervals: Iterable[HrvInterval], classified_ms: float = -math.inf) -> None:
        """Intervals as the cleaner classifies them, and when that was (T_engine)."""
        for interval in intervals:
            if interval.t_beat > self.end_ms:
                self._classified_past_end = True
                self._classified_past_end_ms = min(self._classified_past_end_ms, classified_ms)
            elif interval.t_beat > self.start_ms:
                if self.measured_ppi:
                    index = self._reported_index.get(interval.t_beat)
                    if index is not None and self._classified_ms[index] == math.inf:
                        self._intervals[index] = replace(
                            self._intervals[index], clean=interval.clean, diff_ms=interval.diff_ms
                        )
                        self._classified_ms[index] = classified_ms
                else:
                    self._intervals.append(interval)
                    self._classified_ms.append(classified_ms)

    def ready(self, now_ms: float) -> bool:
        """Wait for complete HRV when possible; the session enforces its separate hold cap.

        In PPI mode the gate/HR are available earlier through add_reported, but the immutable
        snapshot waits for this classification watermark or is explicitly closed at the cap.
        """
        return self._classified_past_end or now_ms >= self.end_ms + READY_TIMEOUT_MS

    def progress(self) -> float:
        """How far the intervals classified so far go towards passing the gate, 0 to 1."""
        trusted = self._trusted()
        accepted_ms = sum(covered_ms(i, self.start_ms, self.end_ms) for i in trusted)
        return min(1.0, accepted_ms / MIN_CLEAN_MS, len(trusted) / MIN_ACCEPTED)

    def provisional_quality(self) -> float | None:
        """baseline_quality from the intervals classified so far; None before there is a slope."""
        slope = _slope_bpm_per_min(self._trusted())
        return None if slope is None else _quality(slope)

    def provisional_rmssd_differences(self) -> int:
        """How many differences rmssd_base would rest on, from what has been classified so far."""
        return summarise(self._intervals, self.end_ms - TAIL_MS, self.end_ms).differences

    def _trusted(self, intervals: list[HrvInterval] | None = None) -> list[HrvInterval]:
        chosen = self._intervals if intervals is None else intervals
        return [i for i in chosen if i.accepted and not i.bootstrap]

    def result(self, as_of_ms: float = math.inf) -> Baseline:
        """Evidence available by as_of_ms, not a later packet or classification.

        PPI uses received intervals for HR/gate, and only classified ones for HRV. If lookahead
        has not passed the capture end, RMSSD and HR spread remain unavailable: a partial tail is
        not passed off as a complete baseline. No later enrichment changes a frozen snapshot.
        """
        start, end = self.start_ms, self.end_ms
        if self.measured_ppi:
            intervals = [
                i if classified <= as_of_ms else replace(i, clean=False, diff_ms=None)
                for i, received, classified in zip(
                    self._intervals, self._received_ms, self._classified_ms, strict=True
                )
                if received <= as_of_ms
            ]
        else:
            intervals = [
                i
                for i, at in zip(self._intervals, self._classified_ms, strict=True)
                if at <= as_of_ms
            ]
        complete = not self.measured_ppi or (
            self._classified_past_end and self._classified_past_end_ms <= as_of_ms
        )
        tail = summarise(intervals, end - TAIL_MS, end, MIN_DIFFERENCES)
        trusted = self._trusted(intervals)
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
            rmssd_base_ms=tail.rmssd_ms if complete else None,
            ln_rmssd_base=tail.ln_rmssd if complete else None,
            slope_bpm_per_min=slope,
            baseline_quality=_quality(slope),
            accepted_ms=accepted_ms,
            accepted_intervals=len(trusted),
            passed=not problems,
            problems=tuple(problems),
            hr_sd_bpm=_robust_sd(
                [60_000 / i.rr_ms for i in intervals if i.clean and i.t_beat > end - TAIL_MS]
            )
            if complete
            else None,
            rmssd_base_differences=tail.differences,
            hrv_complete=complete,
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


def _robust_sd(values: list[float]) -> float | None:
    """1.4826 x the median absolute deviation: the standard deviation, if the data were normal."""
    if len(values) < MIN_SLOPE_POINTS:
        return None
    centre = median(values)
    return 1.4826 * median(abs(v - centre) for v in values)


def _quality(slope: float | None) -> float:
    if slope is None:
        return 0.0
    return max(0.0, min(1.0, (STEEP_SLOPE - abs(slope)) / (STEEP_SLOPE - FLAT_SLOPE)))
