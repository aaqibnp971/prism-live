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

## The session state machine's own numbers

**Handled by:** `bridge/session.py`, prompt 2.4. No document set these; each is provisional until
real sessions say otherwise.

- **HR_load needs 22.5 of its 30 s covered** by accepted beats, the same three quarters a regulate
  window needs (15 of 20 s). With less, there is no threshold, and regulate runs a plain 75 s, as
  after a degraded baseline. An error in HR_load moves the threshold one for one on its 5 bpm floor.
- **HR_load is taken 2 s into regulate.** At load's last instant, the beats of its last second or
  so have not arrived.
- **Each regulate window is judged once, at the first tick past its end**, on a 500 ms grid. The
  beats of its last second have mostly not arrived by then, so a clean window covers about 18 of
  its 20 s when judged. A beat arriving later never changes a verdict.
- **An extension ends "unjudged" 6.2 s after the last accepted beat**: the scheduler's 5 s grace
  plus its 1.2 s reporting lag. A signal lost mid-regulate and back by 75 s still extends.
- **Start is refused when no accepted beat has come in the last 6.2 s.**

**Boundaries do not depend on when the loop ticks.** Load starts at the arrival of the packet that
completed the baseline, and the end of the hold is judged on what had been classified by then,
whenever the tick that notices comes. Every fixed boundary is its deadline. A stalled loop catches
them all up, in order. Two verdicts do still depend on the tick:

- A window judged after a stall has all of its last second's beats, where a punctual tick had
  most of them. Near the threshold that can move the regulated moment by a grid step or two.
- A signal lost and back again within one stall is never seen as lost.

Heart rate windows reach back two minutes from the newest beat, so a stall of up to about a minute
and a half keeps HR_load and every regulate window.

**A hold that ends with no result judges the quality gate on what has been classified,** and the
last guard's length before the armband went quiet never is. So an armband lost for good just before
the window closes fails the gate, the re-seat path, where the same data would have passed with the
tail classified. Synthetic armband, no faults, seeds 1 to 10, lost for good this long before the
45 s window closed, what the session did at the end of the 12 s hold:

| Heart rate | Fails the gate | Enters load degraded | Either |
|---|---|---|---|
| 48 bpm | 2 s or more before | at the close | 1 s before: 3 of 10 fail |
| 68 bpm | 5 s or more before | 3 s or less | 4 s before: 7 of 10 fail |
| 95 bpm | 7 s or more before | 5 s or less | 6 s before: 9 of 10 fail |

An armband that comes back after the hold still gets that verdict. An armband lost for good gets no
useful session either way, and the re-seat is the booth's quicker path. **Accepted, 14 September
2026.**

**A failed gate ends baseline when the result arrives**, from 45 s on, also under 2.7's
`hold_to_end`, where only load waits for the end of the hold (decided 14 September).
`Session.schedule` gives baseline's earliest end as the 45 s close.

---

## The audio shim and the PSV feed (prompt 2.5)

**Handled by:** `native/` (the shim, DLL committed at `native/bin/`), `bridge/engine.py`,
`bridge/engine_feed.py`, `bridge/poses.py`. Measured 14 September, offline, against the committed
engine and shim DLLs and a generated test scene. The real stems do not exist yet.

### The output stage

- **The chain is 1,639 frames (34.1 ms) late.** That is the limiter's lookahead, its guard and its
  detector's delay. A beat enters the mix 1,639 frames early, so it still leaves at `t_play`. The
  engine's material comes out 34 ms after its PSV, which at 2 s updates does not matter. Stop
  waits for the fade to leave the limiter: the ramp, plus the latency, plus two blocks, with a
  deadline 500 ms after the ramp.
- **True peak.** The limiter aims at −1.5 dBTP so that −1.0 dBTP holds. The worst measured case
  is +6 dBFS white noise, which comes out at −1.28 dBTP by an FFT-based reference meter: 0.27 dB of
  margin. The four-minute render with the engine at its loudest PSV and the heartbeat at −9 dBFS
  peaks at −3.05 dBTP, and the limiter never engages.
