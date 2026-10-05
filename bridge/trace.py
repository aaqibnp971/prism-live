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
            highest: float | None = None
            end = load_start_ms + WINDOW_MS
            while end <= load_end + 1e-6:
                candidate = _eligible_bpm(heart_rate_window(chosen, end - WINDOW_MS, end))
                if candidate is not None and (highest is None or candidate > highest):
                    highest = candidate
                end += STEP_MS
            trace["highest_task_bpm"] = highest

    if regulate_start_ms is not None:
        after_start = regulate_start_ms + AFTER_TASK_WINDOW[0]
        after_end = regulate_start_ms + AFTER_TASK_WINDOW[1]
        if now_ms >= after_end + settle_ms:
            trace["after_task_bpm"] = _eligible_bpm(
                heart_rate_window(chosen, after_start, after_end)
            )

    at_rest = trace["at_rest_bpm"]
    task_high = trace["highest_task_bpm"]
    after = trace["after_task_bpm"]
    if (
        heart_rate_activation_passed is True
        and at_rest is not None
        and task_high is not None
        and after is not None
    ):
        fall = round(task_high - after, 1)
        if fall >= SPOKEN_N_MIN_BPM:
            trace["spoken_n_bpm"] = fall

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
