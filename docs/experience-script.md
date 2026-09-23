# **Prism Live: Experience Script v1.9**

&nbsp;

&nbsp;

Two rules this document was written under:

&nbsp;

1. **Numbers, not adjectives.**  
2. **This is not a film.** Every value below is a range, a direction or a bound. The person in the chair fills in the rest.

&nbsp;

---

## 0\. Global settings

| Field | Value |
| :---- | :---- |
| Total target duration | **4:11 nominal** (provisional, see the note below), **4:45 hard cap**. **Both count from when start fires**, which is when baseline begins (decided 14 September). 4:11 because baseline is held to 56 s, where load starts on a pulse boundary (prompt 2.7, option 1). **The worst case is 56 + 75 + 105 + 45 = 281 s, 4:41**: baseline to its pulse boundary, load, regulate with its full 30 s extension, and resolve. It fits the cap, and nothing is cut. The attendant console's countdown of up to 11 s to the pulse-aligned moment sits before start fires and is not part of the session (prompts 2.7 and 3.6). The wait is not part of the experience, so it does not count against a cap that limits the experience. Throughput is protected by the cap existing at all; cutting regulate's extension to fit a lower one would attack the primary success measure. |
| PSV emission cadence | 30 s nominal — **but see §6.1**, the demo runs it at 2 s |
| Baseline window | 45 s |
| Authority rule | authority \= min(confidence, segment\_ceiling), per dimension, no exceptions |
| Valence policy | Confidence sits at floor for the whole session. Deliberate, not a bug, and the thing the spectator screen exists to show. **In the current engine it is exactly 0.0, not "near" — design the bar for a hard zero (§6.4).** |
| **Audio ceiling** | **True-peak −1.0 dBTP**, enforced by the engine's master limiter as the last stage, unbypassable. **Integrated programme loudness −16 LUFS** across the nominal 4:00. **Short-term (3 s) never above −11 LUFS** anywhere in the session. |
| **Loudness you tune at** | **Flat reference monitoring only** — open-back studio headphones or nearfields. Author and balance there, then *verify* on the XM5 with ANC on. **Never tune on the XM5**: its bass shelf flatters the per-beat layer, and anything balanced on it will be written too quiet in the sub and will vanish on reference. Attendant sets headphone volume once at setup, marks it on the laptop, and never adjusts it per person. |
| Frame rate target | 90 fps |
| Claims tier | Marketing. Describe what the system does, never what it achieves. |

**Heartbeat timing, 23 September 2026.** The heartbeat layer plays measured optical beat intervals
after a delay. The phone sends PPI in batches and the laptop buffers accepted beats before
scheduled playback; neither the low tone nor its matching visual pulse represents a heartbeat
occurring at that instant. Preserve the measured rhythm rather than substitute predicted beats.
The spectator footer discloses delayed playback throughout, and the attendant explains it before
the session. The surrounding sound is composed during the session; do not describe the whole
experience as unrecorded, instantaneous or as a real-time mirror of the person's heart. Measured
timing and remaining limitations are in `docs/known-limits.md`.

The current buffer is 12 s on the reconstructed timeline, not a measurement of physical
acquisition latency. Recording-specific timing results remain in private local reports. A new baseline excludes
all pre-start measurements, including late batches: its first roughly 12 s have no heartbeat
layer while this visitor's buffer fills. The bed and field continue. The ending is not extended
to play the remaining delayed tail. This prevents a short stop/reset from replaying another
visitor's beats; it does not move the phone's separate ~24 s startup into the session.

&nbsp;

**Note, 13 September 2026: the nominal duration is provisional and under review.** It is now 4:11 rather than 4:00, because option 1 in prompt 2.7 holds baseline to 56 s. Four minutes or more may be too long for someone wearing a headset. It is decided in Week E against the 20 real runs, not on paper (`docs/solo-build-plan.md` E.2a). **Do not shorten load or regulate.** Load raises the heart rate and regulate brings it down, and those two are what the demo proves. If the run needs shortening:

- **Resolve is the cheap cut:** 45 s down to 35 s, and the final 7 s of the heartbeat alone is untouchable.
- **Baseline is not cheap.** Cutting its capture from 45 s to 35 s means re-setting the quality gate, which asks for 35 of 45 s of clean data. The baseline visual would also stop fully resolving: at 35 s the confidence curve reaches only about 0.76 of its rescaled travel, where it reaches 1.0 at 45 s.

&nbsp;