- **One class of signal is not bounded:** energy at exactly fs/2 that starts or stops abruptly.
  Its true peak grows with the meter's length. A naive 8 kHz square wave comes out at −0.67 dBTP.
  The engine, behind its own low-pass of at most 12 kHz, cannot produce this, and neither can a
  44 Hz heartbeat.
- **Set the engine trim from peaks measured after the high-pass (Week B).** The high-pass's phase
  shift around the sub re-aligns components, which raises the engine's sample peak from its own
  −3 dBFS limit to −1.97 dBFS. At trim 0 dB and the loudest PSV, the limiter takes up to 2.4 dB
  almost continuously, which will likely be heard. At the default trim of −6 dB it never engages.
- **The high-pass**, the 10th-order Chebyshev II decided on 14 September:
  - At least 30.001 dB down at and below 62 Hz.
  - Within 1 dB from 69.35 Hz up. At 69.3 Hz it is 1.03 dB down, because order 10 cannot quite
    reach it.
  - The sub's 73.4 Hz is down 0.11 dB.
  - Measured 36 to 62 Hz band attenuation: 32.9 dB on white noise, 32.4 dB on a sweep, 41.2 dB on
    the engine at its loudest PSV.
- **A render block with a non-finite sample** is zeroed and counted in `render_errors`, so one NaN
  from the engine cannot turn the output to NaN for good.

### The device

- **The default output device, and only that one.** The stream never follows a default-device
  change, so plug the headphones in before start. If the endpoint goes away, the stream stops by
  itself, `frames_rendered` stops advancing, and `device_unrequested_stops` counts it. Recover with
  stop, a new time origin and start, which opens the default device afresh and checks it for 48 kHz
  again. The refusal of a device that is not at 48 kHz has not run: this machine's device is at
  48 kHz. The lost-device path needs a real unplug to be checked.
- **The heartbeat's device-anchor correction is implemented but not hardware-verified.** The
  non-audio timing thread polls the WASAPI stream position reported by `IAudioClock`; the callback
  reads only a lock-free snapshot. The first usable result anchors T_engine and later observations
  move the anchor only by a slew of at most 1 ms per second. Offline tests prove the map does not
  step, obeys that rate, discards prior-run beats on a re-anchor, and never reports an onset from a
  failed current clock read. They cannot prove where a real DAC presents the sample. Every
  scheduled-versus-device estimate is retained for the control thread to log. Confirm the
  relationship on the booth laptop before treating it as a physical-onset measurement.

### The armband-day list

These all need hardware and cannot be verified in software:

1. Confirm the fade to silence makes no audible click on real headphones, wired, across all four interruption scenarios.
2. Pull the audio cable mid-session and confirm recovery. Also switch the default output device while running.
3. Measure the real DAC anchor, which prompt 2.6 is designed to refine.
4. Verify prompt 2.6's anchor correction against the real device's reported position over time.
   Its relationship to physical DAC output is unverified until this hardware check.

### The PSV feed

All provisional until Week B listening.

- **The phase-aware gate plan is implemented in prompt 2.7.** A gate opens at density ≥ threshold
  + 0.01 and closes at ≤ threshold − 0.01. `bridge/phase.py` reads the shim's rendered-frame
  counter, gives every crossing a one-block-early send frame, and identifies the actual 1.5 s
  ramp. Hysteresis holds only across that ramp; a reversal before its boundary cancels cleanly.
  Air closes on the last air boundary before pulse's first regulate boundary, so air's fade ends
  first for every relative phase. The cutoff can still sit at a hysteresis edge and then move by
  about 100 to 300 Hz when a gate changes; the engine's 0.6 s smoothing makes that a glide.
- **Idle and reset send the baseline pose under both PSV sources.** An authority-zero body value
  remains neutral in the state message, but it is not sent to the engine between visitors, so it
  cannot open pulse there. Baseline itself is held below both gates until the aligned pre-arm.
