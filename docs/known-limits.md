# Known limits

Limits of what is already built that later work has to design around. Each one is measured, and
each names who has to handle it.

---

## The beat scheduler's accepted intervals, for HRV

**Handled by:** `bridge/hrv.py` (`IntervalCleaner`), since prompt 2.1. All four limits are handled.
What still gets through is at the end of this section, with measured rates.

`BeatScheduler.on_packet` returns every interval a packet carried in `PacketResult.intervals`,
in order, each tagged `accepted` and placed on the reconstructed timeline (`t_beat`). The scheduler
filters for the rhythm it plays. That filter is not good enough for HRV on its own. Nothing but the
cleaner should read these intervals for HRV.

Measured on 13 September 2026 with default `Tuning`, driven by `tools/synthetic_rr.py`. False
intervals were identified by position in the delivered beat sequence, not by value.

### 1. Some artefacts pass the scheduler's filter

An interval is accepted if it is within 20 % of the median of the last 8 accepted intervals. Garbage
that happens to land inside that band is accepted.

- Default profile, seed 1, `artefact_burst@60`: 9 false intervals, 6 rejected, **3 accepted: 672.9,
  714.8 and 775.4 ms**, against a true rate near 800 ms.
- At 100 bpm a burst can open with accepted false intervals two beats before its first rejection.
  At 55 bpm its last false interval can land more than 3 s after its last rejection.
- A single beat detected late leaves a long interval and a short one, both inside the band, with no
  rejection anywhere.

**Handled:** nothing suspect may lie within 3 s or 6 beats of a clean interval, either side,
whichever reaches further. Suspect is a rejected interval, or the second interval of a misplaced-beat
pair (one each side of the local median, summing to twice it within 5 %, swinging by more than 7
quartile deviations of recent successive differences and 10 % of the median). A guard of 3 s alone
let false intervals through at slow rates, and 4 beats let some through.

### 2. Accepted intervals are not always adjacent

A rejected interval leaves a hole. The accepted intervals on either side of it are not consecutive
beats, so their difference is not a successive difference.

- Differencing the accepted list alone spans a rejection once for a doubled beat, once for a missed
  beat, and 3 times in the artefact burst.

**Handled:** a difference is taken only between an interval and the one reported just before it,
both clean, within 20 % of the earlier interval. The guard keeps both sides of any rejection
unclean.

### 3. Packet loss

A dropped notification or a disconnect removes beats outright, and the device does not resend. A
packet that arrives more than 1.5 s late is treated the same way by the scheduler. Nothing marked
either, and the timeline alone cannot: after a link gap the chain is re-anchored, and the guess can
be a whole interval out.

**Handled:** the scheduler now tags every interval `contiguous`: false for the first interval after
a re-anchor. The cleaner never takes a difference across one, and **guards a gap as if a rejection
sat in it**, on both sides, because the packet that went missing may have held a burst's
rejections. Without that, a burst with one lost packet let false intervals through in over 100 of
1,368 runs and pushed RMSSD up to 37 % high. The same change fixed a scheduler bug: a link gap ended by a
packet with no intervals, which happens below 60 bpm, left the chain stale.

### 4. The first three intervals are barely filtered

Until the median window holds 3 intervals, anything plausible (300 to 2000 ms) is accepted. The
same happens after a window reset: 8 rejections in a row clear the window.

**Handled:** the scheduler now tags those intervals `bootstrap`. They are never clean, and the
baseline does not count them as accepted.

### What still gets through, measured

Held-out sweep: 100 seeds never used for tuning, 8 rates from 48 to 130 bpm, 800 runs per case.

| Case | Runs with any false data in HRV | RMSSD over 60 s, error in 95 % of runs | Worst run |
|---|---|---|---|
| Artefact burst, 5 s | 0 | 12.7 % | 28.9 % |
| Artefact burst with a lost packet | 0 | 13.2 % | 28.9 % |
| Doubled beat | 0 | 10.5 % | 26.1 % |
| Missed beat | 0 | 11.0 % | 27.4 % |
| Artefact burst, 1 s | 23 (2.9 %) | 11.5 % | 45.3 % |
| Artefact burst, 0.5 s | 61 (7.6 %) | 12.0 % | 30.1 % |
| No fault | 0 | 1.6 % | 13.3 % |

Error is against the true RMSSD of the same 60 s. With no leak, most of it is the cost of losing data:
the guard removes about 12 real intervals around each artefact, and RMSSD from fewer differences is
noisier. The worst 1 s burst run is a leak.

The leaks are false intervals that no rule on intervals can tell from real ones:

- **a burst the scheduler accepts whole**, with no rejection to key the guard on. Examples found
  were at 48 and 52 bpm, where the ±20 % band is widest in milliseconds;
- **a misplaced beat whose swing sits inside the person's normal variability**, below the 7 quartile
  deviation threshold.

The misplaced-beat test fired on clean runs in 5 of 800, all at 80 bpm or slower, each costing about
13 intervals. On the seeds used to tune it, 7 quartile deviations fired in 3 % of clean runs and
Lipponen and Tarvainen's 5.2 in 19 %. All of this
is measured on the synthetic armband, whose variability is independent beat to beat. Real sinus
arrhythmia is smoother; check the firing rate against the recorded real session when it exists.

### What the cleaner costs in time

Classifying an interval needs a guard's length of what follows it: 3 s or 6 beats, whichever is
longer, so 3 to 7.5 s at seated heart rates, plus the armband's reporting delay of up to 1.2 s.
`RollingHrv.reading(now, cleaner.horizon_ms)` ends its 60 s window where classification has reached
and reports the difference from now as `lag_ms`. The baseline result is ready once the first
interval past the end of the 45 s is classified: in the held-out sweep, 3.4 to 10.2 s after the
baseline ended, with a median of 8.6 s at 48 bpm and 4.3 s at 130 bpm. Whatever consumes it during
the first seconds of load must wait for it.

### What the baseline takes from where

`hr_base` and the slope behind `baseline_quality` use every accepted, non-bootstrap interval: the set
the quality gate trusts, so `hr_base` exists whenever the gate passes, and the slope spans the whole
window. The slope is a Theil-Sen fit, so a stray false interval cannot drag an end of it. `rmssd_base`
uses only HRV-clean differences from the last 30 s, and is None when there are fewer than 10: after
a few artefacts in the tail the gate can pass with no RMSSD baseline.
