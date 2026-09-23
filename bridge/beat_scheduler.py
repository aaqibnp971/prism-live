"""The beat scheduler: from late, bundled packets to a steady rhythm scheduled ahead.

The armband reports beats late. A notification arrives about once a second carrying the
intervals of beats that already happened, up to 1200 ms ago. Sounding a beat on arrival
gives a 1 Hz stutter, not a heartbeat.

So this keeps two timelines, both in host monotonic milliseconds (T_engine):

- The reconstructed real timeline. Each interval is chained onto the last, working back from
  the packet's arrival, so beat positions are exact relative to each other and only the
  anchor is uncertain. Intervals that do not fit the recent median are rejected before
  anything else sees them, but the chain still advances through them: time passed even if
  the beat was misread.
- The played timeline. A lattice at the running interval estimate, a fixed buffer behind the
  real beats. Every beat on it is emitted at least ``lead_ms`` ahead, so audio and visuals
  can land on it together. When a packet shows the lattice has drifted from the real beats,
  its phase moves by at most a few percent of an interval per beat. Never a jump.

When real beats stop coming, the lattice keeps going on the last known interval for a grace
period, marking those beats ``interpolated``. After that it stops, rather than invent a
heartbeat.

A missed played slot is skipped, never replayed. It still closes a lattice interval: the next
event's RR describes one interval, not the silence since the last event that was published.

Nothing here knows about wall clocks, threads or sockets. Call ``on_packet`` when a
notification arrives and ``tick`` often (every 20 to 50 ms), and act on what they return.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

from bridge.hrm import parse_hrm

OK = "ok"
INTERPOLATED = "interpolated"
REJECTED = "rejected"


@dataclass(frozen=True)
class Tuning:
    """Every knob, with the reason it exists. Change these when it feels wrong."""

    lead_ms: float = 300.0  # contract: t_play is at least this far ahead when emitted
    horizon_ms: float = 200.0  # emit up to this far beyond the lead, so a late tick is harmless
    buffer_ms: float = 1000.0  # the played rhythm runs this far behind the reconstructed beats
    anchor_lag_ms: float = 500.0  # first guess: a packet's last beat happened half a window ago
    min_lag_ms: float = 20.0  # a beat is never reported sooner than this after it happened
    max_lag_ms: float = 1200.0  # nor later: one notification window plus link latency
    link_timeout_ms: float = 1500.0  # a longer silence on the link means a packet was lost
    snap_gap_max_ms: float = 10_000.0  # after a gap shorter than this, re-anchor onto the lattice
    interpolate_after_ms: float = 2500.0  # no accepted interval for this long: interpolating
    grace_ms: float = 5000.0  # no accepted interval for this long: stop scheduling
    ema_alpha: float = 0.5  # weight of the newest accepted interval in the running estimate
    max_step_fraction: float = 0.04  # phase correction per beat, as a fraction of the interval
    reject_fraction: float = 0.2  # reject an interval this far from the recent median
    window: int = 8  # accepted intervals the median is taken over
    bootstrap: int = 3  # accept anything plausible until this many are in the window
    max_consecutive_rejects: int = 8  # then the window itself is stale: start it over
    plausible_ms: tuple[float, float] = (300.0, 2000.0)  # 200 down to 30 bpm
    min_spacing_ms: float = 250.0  # two played beats are never closer than this
    measured_ppi: bool = False
    packet_settle_ms: float = 2000.0  # wait for a segment's last measurement packet

    @classmethod
    def ppi(cls) -> Tuning:
        """Captured phone cadence, not the legacy one-second HRM lattice."""
        return cls(
            buffer_ms=12 * 1000.0,  # seconds of PPI buffering, unrelated to authority's taper
            anchor_lag_ms=2000.0,
            max_lag_ms=4000.0,
            link_timeout_ms=6200.0,
            interpolate_after_ms=5750.0,
            grace_ms=6200.0,
            measured_ppi=True,
            packet_settle_ms=10_200.0,
        )


@dataclass(frozen=True)
class BeatEvent:
    """One beat for the wire. The last three fields are diagnostics and never leave the host:
    the message contract is frozen."""

    seq: int
    t_play: float  # T_engine ms
    rr_ms: float  # the interval this beat closed
    hr_bpm: float
    quality: str  # ok | interpolated | rejected
    t_emitted: float
    interval_ms: float
    phase_step_ms: float  # the correction applied when scheduling the beat after this one

    def message(self, session: str) -> dict:
        """The ``beat`` message from docs/message-contract-v1.md, and nothing else."""
        return {
            "type": "beat",
            "v": 1,
            "session": session,
            "seq": self.seq,
            "t_play": round(self.t_play),
            "rr_ms": round(self.rr_ms, 1),
            "hr_bpm": round(self.hr_bpm, 1),
            "quality": self.quality,
        }


@dataclass(frozen=True)
class Interval:
    """One reported interval, placed on the reconstructed timeline.

    bridge/hrv.py decides which are clean enough for HRV, and needs the two tags to do it
    (docs/known-limits.md): a lost packet leaves no interval behind, and an interval accepted
    before the median window had anything in it was barely checked.
    """

    t_beat: float  # T_engine ms, reconstructed
    rr_ms: float
    accepted: bool
    arrival: float
    contiguous: bool = True  # the beat right after the previous reported one: no lost packet
    bootstrap: bool = False  # accepted on plausibility alone, before the window held enough


@dataclass(frozen=True)
class PacketResult:
    intervals: tuple[Interval, ...]  # everything the packet carried, accepted or not
    events: tuple[BeatEvent, ...]  # rejected beats, for logging; never rendered
    contact: bool | None = None  # the packet's sensor-contact bit; None when it reports none


@dataclass
class Stats:
    accepted: int = 0
    rejected: int = 0
    skipped: int = 0  # beats that were too late to schedule by the time tick ran
    starts: int = 0
    stops: int = 0
    anchors: int = 0
    link_gaps: int = 0
    window_resets: int = 0


class BeatScheduler:
    def __init__(self, tuning: Tuning | None = None) -> None:
        self.t = tuning or Tuning()
        self.stats = Stats()
        # The reconstructed real timeline.
        self._chain_t: float | None = None  # last reported beat, accepted or not
        self._ref_t: float | None = None  # last accepted beat: the phase reference
        self._interval: float | None = None  # running estimate of the current interval
        self._window: deque[float] = deque(maxlen=self.t.window)
        self._rejects_in_a_row = 0
        self._last_arrival: float | None = None
        self._last_accepted_arrival: float | None = None
        self._needs_anchor = (
            True  # at start, and after any link gap, even one ended by an empty packet
        )
        # The played timeline.
        self._play_next: float | None = None  # t_play of the next beat; None while stopped
        self._last_t_play: float | None = None  # preceding lattice beat, including a skipped one
        self._phase_error = 0.0  # where the lattice should be minus where it is
        self._last_sent_t_play: float | None = None  # survives a stop, unlike _last_t_play
        self._seq = 0

    @property
    def interval_ms(self) -> float | None:
        return self._interval

    @property
    def running(self) -> bool:
        return self._play_next is not None

    # --- packets in ---

    def on_packet(self, now: float, payload: bytes) -> PacketResult:
        t = self.t
        packet = parse_hrm(payload)
        rrs = packet.rr_ms
        link_gap = self._last_arrival is not None and now - self._last_arrival > t.link_timeout_ms
        if link_gap:
            self.stats.link_gaps += 1
            self._needs_anchor = True

        intervals: list[Interval] = []
        events: list[BeatEvent] = []
        if rrs:
            anchored = self._needs_anchor
            if anchored:
                self._anchor(now, rrs)
                self._needs_anchor = False
            for k, rr in enumerate(rrs):
                self._chain_t += rr
                # A beat cannot be reported before it happened. If the chain says so, the chain
                # is running late: pull it back.
                self._chain_t = min(self._chain_t, now - t.min_lag_ms)
                accepted, bootstrap = self._accept(rr)
                intervals.append(
                    Interval(
                        self._chain_t,
                        rr,
                        accepted,
                        now,
                        contiguous=not (anchored and k == 0),
                        bootstrap=bootstrap,
                    )
                )
                if accepted:
                    self.stats.accepted += 1
                    self._interval = (
                        rr
                        if self._interval is None
                        else self._interval + t.ema_alpha * (rr - self._interval)
                    )
                    self._ref_t = self._chain_t
                    self._last_accepted_arrival = now
                else:
                    self.stats.rejected += 1
                    if rr > 0:  # a zero interval is still rejected, but has no rate to report
                        events.append(
                            self._event(self._chain_t + t.buffer_ms, rr, REJECTED, now, 0.0)
                        )
        self._last_arrival = now

        if self._interval is not None and self._ref_t is not None:
            target = self._ref_t + t.buffer_ms
            if self._play_next is None:
                # Nothing is playing: start the lattice on the newest real beat. Beats sent
                # before a stop are still on their way, so never restart on top of one.
                self._play_next = target
                if self._last_sent_t_play is not None:
                    while self._play_next < self._last_sent_t_play + 0.95 * self._interval:
                        self._play_next += self._interval
                self._last_t_play = None
                self._phase_error = 0.0
                self.stats.starts += 1
            elif any(i.accepted for i in intervals):
                self._phase_error = _wrap(target - self._play_next, self._interval)
        return PacketResult(tuple(intervals), tuple(events), packet.contact_detected)

    def _anchor(self, now: float, rrs: Sequence[float]) -> None:
        """Place this packet's last beat somewhere in the notification window it closed, then
        the chain runs back from there."""
        t = self.t
        guess = now - t.anchor_lag_ms
        short_gap = self._last_arrival is not None and now - self._last_arrival < t.snap_gap_max_ms
        if short_gap and self._ref_t is not None and self._interval:
            # Across a short gap the rhythm barely moved, so the lattice extended from the last
            # accepted beat is a better guess than the middle of the window.
            k = round((guess - self._ref_t) / self._interval)
            snapped = self._ref_t + k * self._interval
            if now - t.max_lag_ms <= snapped <= now - t.min_lag_ms:
                guess = snapped
        self._chain_t = guess - sum(rrs)
        self.stats.anchors += 1

    def _accept(self, rr: float) -> tuple[bool, bool]:
        """(accepted, bootstrap): bootstrap if it was accepted on plausibility alone."""
        t = self.t
        lo, hi = t.plausible_ms
        bootstrap = False
        if not lo <= rr <= hi:
            plausible = False
        elif len(self._window) < t.bootstrap:
            plausible = bootstrap = True
        else:
            centre = median(self._window)
            plausible = abs(rr - centre) <= t.reject_fraction * centre
        if plausible:
            self._window.append(rr)
            self._rejects_in_a_row = 0
        else:
            self._rejects_in_a_row += 1
            if self._rejects_in_a_row >= t.max_consecutive_rejects:
                # Nothing has matched the window for a while, so the window is what is wrong.
                self._window.clear()
                self._rejects_in_a_row = 0
                self.stats.window_resets += 1
        return plausible, bootstrap

    # --- beats out ---

    def tick(self, now: float) -> list[BeatEvent]:
        t = self.t
        if self._play_next is None:
            return []
        if now - self._last_accepted_arrival > t.grace_ms:
            # No real beat for too long. Stop rather than invent one.
            self._play_next = None
            self.stats.stops += 1
            return []
        while self._play_next < now + t.lead_ms:
            # Too late to schedule. Leave the beat out and keep the phase.
            # Its time still passed: the next RR closes ONE lattice interval, not the whole
            # silence since the last emitted beat. Otherwise a bridge stall invents a very slow
            # heart rate and can send an out-of-range RR to the audio shim. Do not advance
            # _last_sent_t_play: that separately protects already-published beats on restart.
            self._last_t_play = self._play_next
            self._play_next += self._interval
            self.stats.skipped += 1
        events = []
        while self._play_next <= now + t.lead_ms + t.horizon_ms:
            events.append(self._emit(now))
        return events

    def _emit(self, now: float) -> BeatEvent:
        t = self.t
        t_play = self._play_next
        stale = now - self._last_accepted_arrival > t.interpolate_after_ms
        rr = t_play - self._last_t_play if self._last_t_play is not None else self._interval
        limit = t.max_step_fraction * self._interval
        step = max(-limit, min(limit, self._phase_error))
        self._phase_error -= step
        self._play_next = max(t_play + self._interval + step, t_play + t.min_spacing_ms)
        self._last_t_play = self._last_sent_t_play = t_play
        return self._event(t_play, rr, INTERPOLATED if stale else OK, now, step)

    def _event(self, t_play: float, rr: float, quality: str, now: float, step: float) -> BeatEvent:
        self._seq += 1
        return BeatEvent(self._seq, t_play, rr, 60_000.0 / rr, quality, now, self._interval, step)


def _wrap(x: float, period: float) -> float:
    """x, brought to within half a period of zero: the phase error to the nearest beat."""
    return (x + period / 2) % period - period / 2