- **The baseline pose's density is 0.3374,** only 0.0026 below the pulse gate's closing edge. A
  Week B tweak of about +0.003 arousal would put it where the hysteresis moves it without saying.
- **In the pose source, the body moves arousal inside a segment's range** by
  `clamp((arousal − 0.5) / 0.35, 0, 1) × arousal confidence`. The segment ceiling is not applied,
  because the pose is the segment's design. In the synthetic fixture, regulate sits at 1,377 to
  1,496 Hz, because the synthetic heart stays aroused. Resolve's first PSV then drops the cutoff to
  620 Hz.
- **The body pre-blend uses the state message's `psv`,** rounded to 3 decimals, so the engine and
  the screen move on the same numbers.
- **The session gain.**
  - Idle and reset: 0 over 3 s.
  - Baseline: fades in over 2 s. No document sets this.
  - Resolve: the ending starts on the first state message at or after T−22 s, so up to 2 s late,
    and the ramp is shortened so it still lands at T−10 s.
  - During stop, `SessionGain` holds every change until `resume`.

---

## The live bridge loop (prompt 2.9)

**Handled by:** `bridge/live.py` and `bridge/server.py`. Measured 18 September 2026 on the booth
laptop, on the real performance clock, with the synthetic armband, the committed engine and shim,
the placeholder scene, the real default audio device and the WebSocket server listening. The run
went from aligned start firing through the end of reset: 271.025 s, 8,666 tick intervals and 362
published non-rejected beats.

| Measurement | Median | p95 | Worst relevant value | Requirement |
|---|---:|---:|---:|---:|
| Actual tick interval | **31.244 ms** | **32.139 ms** | **67.520 ms max** | 20 to 100 ms |
| Beat lead at publish | **483.521 ms** | **497.833 ms** | **467.746 ms min** | at least 300 ms |

The shortest tick interval was 20.013 ms. No beat was refused for short lead, no phase-alignment
fault occurred, and neither planned gate crossing missed its boundary. The start fired at exactly
the armed frame.

The measurement changed the production cadence. A first complete run at a nominal 50 ms produced
62.542 ms median, 63.989 ms p95 and one **109.814 ms maximum**, outside the target, although beat
lead remained safe at a 425.344 ms minimum. The live loop now requests the 20 ms floor and checks
spacing on `T_engine` itself; using asyncio's coarser Windows clock alone had also allowed one early
15.674 ms interval. The table is the full rerun after both changes. These figures cover this laptop
under synthetic input, not BLE contention; repeat the same one-session diagnostic after prompt 2.8.

---

## The browser load task's viewing geometry (prompt 3.1)

**Handled by:** `web/task/task.js`, with the active assumption shown in the page's debug corner.
The default is a 27-inch 16:9 monitor, whose physical width is **59.773 cm**, viewed from **60 cm**.
That makes the full horizontal field of view:

`FOV = 2 × atan(screen_width / (2 × viewing_distance)) = 52.956°`.

The target is advanced in angular space at the authored 6 to 19 degrees per second. It is then
projected onto the monitor plane, rather than being moved at one approximate fixed pixel speed:

`x_px = viewport_width_px / 2 + tan(angle) × viewing_distance × viewport_width_px / screen_width`.

At the centre of a 1,920-CSS-pixel full-screen viewport, the default converts 6 degrees per second
to **201.827 px/s** and 19 degrees per second to **639.118 px/s**. The projected pixel speed rises
toward the edges, as it must for the viewed angular speed to remain constant. The debug corner shows
the instantaneous value. Unity should advance the target through the same angular coordinate and
project it through its camera, not copy either centre pixel number.

The URL parameters are `distance_cm` and `screen_width_cm`. The conversion assumes the browser is
full-screen and its viewport width spans the configured physical screen width. It cannot verify the
visitor's actual head distance or whether they move off-centre; measure the booth geometry and enter
it before judging difficulty. `?standalone=1` removes the WebSocket and bridge but deliberately uses
the same task model, ramp, deadlines and event builder as live mode.

