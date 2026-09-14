"""The pose table: a designed effective PSV per segment, for the engine feed's `pose` source.

Under body-derived values the script's regulate comes out inverted (docs/engine-findings.md, "At
the script's own regulate targets"). A pose is the other candidate: per segment, the effective
arousal, cognitive_load and readiness the engine should read, with the body allowed to move
arousal inside a range. Which source ships is decided by listening in Week B (CLAUDE.md open
question 6). These are the findings' starting values; Week B tunes them by ear.

Every number is an effective value, sent as it is with confidence 1.0 (bridge/engine_mapping.py).
A pose is a design, never a reading: the state message keeps reporting the body's psv and
confidence whatever the engine is fed, and nothing here writes to it.

Three shapes, each read from one state message:

- Fixed(v): v.
- BodyRange(lo, hi): lo + (hi - lo) x body_level, where body_level =
  clamp((psv.arousal - 0.5) / 0.35, 0, 1) x confidence.arousal. A reading with no confidence does
  not move a pose. The segment ceiling is not applied: the pose is the segment's design, and
  authority is a statement about the body-derived values, not about this.
- Timed(start, end, from_s_before_end, to_s_before_end): start until from_s_before_end seconds
  before the segment's nominal end, then linear to end at to_s_before_end before it, then end.
  Seconds, not milliseconds: in milliseconds resolve's T-12 s is the authority taper's number,
  which tests/test_authority.py allows only in bridge/contract.py and bridge/authority.py.

Progress comes from segment_elapsed_ms and segment_nominal_ms in the message, never a local
clock: regulate is adaptive.

Load and readiness in regulate step at its first message. Under prompt 2.7's alignment load t=0
sits on a pulse boundary, so regulate begins 75 s later, 9 s into an 11 s pulse loop, and the next
pulse boundary is 2 s into regulate. Pulse's closing crossing has to be in regulate's first PSV to
take that boundary; the engine's own smoothing glides the levels (0.25 s) and the cutoff (0.6 s).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

BODY_SPAN = 0.35  # body arousal this far above neutral takes a BodyRange to its top


@dataclass(frozen=True)
class Fixed:
    value: float

    def at(self, msg: Mapping) -> float:
        return self.value


@dataclass(frozen=True)
class BodyRange:
    lo: float
    hi: float

    def at(self, msg: Mapping) -> float:
        return self.lo + (self.hi - self.lo) * body_level(msg)


@dataclass(frozen=True)
class Timed:
    start: float
    end: float
    from_s_before_end: float
    to_s_before_end: float

    def at(self, msg: Mapping) -> float:
        nominal = msg["segment_nominal_ms"]
        elapsed = msg["segment_elapsed_ms"]
        begins = nominal - 1000 * self.from_s_before_end
        ends = nominal - 1000 * self.to_s_before_end
        if elapsed <= begins:
            return self.start
        if elapsed >= ends:
            return self.end
        return self.start + (self.end - self.start) * (elapsed - begins) / (ends - begins)


Shape = Fixed | BodyRange | Timed


@dataclass(frozen=True)
class Pose:
    arousal: Shape
    cognitive_load: Shape
    readiness: Shape

    def at(self, msg: Mapping) -> dict[str, float]:
        """The effective values this pose sends for one state message."""
        return {
            "arousal": self.arousal.at(msg),
            "cognitive_load": self.cognitive_load.at(msg),
            "readiness": self.readiness.at(msg),
        }


def body_level(msg: Mapping) -> float:
    """How far the body's arousal moves a BodyRange, 0 to 1, weighted by its confidence."""
    rise = (msg["psv"]["arousal"] - 0.5) / BODY_SPAN
    return min(1.0, max(0.0, rise)) * msg["confidence"]["arousal"]


# 1,400 Hz, density 0.3374 (the findings round it to 0.338): pulse and air both closed, bed
# 0.69. The load input sits at 0.65 here and through load, so bed does not step at that boundary;
# the price is a 0.0126 margin under the pulse gate, only 0.0026 inside the feed's hysteresis
# edge. Sent in idle and reset too, so both gates are closed before the next person sits down
# (prompt 2.7).
BASELINE = Pose(Fixed(0.486), Fixed(0.65), Fixed(0.50))

POSES: dict[str, Pose] = {
    "idle": BASELINE,
    "baseline": BASELINE,
    # 1,400 to 3,600 Hz as the body's arousal rises. Pulse opens at an arousal input of 0.50
    # (about 1,470 Hz), air at 0.722 (about 3,070 Hz), before the feed's hysteresis margin.
    "load": Pose(BodyRange(0.486, 0.771), Fixed(0.65), Fixed(0.50)),
    # Both gates closed from the first message: 620 Hz at the bottom of the range, about
    # 1,500 Hz at its top. Bed +3.0 dB and sub +5.0 dB against the end of load.
    "regulate": Pose(BodyRange(0.506, 0.771), Fixed(0.948), Fixed(0.11)),
    # 620 Hz, lifting to 900 Hz across T-20 s to T-12 s as the trace appears (experience script
    # §2); bed and sub held, gates closed. T-12 s is also where the body source's authority has
    # tapered to 0: if resolve's timing moves, move this with it. The ending itself is the
    # session gain (bridge/engine_feed.py), not a PSV.
    "resolve": Pose(Timed(0.506, 0.618, 20, 12), Fixed(0.948), Fixed(0.11)),
    "reset": BASELINE,
}


def pose_inputs(msg: Mapping) -> dict[str, float]:
    """The effective arousal, cognitive_load and readiness the pose source sends for msg."""
    return POSES[msg["segment"]].at(msg)
