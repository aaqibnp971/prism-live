"""Heart rate variability from the beat scheduler's intervals.

RMSSD over a rolling 60 s window, its natural log, and mean heart rate. Nothing else. There
is no LF/HF: frequency-domain HRV needs several minutes of recording and what it means is
contested, so reporting it from 60 s would be indefensible.

The scheduler filters intervals for the rhythm it plays, which is not good enough for HRV on
its own (docs/known-limits.md). IntervalCleaner decides, for every reported interval, whether
it is clean enough for HRV, and whether its successive difference can be used.

An interval is clean when the scheduler accepted it, it is not bootstrap (accepted on
plausibility alone), and nothing suspect lies within 3 s or 6 beats of it, before or after,
whichever reaches further. Suspect means any of:

- an interval the scheduler rejected. Artefacts come in bursts, and the false intervals that
  pass the scheduler's median test sit among ones that fail it, sometimes several beats away.
  Nothing in a false interval's value gives it away; being near a rejection does.
- a misplaced beat: a beat detected late or early leaves a long interval and a short one that
  sum to about two normal intervals, both inside the scheduler's band, so nothing is rejected.
  It is caught by its shape: one interval on each side of the local median, a sum within 5 % of
  twice the median, and a swing between the two of more than 7 quartile deviations of this
  person's recent successive differences and more than 10 % of the median. The idea follows
  Lipponen and Tarvainen (2019), who use 5.2; against the synthetic armband that fired in 19 % of
  clean 90 s runs, and 7 with the floor fires in 3 %.
- a lost or late packet. The packet that went missing may have held a burst's rejections, and
  nothing on either side can prove it did not, so a gap is guarded on both sides.

A successive difference is used only between an interval and the one reported just before it,
both clean, and only when it is within 20 % of the earlier interval, the usual artefact
criterion. Those intervals still count for heart rate; only the difference is left out.

What still gets through: a burst the scheduler accepts whole, with no rejection, or a misplaced
beat whose swing sits inside the person's normal variability. Nothing in the intervals can tell
those from real beats. docs/known-limits.md has the measured rates.

Looking a guard's length ahead means each interval is classified 3 to 7.5 s late at seated
heart rates, plus the armband's own reporting delay. ``horizon_ms`` says how far
classification has reached; readings end there, not at now.

    cleaner, hrv = IntervalCleaner(), RollingHrv()
    hrv.add(cleaner.add(result.intervals))  # every PacketResult, in order
    hrv.reading(now, cleaner.horizon_ms)
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import median, quantiles

from bridge.beat_scheduler import Interval

WINDOW_MS = 60_000
MAX_DIFF_FRACTION = 0.2  # a successive difference beyond this share of the earlier interval
MIN_DIFFERENCES = 10  # fewer than this and RMSSD is too noisy to report
GUARD_MS = 3_000  # nothing suspect this close to a clean interval, before or after,
GUARD_BEATS = 6  # nor this many intervals from it, whichever reaches further
# The misplaced-beat test.
REFERENCE_BEATS = 8  # the local median is taken over this many recent accepted intervals
SPREAD_DIFFS = 32  # this person's recent successive differences, for the swing threshold
MIN_SPREAD_DIFFS = 8  # the test waits until it has this many
SWING_QUARTILE_DEVIATIONS = 7.0
MIN_SWING_FRACTION = 0.10  # of the local median, however steady the run has been
PAIR_SUM_FRACTION = 0.05  # the pair sums to twice the median within this share of it


@dataclass(frozen=True)
class HrvInterval:
    """One reported interval, classified for HRV."""

    t_beat: float  # T_engine ms: when the beat that closed this interval happened
    rr_ms: float
    accepted: bool  # by the scheduler
    bootstrap: bool  # accepted by the scheduler on plausibility alone
    clean: bool  # usable for heart rate and HRV
    diff_ms: float | None  # rr_ms minus the previous beat's, when usable for RMSSD


@dataclass(frozen=True)
class HrvReading:
    start_ms: float  # the window is start_ms < t_beat <= end_ms
    end_ms: float  # where classification had reached, never later than now
    lag_ms: float  # now minus end_ms: grows when the armband goes quiet
    mean_hr_bpm: float | None  # clean beats per minute of clean interval time
    rmssd_ms: float | None  # None below MIN_DIFFERENCES
    ln_rmssd: float | None
    intervals: int  # clean intervals in the window
    differences: int  # successive differences used
    covered_ms: float  # clean interval time inside the window


class IntervalCleaner:
    """Classifies the scheduler's intervals for HRV, a guard's length late. Feed every interval."""

    def __init__(
        self,
        max_diff_fraction: float = MAX_DIFF_FRACTION,
        guard_ms: float = GUARD_MS,
        guard_beats: int = GUARD_BEATS,
    ) -> None:
        self.max_diff_fraction = max_diff_fraction
        self.guard_ms = guard_ms
        self.guard_beats = guard_beats
        self.horizon_ms: float | None = None  # every interval up to here has been classified
        self._pending: deque[list] = deque()  # [interval, near something suspect]
        self._previous: Interval | None = None  # the last classified interval in this run
        self._previous_clean = False
        self._start_run()

    def add(self, intervals: Iterable[Interval]) -> list[HrvInterval]:
        """Take intervals in the order reported; return those that can now be classified."""
        classified: list[HrvInterval] = []
        for interval in intervals:
            if not interval.contiguous:
                self._gap(interval, classified)
            self._beats_since_suspect += 1
            near = (
                interval.t_beat - self._last_suspect <= self.guard_ms
                or self._beats_since_suspect <= self.guard_beats
            )
            misplaced = self._misplaced(interval)
            if not interval.accepted or misplaced:
                self._suspect(interval.t_beat)
                near = True
            self._pending.append([interval, near])
            self._remember(interval, misplaced)
            while self._pending and self._seen_past_guard():
                classified.append(self._classify(*self._pending.popleft()))
        return classified

    # --- runs, gaps and suspects ---

    def _start_run(self) -> None:
        self._last_suspect = -math.inf  # t_beat of the latest suspect in this run
        self._beats_since_suspect = math.inf  # intervals reported since then
        self._last_reported: Interval | None = None
        self._accepted_rr: deque[float] = deque(maxlen=REFERENCE_BEATS)
        self._diffs: deque[float] = deque(maxlen=SPREAD_DIFFS)

    def _gap(self, interval: Interval, classified: list[HrvInterval]) -> None:
        had_run = self._last_reported is not None
        if had_run and self._pending:
            self._mark_before(self._pending[-1][0].t_beat)
        while self._pending:
            classified.append(self._classify(*self._pending.popleft()))
        self._start_run()
        self._previous = None
        if had_run:
            # Whatever the gap hid may have come right before this interval: guard after it too.
            self._last_suspect = interval.t_beat - interval.rr_ms
            self._beats_since_suspect = -1

    def _suspect(self, t_beat: float) -> None:
        self._mark_before(t_beat)
        self._last_suspect = t_beat
        self._beats_since_suspect = 0

    def _mark_before(self, t_edge: float) -> None:
        for back, entry in enumerate(reversed(self._pending), start=1):
            if back <= self.guard_beats or t_edge - entry[0].t_beat <= self.guard_ms:
                entry[1] = True

    def _misplaced(self, interval: Interval) -> bool:
        """The second interval of a long-short or short-long pair from one misplaced beat."""
        before = self._last_reported
        if (
            not interval.accepted
            or not interval.contiguous
            or before is None
            or not before.accepted
            or len(self._diffs) < MIN_SPREAD_DIFFS
        ):
            return False
        centre = median(self._accepted_rr)
        q1, _, q3 = quantiles(self._diffs, n=4)
        threshold = max(SWING_QUARTILE_DEVIATIONS * (q3 - q1) / 2, MIN_SWING_FRACTION * centre)
        return (
            abs(interval.rr_ms - before.rr_ms) > threshold
            and (before.rr_ms - centre) * (interval.rr_ms - centre) < 0
            and abs(before.rr_ms + interval.rr_ms - 2 * centre) <= PAIR_SUM_FRACTION * centre
        )

    def _remember(self, interval: Interval, misplaced: bool) -> None:
        before = self._last_reported
        if interval.accepted:
            if before is not None and before.accepted and not misplaced:
                self._diffs.append(interval.rr_ms - before.rr_ms)
            self._accepted_rr.append(interval.rr_ms)
        self._last_reported = interval

    def _seen_past_guard(self) -> bool:
        # Strict on time and inclusive on beats, so nothing still ahead could mark the oldest.
        oldest, newest = self._pending[0][0], self._pending[-1][0]
        return (
            newest.t_beat - oldest.t_beat > self.guard_ms
            and len(self._pending) - 1 >= self.guard_beats
        )

    def _classify(self, interval: Interval, near_suspect: bool) -> HrvInterval:
        clean = interval.accepted and not interval.bootstrap and not near_suspect
        diff = None
        if clean and interval.contiguous and self._previous is not None and self._previous_clean:
            change = interval.rr_ms - self._previous.rr_ms
            if abs(change) <= self.max_diff_fraction * self._previous.rr_ms:
                diff = change
        self._previous, self._previous_clean = interval, clean
        self.horizon_ms = interval.t_beat
        return HrvInterval(
            interval.t_beat, interval.rr_ms, interval.accepted, interval.bootstrap, clean, diff
        )


class RollingHrv:
    """RMSSD, ln RMSSD and mean heart rate over the last WINDOW_MS of classified beats."""

    def __init__(self, window_ms: float = WINDOW_MS, min_differences: int = MIN_DIFFERENCES):
        self.window_ms = window_ms
        self.min_differences = min_differences
        self._clean: deque[HrvInterval] = deque()

    def add(self, intervals: Iterable[HrvInterval]) -> None:
        self._clean.extend(i for i in intervals if i.clean)

    def reading(self, now_ms: float, horizon_ms: float | None = None) -> HrvReading:
        """The window ends at horizon_ms, where classification has reached, if given.

        Pass the cleaner's horizon_ms in live use. Without it the newest 3 to 7.5 s of the window
        are always empty, because nothing there has been classified yet.
        """
        end = now_ms if horizon_ms is None else min(now_ms, horizon_ms)
        start = end - self.window_ms
        while self._clean and self._clean[0].t_beat <= start:
            self._clean.popleft()
        return summarise(self._clean, start, end, self.min_differences, lag_ms=now_ms - end)


def summarise(
    intervals: Iterable[HrvInterval],
    start_ms: float,
    end_ms: float,
    min_differences: int = MIN_DIFFERENCES,
    lag_ms: float = 0.0,
) -> HrvReading:
    """Heart rate and RMSSD from the clean intervals whose beat falls in (start_ms, end_ms]."""
    clean = [i for i in intervals if i.clean and start_ms < i.t_beat <= end_ms]
    diffs = [i.diff_ms for i in clean if i.diff_ms is not None]
    total_rr = sum(i.rr_ms for i in clean)
    rmssd = (
        math.sqrt(sum(d * d for d in diffs) / len(diffs)) if len(diffs) >= min_differences else None
    )
    return HrvReading(
        start_ms=start_ms,
        end_ms=end_ms,
        lag_ms=lag_ms,
        mean_hr_bpm=60_000 * len(clean) / total_rr if clean and total_rr > 0 else None,
        rmssd_ms=rmssd,
        ln_rmssd=math.log(rmssd) if rmssd else None,
        intervals=len(clean),
        differences=len(diffs),
        covered_ms=sum(covered_ms(i, start_ms, end_ms) for i in clean),
    )


def covered_ms(interval: HrvInterval, start_ms: float, end_ms: float) -> float:
    """How much of the time between this beat and the one before it lies in the window."""
    return max(0.0, min(interval.t_beat, end_ms) - max(interval.t_beat - interval.rr_ms, start_ms))