The dwell and speed numbers remain provisional. They were authored for eye control, which Quest 3S
does not provide; pointer control and head-reticle control are retuned against real runs in Week E.

---

## Cognitive load from task events (prompt 3.2)

**Handled by:** `bridge/psv.py`, from events stamped at WebSocket arrival on `T_engine`. The mapping
is deliberately based on the event grammar rather than on mouse-specific dwell durations.

The chosen rule is:

- Each `split` opens one opportunity. A `lock` proves active pursuit. An `abandon` on either the
  moving half or a distractor also proves active pursuit, but with strain: the reticle reached a
  valid half and left before its dwell completed.
- A `miss` before 85 % of that opportunity's advertised split interval is a wrong-target dwell, so
  it proves active pursuit and high strain. A later miss after an `abandon` also means the person
  was still chasing. A deadline miss with no abandon is the task's automatic timeout and is treated
  as disengagement, not overload.
- Participant engagement stays fully fresh for half an advertised interval after the last `lock`,
  `abandon` or wrong-target miss, then falls linearly to zero by one and a half intervals. An
  automatic deadline miss never refreshes that timestamp, even when an earlier abandon makes the
  round an engaged miss. This is the event-gap signal that separates an early attempt followed by
  silence from someone still chasing near the deadline.
- A separate gap in the whole event stream is a task-screen failure, not evidence about the person.
  It holds confidence through 1.5 advertised intervals and fades it to zero by four intervals.
- Over the last 30 s, disengagement maps to 0.20. Engaged load is
  `0.25 + 0.50 × difficulty + 0.25 × strain`; strain is 1.0 for an engaged miss, 0.60 for a lock
  after an abandon, and 0.50 while an abandoned opportunity remains unresolved. Engagement blends
  between the engaged value and 0.20.
- Confidence grows from 0 to 1 across eight distinct split opportunities, rather than raw event or
  abandon count, and is multiplied by whole-stream freshness. Task load supplies 80 % of the final
  cognitive-load value and heart rate supplies 20 %. With no task event, the value is neutral and
  confidence is **exactly 0.0** regardless of heart rate; heart rate alone is arousal, not load.

There is an unavoidable ambiguity: a person who is overloaded and becomes completely motionless
looks the same as a person who has disengaged. The implementation chooses the conservative answer,
low load, because the stream contains no evidence of continued pursuit. The diagnostic components
(`task_engagement`, `task_strain`, freshness, opportunity count and interaction gap) are written to
the PSV session log so that this choice can be checked against observed runs.

The current event distribution comes from a mouse. Head pose in VR will change dwell timing,
wrong-target misses and especially abandon frequency. Raw `dwell_ms` is therefore not used, timing
tests are expressed as a fraction of each advertised split interval, and repeated abandons within
one opportunity are coalesced into that opportunity and do not raise confidence or evict its split
boundary. Those protections do not prove the mapping transfers:
the 85 % boundary, freshness windows, strain weights and eight-opportunity confidence ramp remain
provisional until Week E runs with head control. Arrival-time classification also assumes the local
WebSocket does not add interval-scale jitter.

---

## The spectator's trace and reveal (prompts 3.3, 3.5)

**Handled by:** `web/spectator/`. The frozen contract carries live beat and state messages, but no
history. The screen can therefore hold the completed trace through the 20-second reset and following
idle only while that browser page remains open. Reloading or opening a second spectator during idle
produces the honest cold-idle view, with no trace, rather than inventing or replaying one. A page
opened during a session starts its trace with the next scheduled live beat, so its final held trace
contains only the part it actually observed. Persisting or reconstructing a trace would require a
future, explicitly versioned contract change.

The screen judges the feed lost after **2.5 seconds without a state message**, against the host's
fixed 2-second state cadence. It freezes the last verified view, cancels pending beat draws and puts
a full-width red marker over it. This is intentionally aggressive for an exhibition display; verify
on the booth network that normal scheduling jitter does not create false disconnects.

