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
  rejection anywhere. In one run (`60:60`, `artefact_burst@35:0.5`, seed 4) that put rmssd_base at
  **57.9 ms against a true 35.6 ms, 62 % high**, and the quality gate passed.

**Handled:** nothing suspect may lie within 3 s or 6 beats of a clean interval, either side,
whichever reaches further. Suspect is a rejected interval, or a misplaced beat found by the ectopic
rule of Lipponen and Tarvainen (2019), described under "Deviation from the published method" below.
A guard of 3 s alone let false intervals through at slow rates, and 4 beats let some through. The
62 % run above now gives 29.3 ms: nothing false gets through, and the guard's data loss leaves it
noisy rather than inflated.

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
1,368 runs and pushed RMSSD up to 37 % high. The same change fixed a scheduler bug: a link gap ended
by a packet with no intervals, which happens below 60 bpm, left the chain stale.

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
| Artefact burst with a lost packet | 0 | 12.8 % | 28.9 % |
| Doubled beat | 0 | 10.5 % | 30.3 % |
| Missed beat | 0 | 10.9 % | 27.4 % |
| Artefact burst, 1 s | 21 (2.6 %) | 11.5 % | 45.3 % |
| Artefact burst, 0.5 s | 55 (6.9 %) | 11.9 % | 28.7 % |
| No fault | 0 | 1.6 % | 11.9 % |

Error is against the true RMSSD of the same 60 s. With no leak, most of it is the cost of losing data:
the guard removes about 12 real intervals around each artefact, and RMSSD from fewer differences is
noisier. The worst 1 s burst run is a leak.

The leaks are false intervals that no rule on intervals can tell from real ones:

- **a burst the scheduler accepts whole**, with no rejection to key the guard on. Examples found
  were at 48 and 52 bpm, where the ±20 % band is widest in milliseconds;
- **a misplaced beat too small for the ectopic rule**, whose differences stay under the threshold
  set by the person's own recent differences. The ones inspected at 50 bpm were misplaced by 50 to
  100 ms.

The ectopic rule fired on clean runs in 3 of 800, each costing about 13 intervals. All of this is
measured on the synthetic armband; see the next section for why that matters.

### Deviation from the published method: the ectopic threshold

For validation, which Nawfil owns. This is the justification for the one constant that departs from
the published method, and what is still needed before it can be relied on.

**What the method says.** Lipponen and Tarvainen (2019), *A robust algorithm for heart rate
variability time series artefact correction using novel beat classification*, J Med Eng Technol
43(3):173-181. Successive RR differences are divided by a threshold of **5.2 quartile deviations**,
QD = (Q3 − Q1) / 2, taken over 91 beats around the beat. A beat is ectopic when its normalised
difference is beyond ±1 and its neighbouring differences, of the opposite sign, cross a boundary
set by c1 = 0.13 and c2 = 0.17. These constants were checked against NeuroKit2's implementation of
the method (`signal_fixpeaks`), not against the paper's text.

**What prism-live does.** The ectopic rule, with c1 and c2 unchanged, and four differences:

1. The threshold is **7 quartile deviations**, not 5.2.
2. The quartiles come from the person's last 32 successive differences before the beat, not 91
   around it. The cleaner cannot wait 45 beats.
3. The rest of their classifier is not used. It finds long, short, missed and extra beats against a
   median-filtered series. The scheduler's 20 % median filter rejects most of those first, and the
   guard keeps the data around a rejection out. How many it misses is not measured.
4. Nothing is corrected or interpolated. A flagged beat is treated as a rejection, and its guard
   removes the data around it.

**Why 7.** A false detection on clean data is not neutral. It removes about 13 real intervals.
Because the rule picks out the largest swings, what it removes are real large differences, so RMSSD
reads low. On the synthetic armband, 90 s runs:

| Seeds | Threshold | Clean runs where it fired | Clean RMSSD, reading ÷ true, 5th percentile | 0.5 s bursts leaking | 1 s bursts leaking |
|---|---|---|---|---|---|
| Tuning, 300 runs | 5.2 | 40 (13 %) | 0.900 | 21 | 4 |
| Tuning, 300 runs | 7 | 14 (4.7 %) | 0.983 | 24 | 4 |
| Held out, 800 runs | 5.2 | 139 (17 %) | 0.929 | 47 | 18 |
| Held out, 800 runs | 7 | 3 (0.4 %) | 0.990 | 55 | 21 |

At 5.2, one clean run in six loses data to a false detection, and 1 run in 20 reads RMSSD 7 % low or
worse. At 7, clean RMSSD reads no more than 1 % low in 95 % of runs. The cost is a few more short bursts
getting through: 55 against 47 of 800 for half-second bursts, 21 against 18 for 1 s bursts.

**History: 19 % against 3 %.** The first misplaced-beat test, committed in prompt 2.1, made the same
choice for a simpler pair rule. It flagged a long and a short interval that straddled the local
median and summed to twice it within 5 %. On the tuning seeds it fired in **19 % of clean runs at
5.2 (56 of 300) and 3 % at 7** with a 10 % floor on the swing (9 of 300). It missed the 62 % run in
limit 1. The true intervals around that beat were 3 % slower than the local median, so the pair
failed the sum test. On 13 September it was replaced by the published rule, which catches that run
at either threshold, and 7 was measured again for the new rule, in the table above.

**What synthetic data cannot show.** The synthetic armband's variability is independent from beat to
beat, so large alternating differences are more common than in real sinus rhythm. In a real heart,
breathing moves successive intervals smoothly. That makes false detections at 5.2 more likely here
than on the recordings 5.2 was chosen from. **Check 7 against real data before relying on it**:
first the recorded real session in `tools/fixtures/` once it exists, and ideally an annotated public
dataset with real ectopic beats. At both thresholds, check the firing rate on clean stretches and
the detection of beats known to be misplaced.

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

