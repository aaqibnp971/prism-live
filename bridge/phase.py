"""Sample-exact phase and gate-boundary planning for the one Prism scene (prompt 2.7).

The engine's scene phase is exactly the number of frames the shim has passed to
``prism_render``.  All calculations here are integers: no wall clock is used to reconstruct an
audio phase.  A gate crossing is sent one maximum render block before its chosen loop boundary,
so the engine consumes it in time for that boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

SAMPLE_RATE = 48_000
STEM_FRAMES = {
    "bed": 912_000,
    "sub": 816_000,
    "pulse": 528_000,
    "air": 624_000,
}
PULSE_ALIGNMENT_PHASE = 10 * SAMPLE_RATE
BASELINE_TO_LOAD_FRAMES = 56 * SAMPLE_RATE
LOAD_FRAMES = 75 * SAMPLE_RATE


class FrameSource(Protocol):
    def frames_rendered(self) -> int: ...


@dataclass(frozen=True)
class GateTiming:
    stem: str
    boundary_frame: int
    send_frame: int


@dataclass(frozen=True)
class StartAlignment:
    pressed_frame: int
    fire_frame: int

    @property
    def wait_frames(self) -> int:
        return self.fire_frame - self.pressed_frame

    @property
    def wait_ms(self) -> float:
        return self.wait_frames * 1000.0 / SAMPLE_RATE


@dataclass(frozen=True)
class SessionGatePlan:
    """The fixed boundaries implied by one aligned baseline start."""

    baseline_frame: int
    load_frame: int
    regulate_frame: int
    pulse_open: GateTiming
    air_close: GateTiming
    pulse_close: GateTiming


class PhaseTracker:
    """Read phase from a shim and plan exact stem boundaries."""

    def __init__(self, source: FrameSource, block_frames: int) -> None:
        if not isinstance(block_frames, int) or isinstance(block_frames, bool) or block_frames <= 0:
            raise ValueError("block_frames must be a positive integer")
        self._source = source
        self.block_frames = block_frames

    @classmethod
    def for_shim(cls, shim: FrameSource) -> PhaseTracker:
        return cls(shim, int(shim.config.max_block_frames))  # type: ignore[attr-defined]

    def frame(self) -> int:
        frame = self._source.frames_rendered()
        if not isinstance(frame, int) or isinstance(frame, bool) or frame < 0:
            raise ValueError("frames_rendered must be a non-negative integer")
        return frame

    def phase_frames(self, stem: str, at_frame: int | None = None) -> int:
        frame = self.frame() if at_frame is None else _frame(at_frame)
        return frame % _loop(stem)

    def next_boundary(self, stem: str, after_frame: int | None = None) -> int:
        """The boundary strictly after ``after_frame``, matching the engine."""
        frame = self.frame() if after_frame is None else _frame(after_frame)
        loop = _loop(stem)
        return (frame // loop + 1) * loop

    def boundary_before(self, stem: str, before_frame: int) -> int:
        """The boundary strictly before ``before_frame``."""
        frame, loop = _frame(before_frame), _loop(stem)
        if frame <= 0:
            raise ValueError("there is no boundary before frame zero")
        return ((frame - 1) // loop) * loop

    def nearest_boundary(self, stem: str, target_frame: int) -> int:
        """Nearest boundary to a target; an exact tie chooses the earlier one."""
        target, loop = _frame(target_frame), _loop(stem)
        before = target // loop * loop
        after = before if before == target else before + loop
        return before if target - before <= after - target else after

    def gate_timing(self, stem: str, boundary_frame: int) -> GateTiming:
        boundary, loop = _frame(boundary_frame), _loop(stem)
        if boundary == 0 or boundary % loop:
            raise ValueError(f"frame {boundary} is not a positive {stem} boundary")
        return GateTiming(stem, boundary, max(0, boundary - self.block_frames))

    def start_alignment(self, pressed_frame: int | None = None) -> StartAlignment:
        """When baseline may start so a pulse boundary falls exactly 56 s later."""
        pressed = self.frame() if pressed_frame is None else _frame(pressed_frame)
        loop = STEM_FRAMES["pulse"]
        # (-56 s) mod 11 s is 10 s. Keep the expression pinned by the assertion below.
        target = (-BASELINE_TO_LOAD_FRAMES) % loop
        assert target == PULSE_ALIGNMENT_PHASE
        wait = (target - pressed % loop) % loop
        return StartAlignment(pressed, pressed + wait)

    def session_gate_plan(self, baseline_frame: int) -> SessionGatePlan:
        """Plan load's pulse open and regulate's ordered air/pulse closes.

        Air uses the last air boundary before pulse's first boundary after regulate starts.  Its
        fade therefore starts first and also finishes first, preserving the nested-gate rule.
        """
        baseline = _frame(baseline_frame)
        if self.phase_frames("pulse", baseline) != PULSE_ALIGNMENT_PHASE:
            raise ValueError("baseline does not start at pulse phase 10 s")
        load = baseline + BASELINE_TO_LOAD_FRAMES
        assert load % STEM_FRAMES["pulse"] == 0
        regulate = load + LOAD_FRAMES
        pulse_boundary = self.next_boundary("pulse", regulate)
        air_boundary = self.boundary_before("air", pulse_boundary)
        return SessionGatePlan(
            baseline,
            load,
            regulate,
            self.gate_timing("pulse", load),
            self.gate_timing("air", air_boundary),
            self.gate_timing("pulse", pulse_boundary),
        )


def frames_from_ms(milliseconds: float) -> int:
    return round(milliseconds * SAMPLE_RATE / 1000.0)


def ms_from_frames(frames: int) -> float:
    return _frame(frames) * 1000.0 / SAMPLE_RATE


def _loop(stem: str) -> int:
    try:
        return STEM_FRAMES[stem]
    except KeyError as error:
        raise ValueError(f"unknown stem {stem!r}") from error


def _frame(frame: int) -> int:
    if not isinstance(frame, int) or isinstance(frame, bool) or frame < 0:
        raise ValueError("frame must be a non-negative integer")
    return frame