**Visual port, 21 September:** v3's uncertainty hatching is a display convention: its half-width is
`0.45 × (1 − confidence)` around the host reading, clipped to 0–1. It is not a calibrated statistical
interval and cannot add confidence or authority. Unknown readings show no reading fill; valence
always has zero confidence and authority. The field's original 3.4 placeholder is now replaced
by the shared renderer (ambient-field section below); the 3.5 reveal uses
the same local IBM Plex fonts, independent of internet access. The browser
regression check covers trace/card clipping, including a low endpoint of 34 BPM, and loss of state
messages while clock replies continue. Booth-distance readability still needs an on-site check.

**Trace reveal, 21 September:** the screen shows it on the first resolve state reporting at most
20 s remaining, not on a local timer. With the frozen 2 s state cadence the reveal can appear up to
one state interval after the threshold. No session-duration assumption or adaptive-regulate guess
is involved. The three numbers use the plotted non-rejected beats: first baseline reading, maximum
in **load only**, latest resolve reading. Segment membership uses `t_play` and host boundaries
(`t_engine − segment_elapsed_ms`), so a delayed boundary can correct a provisional classification.
The contract does not carry the earlier physiological detection timestamp; this is the displayed,
scheduled-beat timeline. State-cadence HR, whole-session peaks and `drop_bpm` never supply N.

The values display one decimal; N subtracts those same displayed numbers and stays signed. It does
not choose a close or declare a result. While resolve continues, LEFT AT is labelled as updating;
on completed reset it freezes at the final observed resolve beat. If no resolve beat was observed,
LEFT AT and N stay unavailable. If load was not observed, its peak and N stay unavailable. Late-open
or reconnected screens label partial history; they cannot reconstruct missing beats or guarantee a
missing load peak. Opening after baseline also leaves SAT DOWN AT unavailable. Keep the spectator
open and connected from baseline to photograph a whole session.

The historical authority bars show **maximum observed `state.authority`**, not current idle values,
not inferred confidence and not a client-computed ceiling. NONE OBSERVED does not claim there was
none in missing history. Live resting references use only current `hr_base`; a null removes line
and label, with no fallback to first HR or a client mean. Completed reveals retain that session's
last reference with the frozen trace; idle's new, empty session does not overwrite it. A new baseline
clears it, so a degraded visitor never inherits the previous visitor's resting rate. Autoscaling
includes both all plotted beats and a non-null resting reference. Nothing is persisted on reload.

---

## The attendant console and recovery (prompt 3.6)

**21 September.** The console is `bridge/console.py`, on the live loop's asyncio thread, in
the same process. There is no network control path. Space or Enter arms in idle, cancels and
disarms during the pulse-aligned countdown, and stops a running session through its existing
3 s reset. During reset, another press only says to wait. The host owns all timing; the countdown
does not start a session clock. Press, planned wait, cancellation and firing are logged. The
final alignment wait now yields to local input instead of blocking cancellation in its last 50 ms.

Signal loss uses `Session.signal_lost`, unchanged at 6.2 s after the last accepted beat, with a
red background over the whole console and large LOST lettering. It never triggers a console
auto-stop. Baseline notices use only `baseline_end.outcome` and `problems`; slope quality cannot
turn a failed gate into success. The console never reads `regulate_result` and never calculates
or chooses a spoken close. Its reminder points to peaked-at minus left-at on the spectator trace.

**Test and VR booth modes, clarified 21 September:** the default launcher stays in local browser
test mode: a localhost-only bridge, `task-screen` as the task-event producer, a browser task on
65% of the laptop beside a 35% terminal console, and a full-screen external spectator. `--booth`
switches all of these together: bind the selected local RFC1918 LAN address, accept `quest` as
the task-event producer, open **no browser task**, make the laptop console full screen and keep
the external spectator full screen. The participant does the task in the headset and never
looks at the laptop. Prompt 3.2 permits only one bound task-event producer; opening a browser
task in booth mode would compete with the headset for that slot.

