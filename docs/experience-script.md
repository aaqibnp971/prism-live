# **Prism Live: Experience Script v1.1**

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
| Total target duration | 4:00 nominal, 4:30 hard cap |
| PSV emission cadence | 30 s nominal — **but see §6.1**, the demo runs it at 2 s |
| Baseline window | 45 s |
| Authority rule | authority \= min(confidence, segment\_ceiling), per dimension, no exceptions |
| Valence policy | Confidence sits at floor for the whole session. Deliberate, not a bug, and the thing the spectator screen exists to show. **In the current engine it is exactly 0.0, not "near" — design the bar for a hard zero (§6.4).** |
| **Audio ceiling** | **True-peak −1.0 dBTP**, enforced by the engine's master limiter as the last stage, unbypassable. **Integrated programme loudness −16 LUFS** across the nominal 4:00. **Short-term (3 s) never above −11 LUFS** anywhere in the session. |
| **Loudness you tune at** | **Flat reference monitoring only** — open-back studio headphones or nearfields. Author and balance there, then *verify* on the XM5 with ANC on. **Never tune on the XM5**: its bass shelf flatters the per-beat layer, and anything balanced on it will be written too quiet in the sub and will vanish on reference. Attendant sets headphone volume once at setup, marks it on the laptop, and never adjusts it per person. |
| Frame rate target | 90 fps |
| Claims tier | Marketing. Describe what the system does, never what it achieves. |

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

### Segment 1 — BASELINE

| Field | Value |
| :---- | :---- |
| Duration | 45 s, fixed |
| Entry | Attendant presses start |
| Exit | 45 s elapsed |
| Adaptive extension | None |
| Timeout branch | N/A |
| Authority ceilings | arousal 0.0 · valence 0.0 · cognitive\_load 0.0 · readiness 0.0. The engine observes and does not act. |
| Regulation target | None |
| Success threshold | Not a success gate. **Quality gate:** ≥ 35 of 45 s of clean beat data, and ≥ 30 accepted RR intervals, or the attendant re-seats the armband and restarts. |
| **Audio** | **Material:** two stems only — bed (sustained pad, D minor, root D3 \= 146.8 Hz, 19 s loop) and sub (drone, D2 \= 73.4 Hz, high-passed 62 Hz, 17 s loop). pulse, lead, air closed. No rhythmic content, no melodic foreground, no transients above the heartbeat. **Driven by:** nothing from the PSV — authority is 0\. The only movement in 45 s is the person's own pulse. Master LP cutoff fixed at **1,400 Hz**. **Dynamic range:** −22 → −17 LUFS short-term. |
| Per-beat layer | **Active.** This is where they first hear themselves. **Amplitude:** −18 → −13 dBFS peak, ramped up across the first 12 s so the first beat is not a surprise. **Frequency band:** 44 Hz fundamental, energy confined 36–62 Hz, roll-off 24 dB/oct above 120 Hz. Envelope 8 ms attack, decay min(220 ms, 0.55 × current RR interval) so beats never overlap. |
| **Visual base frame** | Baseline base. **Driven axes:** mean\_confidence (0.0 → 1.0, across all four dimensions) → **fog density 0.38 → 0.26**, **light intensity 0.45 → 0.62**, **horizon position 0.44 → 0.50**. The world resolves as the system learns them — the in-headset counterpart of the confidence bars climbing on the spectator screen. Hue fixed **208°**, saturation fixed **0.12**, field motion rate fixed **0.008**. heartbeat → **pulse amplitude 0.06 → 0.10**. |
| Person does | Sits, looks around. Nothing asked of them. |
| **Person is told** | *(attendant, before the headset goes on)* — **"Sit however you're comfortable. For the first minute you don't have to do anything at all. Just look around. It's learning what your normal looks like, so normal is exactly what we want."** *(No in-headset text.)* |
| Spectator foreground | Confidence bars climbing from zero as the baseline fills. |

&nbsp;

