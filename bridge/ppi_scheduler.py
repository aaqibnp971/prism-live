"""Delayed playback of *measured* optical intervals; never an extrapolated lattice.

The phone provides no acquisition timestamps. We anchor a burst's newest beat 2000 ms
before its laptop callback, then chain every PPI (including rejected ones). The absolute
anchor is an estimate, NOT measured physiological latency. Playback is 12 s behind that
reconstruction: the capture's longest within-burst lookback was 7.858 s. This leaves
room for coalescing and the mandatory 300 ms publication lead. The conservative newest-
beat anchor covers a complete plausible interval, avoiding future reconstructed beats
when the packet/heart phases shift. A 4 s lag envelope accommodates that uncertainty.

Normal packets correct the reconstructed phase by at most 4% of the smallest interval
at the burst boundary. A timestamp impossibility or a missing burst is a discontinuity,
not permission to stretch a measured RR or invent replacement beats. All rejected
intervals still advance time. Quality is blocker=false AND errorEstimate <= 10% of PPI,
then the unchanged plausibility/median test. The relative cutoff is a provisional local
policy, not a Polar accuracy guarantee or an ectopic-threshold retune. Contact and the
sensor's HR are deliberately ignored (both were positive off-arm in the capture).
"""

from __future__ import annotations

import math
from collections import deque

from bridge.beat_scheduler import (
    OK,
    REJECTED,
    BeatEvent,
    BeatScheduler,
    Interval,
    PacketResult,
    Tuning,
)
from bridge.packets import PpiPacket

PPI_MAX_ERROR_FRACTION = 0.10
MAX_PENDING = 256


class PpiBeatScheduler(BeatScheduler):
    def __init__(self) -> None:
        super().__init__(Tuning.ppi())
        self._pending: deque[tuple[float, float, float]] = deque()
        self._source_id: str | None = None
        self._last_queued: float | None = None
        self.quality_rejected = 0
        self.discontinuities = 0
        self._session_start = -math.inf
        self.before_session = 0

    def begin_session(self, now: float) -> None:
        """Never relabel queued or subsequently arriving pre-visitor measurements."""
        self._session_start = now
        keep = deque(item for item in self._pending if item[0] - self.t.buffer_ms >= now)
        self.before_session += len(self._pending) - len(keep)
        self._pending = keep

    @property
    def running(self) -> bool:
        return bool(self._pending)

    def on_packet(self, now: float, payload: PpiPacket) -> PacketResult:
        if not isinstance(payload, PpiPacket):
            raise TypeError("PPI scheduler requires PpiPacket, not a fabricated HRM packet")
        if not math.isfinite(now) or not math.isfinite(payload.arrived_ms):
            raise ValueError("finite monotonic arrival required")
        if payload.arrived_ms > now or now - payload.arrived_ms > 1500:
            # An event-loop stall must not freshen an old packet.
            self.stats.skipped += len(payload.samples)
            self._needs_anchor = True
            return PacketResult((), ())
        samples = payload.samples
        if not samples:
            return PacketResult((), ())
        if len(samples) > 128 or any(
            not math.isfinite(s.rr_ms) or not 1 <= s.rr_ms <= 60_000
            or not math.isfinite(s.error_ms) or s.error_ms < 0 for s in samples
        ):
            raise ValueError("invalid PPI sample")
        arrival = payload.arrived_ms
        if self._last_arrival is not None and arrival <= self._last_arrival:
            self.stats.skipped += len(samples)
            return PacketResult((), ())
        gap = (
            self._needs_anchor or payload.discontinuity or payload.source_id != self._source_id
            or (self._last_arrival is not None
                and arrival - self._last_arrival > self.t.link_timeout_ms)
        )
        span = sum(s.rr_ms for s in samples)
        newest = arrival - self.t.anchor_lag_ms
        correction = 0.0
        if not gap and self._chain_t is not None:
            predicted = self._chain_t + span
            # A plausible notification-lag envelope allows jitter without reshaping the heart.
            error = newest - predicted
            limit = self.t.max_step_fraction * min(s.rr_ms for s in samples)
            correction = max(-limit, min(limit, error))
            newest = predicted + correction
            if not arrival - self.t.max_lag_ms <= newest <= arrival - self.t.min_lag_ms:
                gap = True
                newest = arrival - self.t.anchor_lag_ms
                correction = 0.0
        if gap:
            self.discontinuities += 1
            self.stats.anchors += 1
            if self._last_arrival is not None:
                self.stats.link_gaps += 1
            self._window.clear()
            self._rejects_in_a_row = 0
        chain = newest - span
        previous = self._chain_t
        intervals, events = [], []
        for index, sample in enumerate(samples):
            chain += sample.rr_ms
            # A bad batch can overlap the already consumed measurement timeline. Never feed
            # duplicate time into baseline/HRV, or move it forward and call it fresh.
            if previous is not None and chain <= previous:
                self.stats.skipped += 1
                gap = True
                continue
            quality_ok = (
                not sample.blocked
                and sample.error_ms <= PPI_MAX_ERROR_FRACTION * sample.rr_ms
            )
            if quality_ok:
                accepted, bootstrap = self._accept(sample.rr_ms)
            else:
                accepted, bootstrap = False, False
                self.quality_rejected += 1
            interval = Interval(
                chain, sample.rr_ms, accepted, arrival,
                contiguous=not gap, bootstrap=bootstrap,
            )
            intervals.append(interval)
            gap = False
            target = chain + self.t.buffer_ms
            if accepted:
                self.stats.accepted += 1
                self._interval = sample.rr_ms
                self._ref_t = chain
                self._last_accepted_arrival = arrival
                if chain < self._session_start:
                    self.before_session += 1
                elif (target < now + self.t.lead_ms + 1
                    or len(self._pending) >= MAX_PENDING
                    or (self._last_queued is not None
                        and target < self._last_queued + self.t.min_spacing_ms)):
                    self.stats.skipped += 1
                else:
                    self._pending.append((target, sample.rr_ms, correction if index == 0 else 0.0))
                    self._last_queued = target
            else:
                self.stats.rejected += 1
                # Rejection counters retain all evidence, but the wire cannot carry
                # negative/unsafe timestamps from an overlong startup batch.
                if 0 <= round(target) <= 2**53 - 1 and chain >= self._session_start:
                    events.append(self._event(target, sample.rr_ms, REJECTED, now, 0.0))
            previous = chain
        self._chain_t = max(previous, newest) if previous is not None else newest
        self._last_arrival = arrival
        self._source_id = payload.source_id
        self._needs_anchor = False
        return PacketResult(tuple(intervals), tuple(events), contact=None)

    def tick(self, now: float) -> list[BeatEvent]:
        events = []
        # Drain real measurements even after the phone stops. Silence follows the last
        # buffered beat. No "interpolated" beat is ever manufactured during a dropout.
        while self._pending and self._pending[0][0] <= now + self.t.lead_ms + self.t.horizon_ms:
            target, rr, correction = self._pending.popleft()
            if round(target) < now + self.t.lead_ms + 1:
                self.stats.skipped += 1
                continue
            events.append(self._event(target, rr, OK, now, correction))
        return events