**Spectral reservation (non-negotiable, or the demo's evidence disappears).** 36–62 Hz belongs to the per-beat layer alone. Every scene's sub stem is high-passed at 62 Hz, 24 dB/oct. Without this the heartbeat is masked by the drone and the one thing the person is supposed to *hear* becomes mud.

&nbsp;

**Segment short-term loudness targets** (3 s window, LUFS):

&nbsp;

| Segment | Range |
| :---- | :---- |
| Baseline | −22 → −17 |
| Load | −17 → −12 |
| Regulate | −20 → −14, descending across the segment |
| Resolve | −24 → −16, tapering to silence |

&nbsp;

**Important on authority ceilings.** In the regulate segment every dimension's ceiling is 1.0 and confidence is the only limiter. Valence is **not** hand-capped. The whole point is that it limits itself, live, in front of the person watching the screen. Cap it manually and you have faked the one thing that is real.

&nbsp;

---

## 1\. Segment template

The schema. §2 is this block filled four times, in table form.

&nbsp;

SEGMENT: \<name\>

&nbsp;

Duration / Entry / Exit / Adaptive extension / Timeout branch

&nbsp;

AUTHORITY CEILING       arousal, valence, cognitive\_load, readiness

&nbsp;

REGULATION TARGET       dimension, direction, success threshold

&nbsp;

AUDIO                   material, driven by, dynamic range

&nbsp;

PER-BEAT LAYER          active, amplitude, frequency band

&nbsp;

VISUAL                  base frame, driven axes

&nbsp;

PERSON                  what they do, what they are told

&nbsp;

SPECTATOR SCREEN        foregrounded

&nbsp;

---

## 2\. The four segments

Timings inside a segment are written as offsets from that segment's own start (t) or end (T), never as absolute session times — regulate is adaptive, so absolute times are a lie after 2:00.

**Note, 11 September 2026.** The audio arc in this section has not been validated against the engine's PSV-to-audio mapping. Read against that mapping (`docs/engine-findings.md`), regulate currently comes out inverted: at this section's own targets (arousal 0.32, cognitive_load 0.28, readiness 0.62) the engine sits at about 2,400 Hz with the `pulse` stem open and bed and sub thinner, not at 620 Hz with everything subtracted. The pulse and air gates are tied to the filter and to loop boundaries, so the air moves written below (a gradual fall in regulate, a tail in resolve) cannot be produced. Decision of 11 September: air gates out before pulse in one 1.5 s fade, and there is no air tail. Whether the engine is fed body-derived values or a designed pose per segment is decided by listening in Week B. The numbers below are unchanged until then.

**Note, 13 September 2026.** The baseline capture is still 45 s, but its result, and with it HR\_base, arrives 2.0 to 10.6 s after the window closes. The session now holds in baseline to 56 s, when load starts on a pulse boundary, and enters load without HR\_base if it has still not come (`docs/all-prompts.md` prompts 2.4 and 2.7). Duration and Exit below describe the capture, and absolute times are unreliable from 0:45, not only after 2:00.

### Segment 1 — BASELINE

| Field | Value |
| :---- | :---- |
| Duration | 45 s, fixed |
| Entry | Attendant presses start. The button arms and fires at the pulse-aligned moment, up to 11 s later (prompt 2.7); baseline begins then |
| Exit | 45 s elapsed |
| Adaptive extension | None |
| Timeout branch | N/A |
| Authority ceilings | arousal 0.0 · valence 0.0 · cognitive\_load 0.0 · readiness 0.0. The engine observes and does not act. |
| Regulation target | None |
| Success threshold | Not a success gate. **Quality gate:** ≥ 35 of 45 s of clean beat data, and ≥ 30 accepted RR intervals, or the attendant re-seats the armband and restarts. |
| **Audio** | **Material:** two stems only — bed (sustained pad, D minor, root D3 \= 146.8 Hz, 19 s loop) and sub (drone, D2 \= 73.4 Hz, high-passed 62 Hz, 17 s loop). pulse, lead, air closed. No rhythmic content, no melodic foreground, no transients above the heartbeat. **Driven by:** nothing from the PSV — authority is 0\. The only movement in 45 s is the person's own pulse. Master LP cutoff fixed at **1,400 Hz**. **Dynamic range:** −22 → −17 LUFS short-term. |
| Per-beat layer | **Active.** This is where they first hear themselves. **Amplitude:** −18 → −13 dBFS peak, ramped up across the first 12 s so the first beat is not a surprise. **Frequency band:** 44 Hz fundamental, energy confined 36–62 Hz, roll-off 24 dB/oct above 120 Hz. Envelope 8 ms attack, decay min(220 ms, 0.55 × current RR interval) so beats never overlap. |
| **Visual base frame** | Baseline base. **Driven axes:** baseline\_confidence (0.0 → 1.0) → **fog density 0.38 → 0.26**, **light intensity 0.45 → 0.62**, **horizon position 0.44 → 0.50**. baseline\_confidence is the mean confidence of arousal, cognitive\_load and readiness, divided by **0.393** and capped at 1.0. Valence is left out because its confidence is 0.0 by design, so a mean over four could never pass 0.75. 0.393 is the most the three reach during baseline, and a settled person usually reaches it by t=45 s; at slow heart rates, around 48 bpm, it tops out near 0.94 of the travel. The climb is slow for the first 20 s and fastest from 25 to 40 s: about 0.25 at t=20 s, 0.56 at 30 s, 0.95 at 40 s (docs/vr-handoff.md §9 has the full curve). The world resolves as the system learns them — the in-headset counterpart of the confidence bars climbing on the spectator screen. Hue fixed **208°**, saturation fixed **0.12**, field motion rate fixed **0.008**. heartbeat → **pulse amplitude 0.06 → 0.10**. |
| Person does | Sits, looks around. Nothing asked of them. |
| **Person is told** | *(attendant, before the headset goes on)* — **"Sit however you're comfortable. The low heartbeat is your measured rhythm played back after a delay, not the beat happening this instant. For the first minute you don't have to do anything at all. Just look around. It's learning what your normal looks like, so normal is exactly what we want."** *(No in-headset text.)* |
| Spectator foreground | Confidence bars climbing from zero as the baseline fills. |

&nbsp;

The narrative job of this segment is that the crowd watches confidence build as measured batches arrive. That is the most legible thing on the screen all session and it happens during baseline; it does not imply instantaneous physiological readings.

&nbsp;

---

### Segment 2 — LOAD

| Field | Value |
| :---- | :---- |
| Duration | 75 s, fixed |
| Entry | Baseline complete |
| Exit | 75 s elapsed |
| Adaptive extension | None |
| Timeout branch | N/A — fixed duration |
| **Authority ceilings** | arousal **0.20** · valence 0.0 · cognitive\_load **0.20** · readiness 0.0. Present but not yet working. *These two partially oppose each other by design — rising arousal opens density, rising load recedes it. That is correct: the engine is barely acting, the task is doing the work.* |
| Regulation target | None. This segment raises, it does not regulate. |
| **Success threshold** | HR\_base \= mean HR over the final 30 s of BASELINE. HR\_load \= mean HR over the final 30 s of LOAD. **Activated when HR\_load ≥ HR\_base \+ 6 bpm.** Secondary, either alone also counts: **RMSSD over the final 30 s ≤ 0.80 × baseline RMSSD.** It counts only when both 30 s windows hold at least 20 clean successive differences; fewer, and a 30 s RMSSD is too often more than 20 % off. Recorded per run; drives the Week 5 tuning in plan task 5.3. |
| **Audio** | **Material:** bed and sub carry over unchanged (same key, same loops — the world does not change, the person does). pulse opens at t=0 (non-melodic, 11 s loop, 96 BPM implied, no downbeat). air opens at t≈25 s as arousal rises. **lead stays closed for the whole segment** — no melodic foreground competes with a visual task (PGAE §5 focus protection). **Driven by:** arousal\_effective × 0.20 → master LP cutoff **1,400 → 3,600 Hz**, pulse gain **−24 → −16 dBFS**. cognitive\_load\_effective × 0.20 → air gain **−26 → −30 dBFS**, transient density down. **Dynamic range:** −17 → −12 LUFS short-term. |
| Per-beat layer | **Active, and this is where they hear it speed up.** **Amplitude:** −13 → −9 dBFS peak, scaled by instantaneous HR: −13 dBFS at HR\_base, −9 dBFS at HR\_base \+ 15 bpm, clamped. **Frequency band:** unchanged — 44 Hz fundamental, 36–62 Hz. Decay still min(220 ms, 0.55 × RR), which shortens on its own as they speed up. |
| **Visual base frame** | Load base. **Driven axes:** arousal\_effective × 0.20 → **hue 214° → 202°**, **saturation 0.16 → 0.30**, **light intensity 0.70 → 1.05**. cognitive\_load\_effective × 0.20 → **fog density 0.20 → 0.30**, **field motion rate 0.010 → 0.004** (the field *slows* as load rises — less to fight). Horizon fixed **0.50**. heartbeat → **pulse amplitude 0.10 → 0.16**. |
| **Person does** | Drifting object splits, select the correct half by holding the reticle on it, difficulty ramps. **Ramp, linear over 75 s:** dwell-to-select **900 → 420 ms**; split interval **5.5 → 2.2 s**; distractor halves after each split **1 → 3**; object angular speed **6 → 19 °/s**. **Miss penalty:** each miss brings the next split 0.4 s sooner, compounding, floor 1.8 s. **Note on the ramp.** These values were authored assuming eye control, which the Quest 3S cannot provide. What is built is head-pose or pointer control, which is harder at the same numbers because the neck or hand has to track a target moving at up to 19 degrees per second. Treat every value here as provisional until Week E retunes them against real runs. The extra physical effort is not a problem: it raises heart rate, which is what this segment exists to do. No controllers, no hand tracking. Reticle only, driven by head pose in VR or by pointer on the task screen. The Quest 3S has no eye tracking hardware. |
| **Person is told** | *(attendant, before the segment)* — **"Next part you've got a job. A shape will drift in front of you and then split in two. One half keeps moving, one stops. Keep the one that's still moving in the centre and hold it there until it locks. It speeds up as it goes. Keep up as best you can — nobody gets all of them."** *(in-headset, 3 s before t=0, one line, then it clears)* — **"Follow the half that's still moving."** |
| Spectator foreground | Arousal and cognitive\_load rising, authority still near zero. |

&nbsp;

**This segment carries the whole demo.** If their heart rate does not rise here, nothing can fall later and the proof does not land. The ramp above is deliberately harder than feels comfortable. Soften it in Week 5 against real data, not before.

&nbsp;

---

### Segment 3 — REGULATE

| Field | Value |
| :---- | :---- |
| Duration | 75 s, adaptive |
| Entry | Load complete |
| Exit | Success threshold met, or duration elapsed |
| Adaptive extension | Up to **\+30 s** if the threshold has not been met. Hard cap, protects booth throughput. With no HR\_base, after a degraded baseline, there is no threshold to wait for, and regulate runs a plain 75 s. |
| Timeout branch | Proceed to resolve anyway. The trace reports honestly. The close follows the visible fall on the trace screen, not the timeout (§3). |
| Authority ceilings | arousal 1.0 · valence 1.0 · cognitive\_load 1.0 · readiness 1.0. Confidence is the only limiter. |
| **Regulation target** | **Primary: arousal, direction DOWN, target value 0.32.** **Secondary: cognitive\_load, direction DOWN, target value 0.28.** Held: readiness target 0.62 (up, but confidence will be moderate so it acts weakly, which is honest). valence target 0.50 — no authority, it will not move, and that is the point. |
| **Success threshold** | Let rise \= HR\_load − HR\_base (from §2 LOAD). **Regulated when any 20 s rolling window has mean HR ≤ HR\_load − max(5 bpm, 0.5 × rise).** Floor of 5 bpm so a person who barely activated still has a reachable bar. Secondary, recorded not gating: RMSSD returns to ≥ 0.95 × baseline. |
| **Audio** | **Material:** the payoff, and it is subtraction, not addition. pulse closes over t=0→18 s. air gain falls. lead **never opens.** bed and sub remain and thicken. Same key, same loops throughout — the seam the person must not hear. **Driven by:** arousal\_effective (authority \= confidence, up to 1.0) → master LP cutoff **3,600 → 620 Hz**, bed gain **−16 → −13 dBFS**, sub gain **−20 → −15 dBFS**. cognitive\_load\_effective → air gain **−28 → −38 dBFS**, pulse gain **−16 → off**. All moves slew-limited; nothing steps. **Dynamic range:** −20 → −14 LUFS short-term, descending. Note it gets *darker*, not quieter — the sub grows as the top ends. |
| Per-beat layer | **Active, and this is the payoff. They hear it slow.** **Amplitude:** −9 → −11 dBFS peak, i.e. it recedes only slightly. It must stay clearly audible as everything above it withdraws. **Frequency band:** unchanged, 44 Hz fundamental, 36–62 Hz. As the LP cutoff drops to 620 Hz the pulse becomes proportionally *more* of what they hear, without its own gain moving. |
| **Visual base frame** | Regulate base. **Transition from Load base is a 10 s cross-dissolve between the two frames, not a hue sweep** (see §4, interpolation rule). **Driven axes:** arousal\_effective → **hue 46° → 24°**, **light intensity 0.95 → 0.38**, **fog density 0.34 → 0.58**, **field motion rate 0.030 → 0.008**. cognitive\_load\_effective → **saturation 0.30 → 0.14**, **horizon position 0.56 → 0.44**. heartbeat → **pulse amplitude 0.16 → 0.08** — the visual pulse shrinks as their rate falls, so the screen and their body agree. |
| Person does | Nothing. Task is over. |
| **Person is told** | *(attendant, one line as the task ends, then silence for the whole segment)* — **"That's the task done. Nothing to do from here."** **Then say nothing at all.** Do not narrate this segment to the person wearing the headset. The narrator keeps talking to the *crowd*, at the screen, not to them. If they speak or ask something, one line only: **"You're fine, just listen."** |
| Spectator foreground | Authority ramping with confidence, and the valence axis visibly sitting still. |

&nbsp;

---

### Segment 4 — RESOLVE

| Field | Value |
| :---- | :---- |
| Duration | 45 s |
| Entry | Regulate complete or timed out |
| Exit | 45 s elapsed |
| Adaptive extension | None |
| Timeout branch | N/A |
| Authority ceilings | Taper all four from their entry value to 0.0, linearly, across T−45 s → T−12 s. |
| Regulation target | Tapers back to neutral 0.50 on every dimension as authority tapers. |
| **Audio** | **Material:** bed and sub only; air tail decays out over the first 10 s. Master LP cutoff holds at 620 Hz, then opens slightly to **900 Hz** across T−20 s → T−12 s as the trace appears — the one small lift in the segment, so the ending is a resolution and not a fade-out. **Driven by:** authority taper only. By T−12 s no PSV dimension is driving anything. **The ending, as offsets from segment end T:** T−22 s → T−10 s — bed, sub and air taper to silence. T−10 s → T−3 s — **the per-beat layer plays alone.** Nothing else is sounding. T−3 s → T — equal-power fade of the pulse to silence. **Dynamic range:** −24 → −16 LUFS short-term, then to silence. |
| **Per-beat layer** | **It holds, and it is the last thing in the room.** Everything else clears out from under it (see the ending schedule above), so the final 7 seconds of a four-minute experience are the person's own heartbeat with nothing on top of it. That is the argument the whole demo exists to make; do not cross-fade it out early, and do not let a bed tail run underneath it. **Amplitude:** −11 dBFS held, then equal-power to silence over the final 3 s. **Frequency band:** unchanged, 44 Hz, 36–62 Hz. |
| **Visual base frame** | Resolve base. 10 s cross-dissolve in from Regulate base. **Driven axes:** authority taper (1.0 → 0.0) → every axis eases to the Resolve base values and holds: **hue 30°, saturation 0.14, fog density 0.44, light intensity 0.50, field motion rate 0.006**. Trace reveal → **horizon position 0.44 → 0.58** across T−20 s → T−12 s, the field opening as their own line is drawn. heartbeat → **pulse amplitude 0.08 held**, then to 0.00 across the final 3 s, in lockstep with the audio. |
| Person does | Nothing. |
| **Person is told** | *(in-headset, at T−20 s, as the trace draws)* — **"This is your heart rate, from the moment you sat down."** *(attendant, as the headset comes off)* — **"That line is yours. Nobody else gets that one. Take a photo of the big screen if you want it — it'll be up for about twenty seconds."** |
| Spectator foreground | Their trace, start against end. |

&nbsp;

Then 20 seconds of trace held on the screen while they photograph it, before the close.

**The attendant's "twenty seconds" line needs a rewrite. Not rewritten yet (14 September).** "It'll be up for about twenty seconds" is no longer true: the spectator screen holds the trace through the close and through idle, until the next session's baseline begins (prompt 3.5), because the close reads N off it (§3).

&nbsp;

---

## 3\. The closes

Four variants, one spine. Only Beat 1 changes.

### Beat 1 — name what just happened to them. 15 s.

|  | It moved | It did not move |
| :---- | :---- | :---- |
| **Investor** | "Your rate came down about **\[N\]** beats from where the task put it. Nobody scripted that — the system read each batch as it arrived and adjusted to the measurements. The heartbeat played those measured intervals after a delay. What's on the screen is the record, not an invented curve." | "Your trace is close to flat, and the system said so. It never claimed a change it couldn't measure — you can see the confidence on that dimension stayed where it was. That's the behaviour we build for. In a car or a classroom, a system that overstates what it's reading is worse than no system at all." |
| **Student** | "That line's your heart rate. It went up when the task got hard, and it came back down after. The low heartbeat replayed your measured intervals after a delay; the surrounding sound was composed during your session, not a playlist." | "Your line's pretty flat, which happens plenty. That's the honest result and the system reported it — it didn't get much purchase on you today and it didn't pretend otherwise. The interesting part is that it knew." |

&nbsp;

**\[N\]** is the visible fall on the trace screen: **peaked at minus left at** (prompt 3.5), read live. It is never the session's `drop_bpm`, which is HR\_load minus the lowest 20 s window, a different number. **The close follows N alone.** 3 bpm or more takes the "It moved" close (close A). Under 3 bpm takes the "It did not move" close (close B), whatever the threshold logic decided and whether regulate timed out. The state machine produces no verdict; the attendant reads N off the screen.

**Definition, 21 September:** **Peaked at is the highest heart rate during the load segment only, not the whole session.** A person may sit down elevated from the exhibition floor and settle through baseline; that settling is not a response to the task. Close A says "from where the task put it", so only load contributes to its peak. **Sat down at stays the first reading**, honestly showing that settling. **Left at is the latest plotted resolve reading**, updating until the session ends and then held with the trace through reset and idle. The screen uses non-rejected scheduled beats, classified against host segment boundaries, for all three numbers. It displays one decimal place and subtracts those same displayed peak and endpoint numbers for N; a negative N stays negative. Missing readings show a dash, never an invented zero. No on-screen verdict or choice of close is produced.

**Delayed-playback caveat, 23 September:** the existing screen classifies those beats by `t_play`,
so "during load" currently means the **load playback window**, not an exact measurement window.
The peak caption makes this explicit. Buffered PPI can cross a segment edge before it plays; the
MQTT samples and frozen beat contract contain no per-beat acquisition timestamp. Do not infer that
a boundary-adjacent plotted beat was measured in that same segment, subtract a guessed delay in
the client, or describe the trace as instantaneous. The load-only peak and visible N rule are
unchanged; exact acquisition-segment attribution is a limitation, not a new on-screen decision.

**Both closes need a rewrite. Not rewritten yet (14 September).** Measured on synthetic sessions in the design critique of 13 September, against the closes as they were chosen until 14 September, by the threshold logic with close B on a timeout:

- **Close A's first clause asserts a rise that 13 % of regulated runs never had.** "It went up when the task got hard" (student), and "from where the task put it" (investor), are said to people who never met the load activation test: 107 of 818 regulated sessions, with simulated load rises of 0 to 20 bpm.
- **Close B tells 95 % of timeouts that their trace is flat, when it fell a median 5.9 bpm.** "Your trace is close to flat" and "Your line's pretty flat" are said over a visible fall of 3 bpm or more: 94 of 99 timeouts with a simulated load rise of 10 to 20 bpm. Counting the rise-6 sessions of the same sweep, it is 181 of 249 (73 %), median 4.3 bpm.

### Beat 2 — name what the system did. 20 s, one version, same for everyone.

> "The surrounding sound was composed while you sat there, not a pre-recorded mix. The low heartbeat played your measured intervals after a delay. The engine reads your state as four numbers — how activated you are, which way it's leaning, how much you're carrying, what you can take on — and every one of those numbers carries its own confidence. It's only allowed to push a number as hard as it's confident about it. That's why the second bar sat at zero the whole time you were in the chair: it couldn't read that one, so it left it alone. Same four numbers drive sound, haptics and interface — this room only had the sound."

### Beat 3 — the handoff. 10 s, one version.

> "There's a QR on this card. Two things behind it: the study app this engine runs inside, and Prism Venues, which is the same engine reading a room instead of a person. If you want the technical version rather than the demo version, my email's on there too."

&nbsp;

**The "did not move" versions were written first, on purpose.** They are the ones that will save an attendant standing in front of a flat trace with nothing to say. Read both out loud before Week 5\. Neither should sound like an apology.

&nbsp;

---

## 4\. Frame and token sheet

The Claude Design deliverable. Endpoint frames Dev B interpolates between — not storyboards.
**Available 22 September:** `docs/design/field-frames.html`. It is a bundled design: extract the
component from the last JSON-escaped script block, do not run the bundle. `web/shared/` is the
working plain-JS reference: `field_mapping.js` maps host state to seven tokens,
`field_pulse.js` schedules the visual heartbeat, and `field_renderer.js` draws them. Both browser
surfaces use it. See `docs/field-reference.md` for the exact Unity-port mapping and acceptance.

### Frames needed

| Frame | Purpose |
| :---- | :---- |
| Baseline base | The neutral field at rest |
| Load base | Same world, task palette |
| Regulate base | Same world, regulation palette |
| Resolve base | Same world, resolved palette |
| Arousal high | Top of the arousal-driven range |
| Arousal low | Bottom of it |
| Cognitive\_load high | Top |
| Cognitive\_load low | Bottom |
| Pulse at 95 bpm | Shape and amplitude of the per-beat visual pulse |
| Pulse at 62 bpm | Same, slow end |

### Token sheet

| Token | Min | Max | Unit / note |
| :---- | :---- | :---- | :---- |
| Fog density | **0.18** | **0.62** | Unity exponential fog density |
| Light intensity | **0.35** | **1.10** | multiplier on the single diffuse source |
| Hue range | **18°** | **216°** | HSV hue. See interpolation rule below. |
| Saturation | **0.08** | **0.36** | HSV saturation |
| Horizon position | **0.38** | **0.62** | normalised vertical position **measured from the top of the view**; **0.50 \= eye level**. Larger values lower the horizon and show more sky: resolve's 0.44 → 0.58 is the field opening. The light centre is fixed at **x 0.50, y 0.40** in every state. Current segment mappings keep the horizon at or below y 0.44 (numerically ≥ 0.44), above-ground light; the global token minimum alone does not guarantee that. |
| Pulse amplitude | **0.00** | **0.22** | peak-to-peak luminance modulation as a fraction of base field luminance |
| Field motion rate | **0.004** | **0.045** | normalised units·s⁻¹ of gradient/fog drift |

&nbsp;

**Hue interpolation rule.** *Within* a segment, interpolate hue directly — every within-segment range above spans less than 25°, so it stays in one colour family. *Between* segments, **cross-dissolve between the two base frames over 10 s. Never sweep hue between segments.** Baseline→Load is 208°→214° and could be swept, but Load→Regulate is 202°→46° and sweeping it passes through green and yellow, which looks like a bug. Dissolve everywhere, for consistency.

&nbsp;

**Flash and modulation rail (hard, applies to every frame and every driven axis).** Peak-to-peak luminance modulation from the per-beat layer never exceeds **0.22** of base field luminance, and is never applied as a full-field flash — it modulates the local fog and the diffuse source only, with a 90 ms raised-cosine rise and 180 ms fall. Envelopes never add. The base gradient and ambient full-field haze never pulse. Absolute linear-light luminance change is additionally capped at **0.09**, including quantized output and palette cross-dissolves. These rails are not style choices.

**Visual-only rate taper, decided 22 September.** Multiply every segment's authored pulse amplitude by **`clamp((120 − hr_bpm) / 25, 0, 1)`**: unchanged through **95 bpm**, half amplitude at **107.5 bpm**, and **exactly zero at and above 120 bpm**. The 62–95 bpm amplitude ranges in §2 still clamp to their authored endpoints before this multiplication; resolve's 0.08 amplitude uses the same taper as well as its final-three-second equal-power fade. Any §2 description of the pulse shrinking as the heart slows applies within the authored range, not across the high-rate taper. Unknown heart rate gives no visual pulse.

The state mapping applies this taper to `state.hr_bpm`. The live beat guard also uses the most conservative rate from the beat's `hr_bpm`, `60000 / rr_ms`, and `60000 / planned_onset_interval_ms`. The interval is measured from the last valid, non-rejected future candidate, including candidates suppressed visually; intervals of **500 ms or less** or beat rates **at least 120 bpm** cannot flash. The view takes the smaller of the mapped amplitude and the active beat's tapered segment amplitude, rather than multiplying the taper twice. This prevents the two-second state cadence from allowing a fresh fast beat to use stale slow-rate brightness. There is no every-Nth-beat visual substitute or replay.

**Reasoning:** the audio heartbeat is the evidence the demo rests on; the visual pulse is secondary. Removing the visual modulation by 120 bpm (2 Hz) keeps it from approaching the 180 bpm / 3 Hz cadence, while **the audio heartbeat continues to follow every eligible scheduled beat at every supported rate**, with its existing level/fade policy unchanged. Mandatory tests cover 45–180 bpm, the taper and zero-output threshold, the complete 90 ms rise on visible pulses, non-addition and the unchanged local-luminance rails. This replaces the unsupported original claim of being below *any* photosensitivity threshold at 95 bpm; it is **not** photosensitivity certification for a person, display, headset or whole experience. See `docs/known-limits.md`, ambient-field section.

&nbsp;

**Lens constraint, applies to every frame.** The Quest 3S uses Fresnel optics. No pinpoint bright sources on near-black, or you get glare. Mid-luminance fields, soft gradients, large diffuse light. Design to the lens from the first frame rather than fixing it in Week 3\. The light intensity floor of 0.35 rather than 0.0 is part of this: the field never goes to true black, so there is never a bright element sitting on one.

&nbsp;

---

## 5\. Definition of done

You can hand this to the devs when all of the following are true.

&nbsp;

1. ~~Every \[FILL\] is filled~~ — **done, this document.**  
2. ~~Every audio and visual field has numbers or ranges~~ — **done.**  
3. ~~Both success thresholds are stated as a measurable change~~ — **done, §2 LOAD and §2 REGULATE.**  
4. **The timeout branch is written** (§2 REGULATE) **and someone has read the "did not move" close out loud** — *outstanding, Ridhwan \+ Mariah.*  
5. **All ten frames exist with their token values** — **done, 22 September**, supplied as `docs/design/field-frames.html`, not a Figma dependency. Shared-renderer comparisons: `docs/field-reference.md`.
6. **Every spoken line is inside the marketing claims tier** — *outstanding, Week 3 task 3.10. Nothing in §2 or §3 asserts an outcome; every line describes what the system does or reports. Needs the formal pass anyway.*

&nbsp;

---

## 6\. Engine bindings — read before quoting any number above

Everything in §0–§4 is written against the engine as it exists on main today. Six things this script assumes that the engine does **not** currently do. None is large; all are Dev A work, and each is called out so nobody discovers it in Week 4\.

&nbsp;

**6.1 The 30 s PSV cadence cannot drive a 4-minute experience.** PceOptions::cadence\_ms defaults to 30,000 (pce/include/pce/pce.h:24) — eight emissions in a whole session, and the regulate segment would get two. Both cadence\_ms and check\_interval\_ms are already settable through prism\_config without an ABI change (include/prism/prism\_core.h:102-105). **Run the demo at cadence\_ms \= 2000, check\_interval\_ms \= 50, significant\_delta \= 0.05.** Keep 30 s in the message contract's state cadence to the Quest if you like — that is a link-rate decision, separate from the inference rate. §0 keeps the \[default\] row for that reason.

&nbsp;

**6.2 Heart rate and HRV do not exist in the PCE.** No biometric input anywhere (pce/, psv/, core/). The ingress today is app switches, idle and task deadlines only. When adding the biometric contribution to fuse\_to\_psv: **gate its importance, not its confidence.** blend() computes confidence \= Σ(conf·imp)/Σ(imp) (pce/src/fusion.cpp:20-36), so the denominator moves for a contribution shipped at confidence 0 — measured drift on arousal confidence is **0.183**, five orders past the 1e-6 golden-trace bar. A contribution at importance 0.0 is **bit-identical** to today's output — verified. That one word is the difference between the golden traces passing and every fixture failing.

&nbsp;

**6.3 Authority does not exist in the engine.** It is in the plan's §11 message contract and in §0–§2 above, but there is no ABI for it and no consumer-side implementation. It is min(confidence, segment\_ceiling) and it needs a home; the natural one is a shared consumer-side layer both actuators read, since the same rule has to govern audio and visuals identically or the screen and the sound will disagree in front of a crowd.

&nbsp;

**6.4 Valence confidence is exactly 0.0, not "near floor".** pce/src/fusion.cpp:74 hardcodes {0.5, 0.0} — it never passes through blend() at all. So authority on valence is exactly zero for the whole session and the bar is pinned, not merely low. Design the spectator screen for a hard zero: a bar that never leaves the origin reads as broken unless the screen labels it. Label it.

&nbsp;

**6.5 There is no fade to silence on stop.** prism\_device\_stop (core/src/prism\_core.cpp:603-612) calls ma\_device\_uninit and nothing else. The PGAE consumption spec §9 requires "equal-power fade to silence over a short fixed interval; never a hard cut", and it is unimplemented. §2 RESOLVE's final 3 s depends on it. Without it, a four-minute experience ends on a click, about fifty times a day, in front of investors.

&nbsp;

**6.6 Scene changes are host-driven and blocking; mode\_hint selects nothing.** grep \-rn mode\_hint pgae/ returns zero hits — the consumption spec says mode\_hint selects the scene, but in this codebase scene selection is 100% a host act. The four segments are therefore four scene manifests, changed by prism\_crossfade\_scene, which **blocks for the decode** (include/prism/prism\_core.h:248) and must be called ahead of the boundary on a control thread, never from a frame loop. Pass align\_to\_loop\_boundary \= 0 for segment transitions — aligning costs up to one loop period, and with a 19 s bed loop that is a quarter of the baseline segment spent waiting.

&nbsp;

Also true and worth knowing before authoring: the engine is **mono float32 end to end, with no resampler** (core/src/prism\_core.cpp:588, pgae/src/scene.cpp:141). All four scene manifests must share one sample rate — **48 kHz** — or prism\_crossfade\_scene rejects the swap outright. There is no spatialisation in the core; if seated VR audio ever needs to be spatial, that happens outside it.

&nbsp;

**Loop lengths, chosen coprime so the four minutes never audibly repeat:** bed 19 s · sub 17 s · air 13 s · pulse 11 s · lead 7 s (lead unused in v1). Combined repeat period ≈ 90 hours. All are exact integer sample counts at 48 kHz.

&nbsp;

---

## Changelog

| Version | Date | Change |
| :---- | :---- | :---- |
| 1.0 | 7 Sep 2026 | Initial fill of the v1 template. §6 added: engine bindings and the six gaps between this script and the engine on main. |
| 1.1 | 11 Sep 2026 | §2 LOAD: eye control replaced by reticle control, since the Quest 3S has no eye tracking. The person selects by holding the reticle on the moving half, driven by head pose in VR or by pointer on the task screen, and the attendant line now matches. Added a note that the ramp values are provisional until Week E. The in-headset line is unchanged. §2 carries a note that the audio arc is unvalidated against the engine mapping and that regulate currently inverts. |
| 1.2 | 13 Sep 2026 | §2 BASELINE: the visual driver is the mean confidence of the three dimensions a pulse can inform, rescaled so the range a settled baseline actually reaches maps to the full visual travel, and a note on the end-of-baseline hold for HR\_base. §2 LOAD: the RMSSD criterion needs at least 20 clean differences in each 30 s window. §2 REGULATE: no extension without HR\_base. |
| 1.3 | 13 Sep 2026 | §0: hard cap 4:30 to 4:45, since the baseline hold and regulate's extension together reach 4:42. The 4:00 nominal is provisional until Week E, and any cut comes from baseline and resolve, never load or regulate. §2: the hold runs to the pulse boundary at 56 s. |
| 1.4 | 14 Sep 2026 | §0: the nominal is 4:11 from baseline start; the hard cap counts from the attendant's press, which arms a countdown of up to 11 s; the worst case from the press, 4:52, is over the cap and open. §2 BASELINE: entry is when the start button fires. §2 REGULATE: a timeout no longer picks close B. §2 RESOLVE: the trace is held through idle until the next baseline (prompt 3.5), so the attendant's "about twenty seconds" line is marked for a rewrite. §3: N is peaked at minus left at on the trace screen, never `drop_bpm`, and alone picks the close; the machine gives no verdict; both closes are marked for a rewrite, with the two measured mismatches. |
| 1.5 | 14 Sep 2026 | §0: the hard cap counts from when start fires, not from the press, correcting 1.4. The console's countdown is not part of the experience, so it does not count against the cap. The worst case is 56 + 75 + 105 + 45 = 281 s, 4:41, inside the cap, and nothing is cut. |
| 1.6 | 21 Sep 2026 | §3: peaked at is the load-only maximum, excluding baseline settling and later spikes. Sat down at remains the first reading; left at updates through resolve and freezes at session end. N subtracts the same displayed numbers, never `drop_bpm`; the screen gives no verdict. |
| 1.7 | 22 Sep 2026 | §4: ten frames now supplied; shared browser renderer and portable mapping. Horizon coordinates are top-origin, fixed light at (0.50, 0.40), resolve opens the sky. Replaced the unsupported 95-bpm photosensitivity assurance with measured software rails and explicit 45–180-bpm/hardware limits. §5 records the supplied frame deliverable. |
| 1.8 | 22 Sep 2026 | §4: visual-only amplitude taper from full authored amplitude at 95 bpm to zero at 120 bpm, including resolve and conservative per-beat cadence checks between state updates. No divided-rate substitute. Audio keeps every eligible scheduled beat; its primacy is why visuals give way. Retained mandatory 45–180-bpm pulse tests and whole-experience/headset safety limitations. |
| 1.9 | 23 Sep 2026 | §0 and spoken introduction/close distinguish delayed playback of measured heartbeat intervals from the surrounding composed sound. No instantaneous-heartbeat or whole-experience "not played back" claim; spectator disclosure remains visible. |

&nbsp;

&nbsp;
