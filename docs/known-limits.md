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
  **57.9 ms against a true 35.6 ms, 62.5 % high**, and the quality gate passed.

**Handled:** nothing suspect may lie within 3 s or 6 beats of a clean interval, either side,
whichever reaches further. Suspect is a rejected interval, or a misplaced beat found by the ectopic
rule of Lipponen and Tarvainen (2019), described under "Deviation from the published method" below.
A guard of 3 s alone let false intervals through at slow rates, and 4 beats let some through. The
run above now gives 29.3 ms: nothing false gets through, and the guard's data loss leaves it
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
| Artefact burst with a lost packet | 0 | 13.2 % | 28.9 % |
| Doubled beat | 0 | 10.5 % | 26.1 % |
| Missed beat | 0 | 10.9 % | 27.4 % |
| Artefact burst, 1 s | 22 (2.8 %) | 11.5 % | 45.3 % |
| Artefact burst, 0.5 s | 58 (7.3 %) | 11.9 % | 28.7 % |
| No fault | 0 | 1.6 % | 11.9 % |

Error is against the true RMSSD of the same 60 s. With no leak, most of it is the cost of losing data:
the guard removes about 12 real intervals around each artefact, and RMSSD from fewer differences is
noisier. The worst 1 s burst run is a leak.

The leaks are false intervals that no rule on intervals can tell from real ones:

- **a burst the scheduler accepts whole**, with no rejection to key the guard on. Examples found
  were at 48 and 52 bpm, where the ±20 % band is widest in milliseconds;
- **a misplaced beat too small for the ectopic rule**, whose differences stay under the threshold
  set by the person's own recent differences.

The ectopic rule fired on clean runs in 6 of 800, each costing about 13 intervals. All of this is
measured on the synthetic armband; see the next section for why that matters.

### Deviation from the published method: the ectopic threshold

> **12 is provisional, and so is every number in this document measured with it, until it has
> been retuned on a real recorded session.** It was tuned against the synthetic armband, whose variability is random from beat to
> beat. Real heart rate variability is correlated with breathing, so successive intervals change
> smoothly and large alternating differences are rare. That same difference very likely explains
> why the published 5.2, chosen on real recordings, fired on 680 of 800 clean synthetic runs. The
> retune is on the armband-day list, docs/all-prompts.md prompt 1.0.

For validation, which Nawfil owns. This is the justification for the one constant that departs from
the published method, and what is still needed before it can be relied on.

**What the method says.** Lipponen and Tarvainen (2019), *A robust algorithm for heart rate
variability time series artefact correction using novel beat classification*, J Med Eng Technol
43(3):173-181. Successive RR differences (dRR) are divided by a threshold of **5.2 quartile
deviations of their sizes**: QD = (Q3 − Q1) / 2 of |dRR|, taken over 91 beats around the beat. A
beat is ectopic when its normalised difference is beyond ±1 and its neighbouring differences, of
the opposite sign, cross a boundary set by c1 = 0.13 and c2 = 0.17. These details were checked
against NeuroKit2's implementation of the method (`signal_fixpeaks`, whose threshold takes the
quartiles of `np.abs(drrs)`), not against the paper's text.

**What prism-live does.** The ectopic rule on the same |dRR| scale, with c1 and c2 unchanged, and
four differences:

1. The threshold is **12 quartile deviations**, not 5.2.
2. The quartiles come from the sizes of the person's last 32 successive differences before the
   beat, not 91 around it. The cleaner cannot wait 45 beats.
3. The rest of their classifier is not used. It finds long, short, missed and extra beats against a
   median-filtered series. The scheduler's 20 % median filter rejects most of those first, and the
   guard keeps the data around a rejection out. How many it misses is not measured.
4. Nothing is corrected or interpolated. A flagged beat is treated as a rejection, and its guard
   removes the data around it.

**Why 12.** A false detection on clean data is not neutral. It removes about 13 real intervals.
Because the rule picks out the largest swings, what it removes are real large differences, so RMSSD
reads low. On the synthetic armband, 90 s runs:

| Seeds | Threshold | Clean runs where it fired | Clean RMSSD, reading ÷ true, 5th percentile | 0.5 s bursts leaking | 1 s bursts leaking |
|---|---|---|---|---|---|
| Tuning, 300 runs | 5.2 | 256 (85 %) | 0.766 | 8 | 1 |
| Tuning, 300 runs | 12 | 5 (1.7 %) | 0.990 | 26 | 5 |
| Held out, 800 runs | 5.2 | 680 (85 %) | 0.752 | 23 | 12 |
| Held out, 800 runs | 12 | 6 (0.8 %) | 0.990 | 58 | 22 |

At 5.2, five clean runs in six lose data to a false detection, and 1 run in 20 reads RMSSD a quarter
low or worse: it would make most people look less variable than they are. At 12, clean RMSSD reads
no more than 1 % low in 95 % of runs. The cost is more short bursts getting through: 58 against 23 of
800 for half-second bursts, 22 against 12 for 1 s bursts.