The narrative job of this segment is that the crowd watches confidence build in real time. That is the most legible thing on the screen all session and it happens in the first 45 s.

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
| **Success threshold** | HR\_base \= mean HR over the final 30 s of BASELINE. HR\_load \= mean HR over the final 30 s of LOAD. **Activated when HR\_load ≥ HR\_base \+ 6 bpm.** Secondary, either alone also counts: **RMSSD over the final 30 s ≤ 0.80 × baseline RMSSD.** Recorded per run; drives the Week 5 tuning in plan task 5.3. |
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
| Adaptive extension | Up to **\+30 s** if the threshold has not been met. Hard cap, protects booth throughput. |
| Timeout branch | Proceed to resolve anyway. The trace reports honestly. **Close B is used.** |
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

&nbsp;

---

## 3\. The closes

Four variants, one spine. Only Beat 1 changes.

### Beat 1 — name what just happened to them. 15 s.

|  | It moved | It did not move |
| :---- | :---- | :---- |
| **Investor** | "Your rate came down about **\[N\]** beats from where the task put it. Nobody scripted that — the system read the change while it was happening and kept adjusting to it. What's on the screen is the record, not a rendering of one." | "Your trace is close to flat, and the system said so. It never claimed a change it couldn't measure — you can see the confidence on that dimension stayed where it was. That's the behaviour we build for. In a car or a classroom, a system that overstates what it's reading is worse than no system at all." |
| **Student** | "That line's your heart rate. It went up when the task got hard, and it came back down after. The sound you were hearing was following that line the whole time — it isn't a playlist, there's no recording of it anywhere." | "Your line's pretty flat, which happens plenty. That's the honest result and the system reported it — it didn't get much purchase on you today and it didn't pretend otherwise. The interesting part is that it knew." |

&nbsp;

**\[N\]** is read live off the trace screen. If the drop is under 3 bpm, use the did-not-move close regardless of what the threshold logic decided.

### Beat 2 — name what the system did. 20 s, one version, same for everyone.

> "What you heard was composed while you sat there, not played back. The engine reads your state as four numbers — how activated you are, which way it's leaning, how much you're carrying, what you can take on — and every one of those numbers carries its own confidence. It's only allowed to push a number as hard as it's confident about it. That's why the second bar sat at zero the whole time you were in the chair: it couldn't read that one, so it left it alone. Same four numbers drive sound, haptics and interface — this room only had the sound."

### Beat 3 — the handoff. 10 s, one version.

> "There's a QR on this card. Two things behind it: the study app this engine runs inside, and Prism Venues, which is the same engine reading a room instead of a person. If you want the technical version rather than the demo version, my email's on there too."

&nbsp;

**The "did not move" versions were written first, on purpose.** They are the ones that will save an attendant standing in front of a flat trace with nothing to say. Read both out loud before Week 5\. Neither should sound like an apology.

&nbsp;

---

## 4\. Frame and token sheet

The Claude Design deliverable. Endpoint frames Dev B interpolates between — not storyboards.

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
| Horizon position | **0.38** | **0.62** | normalised vertical field position; **0.50 \= eye level** |
| Pulse amplitude | **0.00** | **0.22** | peak-to-peak luminance modulation as a fraction of base field luminance |
| Field motion rate | **0.004** | **0.045** | normalised units·s⁻¹ of gradient/fog drift |

&nbsp;

**Hue interpolation rule.** *Within* a segment, interpolate hue directly — every within-segment range above spans less than 25°, so it stays in one colour family. *Between* segments, **cross-dissolve between the two base frames over 10 s. Never sweep hue between segments.** Baseline→Load is 208°→214° and could be swept, but Load→Regulate is 202°→46° and sweeping it passes through green and yellow, which looks like a bug. Dissolve everywhere, for consistency.

&nbsp;

**Flash and modulation rail (hard, applies to every frame and every driven axis).** Peak-to-peak luminance modulation from the per-beat layer never exceeds **0.22** of base field luminance, and is never applied as a full-field flash — it modulates the fog and the diffuse source only, with a 90 ms rise. At the top of the design range (95 bpm ≈ 1.6 Hz) this stays well under any photosensitivity threshold, and it must stay there if the pulse mapping is ever retuned. This rail is not a style choice.

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
5. **All ten frames exist in Figma with the token sheet beside them** — *outstanding, Week 1 task 1.13.*  
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

&nbsp;

&nbsp;