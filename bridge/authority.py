"""Authority: how hard the laptop lets each dimension push the sound and the picture.

    authority = min(confidence, segment ceiling), per dimension

This is the only place authority is computed. It is computed once, on the laptop, carried in the
state message (bridge/state.py), and read by every client and by the engine feed. Nothing else in
this repo or in any client may compute it, so the sound and the picture can never disagree.

The ceilings are contract §2's table, `CEILINGS` in bridge/contract.py, which matches CLAUDE.md:
idle, baseline and reset 0; load 0.2 on arousal and cognitive_load; regulate 1.0 on everything but
valence. Valence is 0 in every segment.

Resolve tapers. Each dimension's ceiling starts at the authority that dimension had when resolve
began, and falls linearly to 0 across T-45 s to T-12 s, where T is the segment's nominal end; from
T-12 s to T it is 0. Resolve is 45 s, so the taper runs from its first moment to 33 s in. If
resolve is ever shortened, the taper and the contract's check of it must be revisited together.

Values go out floored to 3 decimals, computed exactly in thousandths, so authority never exceeds
the confidence or the ceiling it came from, and never falls short of it by float error. Anything
that is not a finite number is treated as 0: no authority.

The segment clock and the resolve entry are required, so a call without them fails loudly instead
of quietly granting nothing. Only bridge/state.py calls this; everything else reads the authority
the state message carries.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from fractions import Fraction

from bridge.contract import CEILINGS, DIMENSIONS, RESOLVE_SILENT_MS, RESOLVE_TAPER_MS

DECIMALS = 3
_NOTHING = dict.fromkeys(DIMENSIONS, 0.0)


def authority(
    confidence: Mapping[str, float],
    segment: str,
    *,
    segment_elapsed_ms: float,
    segment_nominal_ms: float,
    resolve_entry: Mapping[str, float] | None,
) -> dict[str, float]:
    """min(confidence, ceiling) for each of the four dimensions, as the state message carries it.

    confidence: the four confidences, as a mapping or anything with to_wire() (psv.Confidences).
    resolve_entry: in resolve, the authority each dimension had when resolve began. Without it,
    resolve has nothing to taper from, and gives no authority.
    """
    have = {d: _thousandths(v) for d, v in _four(confidence).items()}
    if segment == "resolve":
        entry = _four(resolve_entry)
        remaining = _resolve_remaining(segment_elapsed_ms, segment_nominal_ms)
        ceiling = {
            d: math.floor(
                min(_thousandths(CEILINGS["resolve"][d]), _thousandths(entry[d]))
                * remaining
                / RESOLVE_TAPER_MS
            )
            for d in DIMENSIONS
        }
    else:
        table = CEILINGS.get(segment, _NOTHING) if isinstance(segment, str) else _NOTHING
        ceiling = {d: _thousandths(table[d]) for d in DIMENSIONS}
    return {d: min(have[d], ceiling[d]) / 10**DECIMALS + 0.0 for d in DIMENSIONS}


def resolve_taper(segment_elapsed_ms: float, segment_nominal_ms: float) -> float:
    """1 at T-45 s, 0 from T-12 s on, linear between. T is the nominal end of resolve."""
    return float(_resolve_remaining(segment_elapsed_ms, segment_nominal_ms) / RESOLVE_TAPER_MS)


def _resolve_remaining(segment_elapsed_ms: float, segment_nominal_ms: float) -> Fraction:
    """How much of the taper is left, in ms, exactly: 33,000 at T-45 s, 0 from T-12 s."""
    elapsed, nominal = _finite(segment_elapsed_ms), _finite(segment_nominal_ms)
    if elapsed is None or nominal is None:
        return Fraction(0)
    left = Fraction(nominal) - RESOLVE_SILENT_MS - Fraction(elapsed)
    return min(Fraction(RESOLVE_TAPER_MS), max(Fraction(0), left))


def _four(values: object) -> dict[str, float]:
    if hasattr(values, "to_wire"):
        values = values.to_wire()
    if not isinstance(values, Mapping):
        return dict(_NOTHING)
    out = {}
    for d in DIMENSIONS:
        value = _finite(values.get(d))
        out[d] = 0.0 if value is None else min(1.0, max(0.0, value))
    return out


def _finite(x: object) -> float | None:
    if isinstance(x, bool) or not isinstance(x, int | float):
        return None
    if isinstance(x, int) and abs(x) > 2**53:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def _thousandths(x: float) -> Fraction:
    """x floored to whole thousandths, exactly. 0.2 is 200, however the float stores it."""
    step = 10**DECIMALS
    return Fraction(math.floor(Fraction(repr(x)) * step))