12 was chosen on the tuning seeds alone, as the lowest multiplier at which clean-run firing reached
its floor: 10 fired in 14 of 300 runs, 11 in 7, and 12, 13 and 14 in 5. The held-out rows were run
afterwards.

**History.** This is the third version, all on 13 September.

- The first misplaced-beat test, committed in prompt 2.1, used a simpler pair rule: a long and a
  short interval straddling the local median and summing to twice it, to within 5 % of the median,
  with a swing threshold on signed differences. On the tuning seeds it fired in **19 % of clean runs
  at 5.2 quartile deviations (56 of 300, with a 5 % floor on the swing) and 3 % at 7 (9 of 300, with
  a 10 % floor)**. At the same 10 % floor, 5.2 fired in 8 % (24 of 300). It missed the run in limit
  1: that pair summed to 63.5 ms over twice the median, 6.4 % of the median, against a tolerance of
  5 %.
- The second replaced it with the published ectopic rule at 7, but took the quartiles of signed
  differences, which are about 1.7 times those of their sizes. 7 there was about 12 on the
  published scale, and the "5.2" measured with it was not the published 5.2. It also used 31
  differences where it meant 32: two held-out bursts leaked only because of that, and one other
  leaks only with 32.
- This version takes the quartiles of |dRR| from 32 differences, as described above. The run in
  limit 1 gives 29.3 ms, and nothing false gets through.

**What synthetic data cannot show.** The synthetic armband's variability is independent from beat to
beat, so large alternating differences are far more common than in real sinus rhythm. In a real
heart, breathing moves successive intervals smoothly. That is very likely why the published 5.2,
chosen on real recordings, fires on most synthetic runs. **Check 12 against real data before
relying on it**: first the recorded real session in `tools/fixtures/` once it exists, and ideally an
annotated public dataset with real ectopic beats. At 5.2 and at 12, check the firing rate on clean
stretches and the detection of beats known to be misplaced. On real data 12 may prove too
permissive, and something nearer 5.2 right.

### What the baseline takes from where

`hr_base` and the slope behind `baseline_quality` use every accepted, non-bootstrap interval: the set
the quality gate trusts, so `hr_base` exists whenever the gate passes, and the slope spans the whole
window. The slope is a Theil-Sen fit, so a stray false interval cannot drag an end of it. `rmssd_base`
uses only HRV-clean differences from the last 30 s, and is None when there are fewer than 10.

---

## HRV and baseline timing, for the session state machine

**Handled by:** `bridge/session.py`, prompt 2.4, as rewritten on 13 September (docs/all-prompts.md).
Three consequences of the section above.

### 1. Intervals are classified 3 to 7.5 s late

Classifying an interval needs a guard's length of what follows it: 3 s or 6 beats, whichever is
longer. That is 3 s at fast rates and 7.5 s at 48 bpm, plus the armband's reporting delay of up to
1.2 s. A lost packet is the exception: when the gap shows, everything still pending is classified
at once. `RollingHrv.reading(now, cleaner.horizon_ms)` ends its 60 s window where classification
has reached, and reports how far that is behind now as `lag_ms`.

Heart rate alone does not have to wait. Whether the scheduler accepted an interval, and whether it
was bootstrap, is known the moment the interval arrives.

### 2. The baseline result arrives 2.0 to 10.6 s after the baseline window ends

`BaselineCapture.ready()` is true once the first interval past the end of the 45 s has been
classified. That interval cannot exist at 45 s, so neither can `hr_base`.

With no packet lost around the end of the window: held-out sweep, 5,600 runs across every fault
case, 3.4 to 10.2 s after the window ended, median 6.1 s. On clean runs it depends on the rate:

| Heart rate | Fastest | Median | Slowest |
|---|---|---|---|
| 48 bpm | 7.5 s | 8.6 s | 9.5 s |
| 68 bpm | 5.9 s | 6.6 s | 7.5 s |
| 95 bpm | 4.0 s | 4.7 s | 5.5 s |
| 130 bpm | 3.5 s | 4.3 s | 5.0 s |

A packet lost in the first 2 s after the window changes both ends. Over 2,000 runs (8 rates, 50
seeds, a dropped packet at 45.0 to 47.0 s), the result came 2.0 to 10.6 s after the window, and in
609 runs sooner than 3.4 s.

Prompts 2.4 and 2.7 hold baseline to 56 s, where load starts on a pulse boundary, and enter load
degraded if `hr_base` has not come by then. Every measured wait fits inside those 11 s.

`hr_base`, the slope and the quality gate all use accepted, non-bootstrap intervals, which need no
classification. Only `rmssd_base`, and `hr_sd_bpm`, the spread `bridge/psv.py` uses for its
arousal unit, do. `BaselineCapture` waits for all of it together. Splitting
the two would give `hr_base` about one interval plus the reporting delay after the window ends,
shortening the hold. That is not built and not measured.

### 3. rmssd_base can be empty while the quality gate passes

