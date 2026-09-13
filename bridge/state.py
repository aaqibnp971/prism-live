"""The state message (contract §2), built from the PSV estimate, with authority computed here.

One StateStream per session. It numbers the messages, computes authority once per message with
bridge/authority.py from the very confidences and segment clock the message carries, and
remembers the authority each dimension had when resolve began, which is where resolve's taper
starts. The session state machine (prompt 2.4) owns the segment clock, hr_base and
baseline_quality, and passes them in. The contract's rules on them are enforced here, whatever is
passed: hr_base is null in idle and baseline (it exists only once baseline has ended, contract
§2), and a degraded session sends hr_base null and baseline_quality 0.0 (contract v1.3).

    stream = StateStream(session)
    msg = stream.message(
        t_engine_ms=now, t_session_ms=now - start, segment="load",
        segment_elapsed_ms=elapsed, segment_nominal_ms=75_000,
        estimate=model.estimate(now), hr_base_bpm=hr_base, baseline_quality=quality,
    )
    server.publish(msg)
"""

from __future__ import annotations

import math

from bridge.authority import authority
from bridge.psv import BaselinePhase, PsvEstimate


class StateStream:
    """The state messages of one session, in order."""

    def __init__(self, session: str) -> None:
        self.session = session
        self._seq = 0
        self._last_authority: dict[str, float] | None = None
        self._resolve_entry: dict[str, float] | None = None
        self._in_resolve = False

    def message(
        self,
        *,
        t_engine_ms: float,
        t_session_ms: float | None,
        segment: str,
        segment_elapsed_ms: float,
        segment_nominal_ms: float,
        estimate: PsvEstimate,
        hr_base_bpm: float | None,
        baseline_quality: float,
    ) -> dict:
        """The next state message. Validate it (server.publish does) before it goes out."""
        if segment == "resolve" and not self._in_resolve:
            # The taper starts from where authority stood as resolve began.
            self._resolve_entry = self._last_authority
        self._in_resolve = segment == "resolve"
        if not self._in_resolve:
            self._resolve_entry = None

        elapsed, nominal = round(segment_elapsed_ms), round(segment_nominal_ms)
        confidence = estimate.confidence.to_wire()
        granted = authority(
            confidence,
            segment,
            segment_elapsed_ms=elapsed,
            segment_nominal_ms=nominal,
            resolve_entry=self._resolve_entry,
        )
        degraded = estimate.phase is BaselinePhase.DEGRADED
        before_baseline_ended = segment in ("idle", "baseline")
        self._last_authority = granted
        self._seq += 1
        signal = estimate.signal
        return {
            "type": "state",
            "v": 1,
            "session": self.session,
            "seq": self._seq,
            "t_engine": round(t_engine_ms),
            "t_session": None if segment == "idle" or t_session_ms is None else round(t_session_ms),
            "segment": segment,
            "segment_elapsed_ms": elapsed,
            "segment_nominal_ms": nominal,
            "psv": {d: round(v, 3) for d, v in estimate.psv.to_wire().items()},
            "confidence": confidence,
            "authority": granted,
            "hr_bpm": None if estimate.hr_bpm is None else round(estimate.hr_bpm, 1),
            "hr_base": None if degraded or before_baseline_ended else _positive(hr_base_bpm),
            "signal": {
                "contact": signal.contact is not False,
                "rr_accepted_pct": round(signal.accepted_fraction or 0.0, 3),
                "baseline_quality": 0.0
                if degraded
                else round(min(1.0, max(0.0, _finite(baseline_quality))), 3),
            },
        }


def _finite(x: object) -> float:
    if isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x):
        return 0.0
    return float(x)


def _positive(x: object) -> float | None:
    value = _finite(x)
    return round(value, 1) if value > 0 else None