LAN access is enabled only by `--booth`, on the project's own router, **never venue Wi-Fi**.
The launcher prints `ws://LAN-IP:port/live` for the headset. If more than one suitable local
adapter is available, select the router-facing address explicitly with `--lan-ip`; a private
address alone does not prove that it belongs to the intended router. This mode switch does not
add network start/stop controls: the attendant button remains in-process.

In browser-task testing only, geometry uses the configured physical laptop width apportioned to
the actual viewport, including Windows DPI scaling, and updates on resize. The task's fullscreen
button is hidden so it cannot cover the console. The 27-inch/59.77 cm default remains provisional:
supply the actual laptop width and viewing distance for browser tests. Tiling does not validate
those measurements, and laptop viewing geometry is not the headset's geometry.

`python -m tools.launch` supervises the real bridge/engine and separate task/spectator Edge
profiles in default test mode; `--booth` supervises the bridge/engine and spectator only.
The console is its own Windows Console Host window; no extra dependency or service is installed.
Read-only status files and read-only browser diagnostics monitor readiness; neither carries a
session command. A bridge crash gives a fresh idle session id, no armed start and an explicit
VISITOR MUST START AGAIN notice, held until a new start succeeds. A browser-only restart keeps the
host session running, but cannot recover missing trace history (the reveal remains labelled partial).
The default packet source remains synthetic until 2.8 and the console labels that plainly.

Q deliberately shuts down the booth; crashes are restarted. The supervisor is the outer lifetime
boundary: killing it, shutting down Windows or removing power is not automatically recovered by
an uninstalled OS service. Hardware loss, missing dependencies and a permanently failing process
cannot be promised a 30 s recovery. The existing armband/audio hardware checklist is unchanged.

**Measured 21 September, 12:54 +0400**, by `python -m tools.check_recovery`. This run used the
committed engine/shim, the real default audio device and real clock, synthetic RR, and two
headless Edge pages. This is the three-role **default browser-test configuration**, not a
measurement of the headset or `--booth` mode. Each role was killed during baseline. Recovery
ended only when audio was advancing, the console was fresh, trusted beats were
arriving and both pages had reconnected.

| Process killed | Full software recovery | Session afterwards |
|---|---|---|
| Bridge (including its console and engine) | 6.469 s | New id, idle, disarmed; VISITOR MUST START AGAIN |
| Task browser | 2.438 s | Same host session, still baseline |
| Spectator browser | 3.559 s | Same host session; new screen has partial history |

All three were below 30 s. Both aligned starts in that run fired at frame 480,000 with zero
late frames. Q then shut down the booth cleanly. Reproduction logs and JSON report are under
`logs/recovery-20260921-125334/` (gitignored). Windows terminal input was separately exercised
against the real bridge: press, cancel about 499 ms later, no start firing, then clean Q shutdown.
Unit tests cover stop/reset, all four refusal strings, final-window cancellation, restart warning,
signal-loss display without auto-stop, and failed gate reporting despite a slope quality of 1.0.
In the default browser-test layout, the owned Console Host window was separately placed at
x=1248 on the 1920-pixel laptop display, with width 672 and height 1038 (Windows quantizes the
terminal to character cells). Ten consecutive placement checks stayed stable. An actual Edge
task viewport at 960 CSS pixels / DPR 1.25 on a
configured 1920-pixel, 60 cm-wide monitor reported 37.5 cm; resizing to 768 reported 30.0 cm.
Its fullscreen button stayed hidden, with no JavaScript exceptions. These are separate checks,
not a claim that the unavailable external display was exercised.

For the booth-mode correction, an owned Console Host was separately checked in full-screen mode
on the laptop: all 20 samples over 5 s reported full-screen mode and bounds `(0, 0, 1920, 1080)`;
the launcher's placement checks remained stable after the initial two startup polls. This
console-only check opened no audio device or LAN listener. Loopback socket tests cover `quest`
producer admission, rejection of browser task events in Quest mode, and producer reconnection.
They do not verify the physical external screen, router/firewall path or headset application.