The gate counts accepted intervals. `rmssd_base` needs at least 10 clean successive differences in
the last 30 s, and each artefact costs about 12 intervals. The test suite has a run where the gate
passes and `rmssd_base` is None. In the held-out sweep, one fault 30 s into baseline left it None in
this many of 800 runs:

| Fault at 30 s | Runs with rmssd_base None |
|---|---|
| Missed beat | 85 |
| Artefact burst, 2 s | 74 |
| Artefact burst, 1 s | 46 |
| Artefact burst, 0.5 s | 41 |
| Doubled beat | 8 |
| None | 0 |

**When it is there, a 30 s rmssd_base can still be far off,** mostly because the guard leaves few
differences behind. Reading ÷ true RMSSD of the same 30 s, held-out seeds, the cases above and a 5 s
burst at 20 s, pooled:

| Clean differences behind it | Runs | More than 20 % high | More than 20 % low |
|---|---|---|---|
| 10 to 14 | 909 | 9.9 % | 16.9 % |
| 15 to 19 | 1,008 | 5.7 % | 7.8 % |
| 20 to 24 | 445 | 4.5 % | 4.0 % |
| 25 to 29 | 572 | 3.5 % | 2.6 % |
| 30 to 39 | 1,076 | 0.9 % | 1.8 % |
| 40 or more | 1,225 | 0.2 % | 0.4 % |

This matters wherever `rmssd_base` is compared against, above all load's secondary criterion, RMSSD
at or below 0.80 × baseline. A baseline 20 % high makes a person look activated when they were not.
Prompt 2.4 treats `rmssd_base` as optional, and skips an RMSSD criterion when either of its 30 s
windows has fewer than 20 clean differences.

---

## The PSV, for Week B and for validation

**Handled by:** the Week B listening pass (prompt 2.5), the recorded real session, and whoever owns
the VR handoff. `bridge/psv.py` was built and tuned on the synthetic armband only.

- **Every constant is provisional.** At the 4 bpm unit, the arousal scale reads +6 bpm as 0.67, +15
  as 0.85 and +37 as 0.99. Readiness confidence stops at 0.6. None of it has met a real heart.
- **The RMSSD term's rate correction is exact only for the generator.** `ln RMSSD + 2 ln HR` removes
  the part of RMSSD that follows from heart rate alone, which is what the synthetic armband's
  variability does by construction. The term carries 20 % of arousal until a real recording shows
  how RMSSD and heart rate move together.
- **The person's own heart rate spread stays under its 4 bpm floor on settled synthetic
  baselines.** They spread about 1 to 3 bpm, so the personal part of the arousal unit is exercised
  only by tests that inject a spread. Baselines falling steeply spread wider, but score quality 0.
- **The baseline world is driven by three confidences, rescaled.** Valence confidence is 0.0 by
  design, so a mean over all four could never pass 0.75, and reached only 0.295 in a baseline. The
  driver is now the mean of arousal, cognitive_load and readiness, divided by 0.393
  (`BASELINE_CONFIDENCE_MAX`): arousal reaches 1, cognitive_load has no task events in baseline, and
  readiness has no recovery part yet, so the three reach at most 0.393. Measured on 400 settled
  synthetic baselines at 8 rates: at 45 s the median is 0.393 and 95 % reach at least 0.340. Slow
  heart rates fall short: at 48 bpm the median is 0.368, 0.94 of the visual travel. The climb is
  slow for the first 20 s and fastest from 25 to 40 s (the table after this list). A 5 s artefact
  burst 30 s into the baseline leaves a median of 0.227 at 45 s, and a baseline still settling
  gently 0.304. How long the armband was on beforehand makes no difference. If readiness or its
  confidence split changes in Week B, so does 0.393.
- **A baseline taken while heart rate is still falling steeply gives no confidence once its result
  is in, for the rest of the session.** At quality 0, everything measured against it is untrusted,
  so nothing the body does acts on the engine for that visitor. During the capture the bars still
  climb to about 0.38 and fall back as the slope shows. This is left to the booth, not the code
  (docs/project-plan.md §8): the baseline starts 70 s after the greet and ends 115 s after it. A
  baseline still falling steeply after 115 s of settling is an honest low-confidence reading of that
  person, not an artefact.
- **A degraded baseline sends `hr_base` null for the rest of the session**, with
  `signal.baseline_quality` 0.0. Contract v1.3 records this.
- **The contact bit is unverified.** Whether the Verity Sense reports contact at all, and whether it
  keeps sending RR intervals without it, are open. A sensor that does not report contact is read as
  in contact.

The baseline confidence curve, the median of the 400 settled synthetic baselines:

| Baseline t | 0 s | 10 s | 20 s | 25 s | 30 s | 35 s | 40 s | 45 s |
|---|---|---|---|---|---|---|---|---|
| Mean of the three confidences | 0.000 | 0.030 | 0.100 | 0.137 | 0.219 | 0.298 | 0.374 | 0.393 |
| Baseline confidence, after ÷ 0.393 | 0.00 | 0.08 | 0.25 | 0.35 | 0.56 | 0.76 | 0.95 | 1.00 |
