"""Host-owned heart-rate evidence for the spectator trace reveal.

All arithmetic in this module uses accepted, non-bootstrap intervals placed on the scheduler's
reconstructed measurement timeline.  Playback time is used only after a result exists, to give
the spectator an x-coordinate that lines up with the delayed beat trace.  The browser never
subtracts the PPI buffer and never decides whether a window has enough evidence.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

WINDOW_MS = 30_000.0
MIN_COVERED_MS = 22_500.0
STEP_MS = 500.0
BASELINE_WINDOW = (15_000.0, 45_000.0)
AFTER_TASK_WINDOW = (45_000.0, 75_000.0)
SPOKEN_N_MIN_BPM = 3.0

HeartInterval = tuple[float, float]  # reconstructed t_beat, accepted rr_ms


@dataclass(frozen=True)
class HeartRateWindow:
    bpm: float | None
    covered_ms: float
    intervals: int


def heart_rate_window(
    intervals: Iterable[HeartInterval], start_ms: float, end_ms: float
) -> HeartRateWindow:
    """Return the interval-count mean over ``(start_ms, end_ms]``.

    The formula deliberately matches baseline and activation: 60,000 times interval count,
    divided by the sum of the complete accepted intervals.  Coverage is clipped at the left
    window edge and is a separate eligibility test; it is not used as a weighting shortcut.
    """
    if not _finite(start_ms) or not _finite(end_ms) or end_ms <= start_ms:
        return HeartRateWindow(None, 0.0, 0)
    count = 0
    total = 0.0
    covered = 0.0
    for t_beat, rr_ms in intervals:
        if start_ms < t_beat <= end_ms:
            count += 1
            total += rr_ms
            covered += max(0.0, t_beat - max(t_beat - rr_ms, start_ms))
    bpm = 60_000.0 * count / total if count and total > 0 else None
    return HeartRateWindow(bpm, covered, count)


def empty_trace() -> dict:
    """A fresh session's contract-shaped, not-yet-known trace evidence."""
    return {
        "at_rest_bpm": None,
        "highest_task_bpm": None,
        "after_task_bpm": None,
        "spoken_n_bpm": None,
        "average_30s": [],
    }


def build_trace(
    intervals: Iterable[HeartInterval],
    *,
    now_ms: float,
    settle_ms: float,
    playback_delay_ms: float,
    baseline_start_ms: float | None,
    load_start_ms: float | None,
    regulate_start_ms: float | None,
    resolve_end_ms: float | None,
    heart_rate_activation_passed: bool | None,
) -> dict:
    """Build the contract's complete host-owned ``state.trace`` value.

    Card windows become available only after their end plus the source-specific settlement
    interval.  The after-task window is fixed at regulate seconds 45--75 even if regulate later
    extends.  Average-line points are finalized only when both their measurements have settled
    and their host-projected playback coordinate is no later than ``now_ms``.
    """
    chosen = tuple(intervals)
    trace = empty_trace()
    if baseline_start_ms is None:
        return trace

    baseline_end = baseline_start_ms + BASELINE_WINDOW[1]
    if now_ms >= baseline_end + settle_ms:
        window = heart_rate_window(
            chosen,
            baseline_start_ms + BASELINE_WINDOW[0],
            baseline_end,
        )
        trace["at_rest_bpm"] = _eligible_bpm(window)

    if load_start_ms is not None and regulate_start_ms is not None:
        load_end = regulate_start_ms
        if now_ms >= load_end + settle_ms:
            trace["highest_task_bpm"] = _highest_task_bpm(chosen, load_start_ms, load_end)

    if regulate_start_ms is not None:
        after_start = regulate_start_ms + AFTER_TASK_WINDOW[0]
        after_end = regulate_start_ms + AFTER_TASK_WINDOW[1]
        if now_ms >= after_end + settle_ms:
            trace["after_task_bpm"] = _eligible_bpm(
                heart_rate_window(chosen, after_start, after_end)
            )

    trace["spoken_n_bpm"] = _spoken_n(trace, heart_rate_activation_passed)

    latest_end = min(now_ms - settle_ms, now_ms - playback_delay_ms)
    if resolve_end_ms is not None:
        # A point's host-supplied t_play must not extend past the completed session trace.
        latest_end = min(latest_end, resolve_end_ms - playback_delay_ms)
    end = baseline_start_ms + WINDOW_MS
    points: list[dict[str, int | float | None]] = []
    while end <= latest_end + 1e-6:
        window = heart_rate_window(chosen, end - WINDOW_MS, end)
        points.append(
            {
                "t_play": round(end + playback_delay_ms),
                "hr_bpm": _eligible_bpm(window),
            }
        )
        end += STEP_MS
    trace["average_30s"] = points
    return trace