**OPEN — a multi-second bridge stall can reach audio as an out-of-range RR interval.** In the
first two recovery attempts, fresh Edge profiles were on the project's D: drive. Local page
loading stalled for seconds and the combined run starved the bridge. The audio interface rejected
a scheduler event whose RR exceeded the shim's accepted range with `PLS_ERROR_INVALID_ARGUMENT`.
This was **mitigated by faster browser startup, not fixed**: profiles now live in the system
temporary directory on C:, reducing an isolated task-page load from 11.477 s to 1.114 s. The
combined recovery run then passed without unplanned restarts, but neither that pass nor omitting
the browser task in `--booth` mode resolves the stall-handling bug. Heartbeat and scheduler
code were deliberately not changed under light review. Investigating and fixing their behaviour
after a stall requires a separately authorised heavy-review task.

Other **potential booth stall sources, not established causes of the observed failure**, include:

- Synchronous log/status-file writes, slow storage, antivirus scanning, indexing or cloud sync
  contending for disk access.
- CPU contention from headset rendering or streaming, browsers and background updates; thermal
  or power throttling; memory pressure and paging.
- Windows sleep/resume or other long process descheduling.
- A blocking Python operation, native control call or future BLE/driver call made on the live
  loop's thread, or device/driver interrupt load and system latency delaying that thread.

A slow network or delayed packet alone is not a bridge-loop stall: the loop should still tick
while awaiting asynchronous I/O. It becomes this risk if a call blocks the loop or system
contention prevents it running. No general multi-second-stall recovery guarantee has been proved.

**Still unverified:** physical placement/fullscreen on the actual two-display VR booth, visibility
from across the hall, headset task-producer integration/recovery, browser-test laptop viewing
geometry, recovery with the real BLE source, and the existing wired-headphone/DAC/device-loss
checks. Only one physical display was connected for this run. Headless timing verifies software
recovery in the stated test configuration, not the missing display, headset or armband.

---

## The shared ambient field (prompt 3.4, 22 September)

**Reference and mapping.** The ten frames exist at `docs/design/field-frames.html`. Only the
component from the last JSON-escaped script is extracted; the design bundle is never launched.
`web/shared/field_mapping.js` is the portable state-to-seven-values mapping; the renderer is
separate. Both browser surfaces use it. Load/regulate use `0.5 + (psv - 0.5) × state.authority`
once; the load ceiling is already in that authority. Baseline confidence excludes valence and
uses the script's 0.393 divisor, not `baseline_quality` or a timer. Resolve keeps the observed
regulate-entry offsets and scales them by the host's remaining authority ratios. A client
joining in resolve has no entry history and uses its fixed palette, not invented past values.
All exact formulas, reference comparisons and reproduction commands: `docs/field-reference.md`.

**Horizon / underground-light risk.** Positions are measured from the top. Larger y means a
lower horizon and more sky. The diffuse source centre is always **x 0.50, y 0.40**. Current
segment mappings have horizon y ≥ 0.44, so the centre stays above ground. Retuning the horizon
**above y 0.40 on screen (numerically below 0.40)** would put the source underground; y = 0.40
puts it on the horizon. The global token minimum is 0.38, so the global clamp does not prevent
this: recheck the geometry if ranges change. Never move the source to conceal such a retune.

**Visual-only high-rate taper, 22 September.** The script's old claim about 95 bpm being below
*any* photosensitivity threshold was not validated. Holding amplitude above that authored
endpoint left a gap: 180 bpm would give a 3 Hz visual cadence. The audio heartbeat is the
demo's primary evidence; the visual pulse is secondary and gives way. Every segment's authored
amplitude is now multiplied by **`clamp((120 − hr_bpm) / 25, 0, 1)`**: full through 95 bpm,
half at 107.5, **zero at and above 120 bpm (2 Hz)**. The original 62–95 bpm amplitude mapping
still clamps at its endpoints before tapering. Resolve's final-three-second equal-power fade
multiplies the tapered amplitude. Unknown HR gives zero. There is no every-Nth-beat substitute.
The audio path, scheduler and heartbeat levels are unchanged by this visual-only correction:
every eligible scheduled audio beat still plays at every supported rate.

