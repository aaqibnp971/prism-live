# Known limits

Limits of what is already built that later work has to design around. Each one is measured, and
each names who has to handle it.

---

## The beat scheduler's accepted intervals, for HRV

**Handled by:** prompt 2.1 (`bridge/hrv.py`, `bridge/baseline.py`). It needs all four.

`BeatScheduler.on_packet` returns every interval a packet carried in `PacketResult.intervals`,
in order, each tagged `accepted` and placed on the reconstructed timeline (`t_beat`). The scheduler
filters for the rhythm it plays. That filter is not good enough for HRV on its own, and nothing
stops HRV reading rejected intervals either.

Measured on 13 September 2026 against `bridge/beat_scheduler.py` with default `Tuning`, driven by
`tools/synthetic_rr.py` with the default profile and seed 1. False intervals were identified by
position in the detected beat sequence, not by value.

### 1. Some artefacts pass the filter

An interval is accepted if it is within 20 % of the median of the last 8 accepted intervals. Garbage
that happens to land inside that band is accepted.

- `artefact_burst@60` produced 9 false intervals. 6 were rejected. **3 were accepted: 672.9, 714.8
  and 775.4 ms**, against a true rate near 800 ms.
- `doubled_beat` and `missed_beat` let nothing false through. No real interval was rejected in any
  run.

**For 2.1:** accepted is necessary, not sufficient. RMSSD needs its own check on successive
differences, so an accepted artefact next to a real beat cannot inflate it.

### 2. Accepted intervals are not always adjacent

A rejected interval leaves a hole. The accepted intervals on either side of it are not consecutive
beats, so their difference is not a successive difference.

- Differencing the accepted list alone spans a rejection once for a doubled beat, once for a missed
  beat, and 3 times in the artefact burst.

**For 2.1:** take successive differences only between intervals that are consecutive in the full
tagged sequence, with nothing rejected between them. `PacketResult.intervals` keeps that order for
exactly this.

### 3. Packet loss leaves no marker

A dropped notification or a disconnect removes beats outright. The device does not resend, and no
interval is tagged to say beats are missing.

- The only sign is in the timeline: across a gap, `t_beat` jumps by more than the next interval's
  `rr_ms`. After a link gap the chain is re-anchored, so `t_beat` there is an estimate.
- `scheduler.stats.link_gaps` counts link gaps, but it is a counter, not a mark on an interval.

**For 2.1:** check continuity (`t_beat[i] − t_beat[i−1] ≈ rr_ms[i]`) and never difference across a
break. Count the lost time against baseline quality: the gate is at least 35 of 45 s of clean data
and at least 30 accepted intervals (experience script §2, BASELINE).

### 4. The first three intervals are barely filtered

Until the median window holds 3 intervals, anything plausible (300 to 2000 ms) is accepted. The
same happens again after a window reset: 8 rejections in a row clear the window, on the assumption
that the window itself has gone stale.

- A reset is visible only as `scheduler.stats.window_resets` going up. The intervals accepted just
  after it are not tagged.

**For 2.1:** leave the first 3 accepted intervals after start, and after every window reset, out of
RMSSD and out of the baseline, or put them through the same successive-difference check before
trusting them.