class TraceBuilder:
    """One session's ``state.trace``, built forward and never revised.

    ``build_trace`` recomputes the whole history from whatever intervals exist now. Run on
    every state message that costs tens of milliseconds late in a session, on the loop that
    schedules beats, and a burst arriving after its settlement interval silently changes
    numbers already published. This holds each card from the moment it is first decided, null
    included, and each average-line point from the moment it is first published. With every
    interval inside its settlement interval the two give identical results.
    """

    _UNDECIDED = object()

    def __init__(self) -> None:
        self._cards: dict[str, object] = dict.fromkeys(
            ("at_rest_bpm", "highest_task_bpm", "after_task_bpm"), self._UNDECIDED
        )
        self._points: list[dict[str, int | float | None]] = []
        self._next_end: float | None = None

    def build(
        self,
        intervals: Iterable[HeartInterval],
        *,
        now_ms: float,
        settle_ms: float,
        playback_delay_ms: float,
        baseline_start_ms: float | None,
        load_start_ms: float | None,
        regulate_start_ms: float | None,
        resolve_end_ms: float | None,
        heart_rate_activation_passed: bool | None,
    ) -> dict:
        trace = empty_trace()
        if baseline_start_ms is None:
            return trace
        chosen = tuple(intervals)

        baseline_end = baseline_start_ms + BASELINE_WINDOW[1]
        if self._cards["at_rest_bpm"] is self._UNDECIDED and now_ms >= baseline_end + settle_ms:
            self._cards["at_rest_bpm"] = _eligible_bpm(
                heart_rate_window(chosen, baseline_start_ms + BASELINE_WINDOW[0], baseline_end)
            )
        if (
            self._cards["highest_task_bpm"] is self._UNDECIDED
            and load_start_ms is not None
            and regulate_start_ms is not None
            and now_ms >= regulate_start_ms + settle_ms
        ):
            self._cards["highest_task_bpm"] = _highest_task_bpm(
                chosen, load_start_ms, regulate_start_ms
            )
        if self._cards["after_task_bpm"] is self._UNDECIDED and regulate_start_ms is not None:
            after_end = regulate_start_ms + AFTER_TASK_WINDOW[1]
            if now_ms >= after_end + settle_ms:
                self._cards["after_task_bpm"] = _eligible_bpm(
                    heart_rate_window(chosen, regulate_start_ms + AFTER_TASK_WINDOW[0], after_end)
                )
        for key, value in self._cards.items():
            trace[key] = None if value is self._UNDECIDED else value
        trace["spoken_n_bpm"] = _spoken_n(trace, heart_rate_activation_passed)

        latest_end = min(now_ms - settle_ms, now_ms - playback_delay_ms)
        if resolve_end_ms is not None:
            latest_end = min(latest_end, resolve_end_ms - playback_delay_ms)
        if self._next_end is None:
            self._next_end = baseline_start_ms + WINDOW_MS
        while self._next_end <= latest_end + 1e-6:
            window = heart_rate_window(chosen, self._next_end - WINDOW_MS, self._next_end)
            self._points.append(
                {
                    "t_play": round(self._next_end + playback_delay_ms),
                    "hr_bpm": _eligible_bpm(window),
                }
            )
            self._next_end += STEP_MS
        trace["average_30s"] = [dict(point) for point in self._points]
        return trace


def _highest_task_bpm(
    intervals: tuple[HeartInterval, ...], load_start_ms: float, load_end_ms: float
) -> float | None:
    highest: float | None = None
    end = load_start_ms + WINDOW_MS
    while end <= load_end_ms + 1e-6:
        candidate = _eligible_bpm(heart_rate_window(intervals, end - WINDOW_MS, end))
        if candidate is not None and (highest is None or candidate > highest):
            highest = candidate
        end += STEP_MS
    return highest


def _spoken_n(trace: dict, heart_rate_activation_passed: bool | None) -> float | None:
    """Displayed task-high minus displayed after-task, only after HR activation and a fall of
    at least 3.0 bpm, and only with all three cards."""
    task_high, after = trace["highest_task_bpm"], trace["after_task_bpm"]
    if (
        heart_rate_activation_passed is not True
        or trace["at_rest_bpm"] is None
        or task_high is None
        or after is None
    ):
        return None
    fall = round(task_high - after, 1)
    return fall if fall >= SPOKEN_N_MIN_BPM else None


def _eligible_bpm(window: HeartRateWindow) -> float | None:
    if window.covered_ms < MIN_COVERED_MS or window.bpm is None:
        return None
    return round(window.bpm, 1)


def _finite(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
    )