The state mapping uses `state.hr_bpm`; it is not the only protection. A live beat uses the
maximum rate from its `hr_bpm`, `60000 / rr_ms`, and `60000 / planned_onset_interval_ms`.
The preceding onset is the last valid, non-rejected future candidate, **including a candidate
suppressed visually**; 500 ms or shorter intervals or beat rates at least 120 bpm cannot flash.
The view caps the mapped amplitude by the active beat's tapered segment amplitude using a
minimum, not a second taper multiplication. Thus a fast stream cannot use the two-second
state cadence's stale low-rate brightness or turn suppression into alternating flashes.
Only future non-rejected beats are eligible; late, duplicate and wrong-session beats do not
flash. The envelope retains its 90 ms raised-cosine rise, 180 ms fall and no-addition rule.
Suppressed visuals are neither replayed nor substituted, and do not suppress audio.

The renderer additionally enforces, **per actual 8-bit output pixel**, a positive linear-sRGB
luminance change no greater than the token amplitude (hard-capped at 0.22) times the unpulsed
base-gradient luminance, and no greater than **0.09 absolute**. Using the darker base gradient
is stricter than using the complete unpulsed field. Only local fog/striation and diffuse-light
layers brighten; the base gradient, full-field ambient haze, task objects, reticle and labels
are untouched. Fog/light masks leave an unchanged part of the field; there is no full-field
brightness multiplier. The same caps survive palette cross-dissolves and quantization.

This is informed by the [W3C general-flash definition](https://www.w3.org/WAI/WCAG22/Understanding/three-flashes-or-below-threshold),
which considers changes of at least 0.10 in relative luminance and the number/area of flashes.
The extra 0.09 cap leaves a digital margin below that general-flash amplitude criterion;
the ratio-only 0.22 rail would not itself prove this for arbitrary bright pixels. These tests
**do not certify photosensitivity safety**, red-flash compliance of an entire experience, or
the optical output of a headset. They do not cover simultaneous task/other-UI changes, display
brightness/HDR, headset optics, motion relative to the eye, or individual susceptibility.
Hardware/display-specific assessment is still required; make no medical safety claim.

**Timing and portability.** Mandatory pulse coverage spans every integer rate 45–180 bpm,
including unchanged amplitude through 95, the taper to 120 and zero visual output from 120
through 180; boundary and stale-state/mismatched-rate checks exercise the live guard as well.
The same renderer checks retain the 0.22 relative / 0.09 absolute rails and fog/light-only
modulation. Verified on 22 September: **136 integer rates × four segments**, with **417,792
actual framebuffer pixel comparisons**; all **244 cases at 120–180 bpm** were byte-identical
to the unpulsed field. The 40 JavaScript field checks also cover the 107.5-bpm midpoint,
stale states, disagreeing beat metadata/cadence, dissolve boundaries and resolve's final fade.
The existing ten-reference-frame and 31 rendered-luminance cases still pass.
Late arrival beats and first-render onsets missed by more than 45 ms are dropped,
not replayed.
The 90 ms rise describes the continuous envelope; a 60/90 Hz display samples it in frames,
and 8-bit output adds quantization. This is not millisecond DAC/display alignment evidence.
Cross-dissolve target weights follow host segment elapsed across ten seconds, with up to
250 ms presentation-only easing to each newly reported weight, never beyond it. Drift is
cosmetic animation; it does not advance segment state. A lost/stale link freezes all field
pixels and clears queued beats, with the existing visible marker. Renderer unavailability is
shown as FIELD UNAVAILABLE rather than an invented scene. Unity must port the mapping and
limits, not just the pictures; Quest 90 fps and optical output remain unverified.

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
