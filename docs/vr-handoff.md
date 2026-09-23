# Prism Live: VR Handoff

**For:** whoever is taking the Unity and Quest work
**From:** Ridhwan
**Date:** 24 September 2026

You do not need to know anything about VR, Unity, or this project to start. This document assumes none of it.

---

# PART ONE: WHAT THIS IS

## 1. The company and the thing it makes

R13 Labs builds the **Prism Engine**. It reads a person's state and composes sound in response to it, in real time. Not a playlist, not a recording. The sound is generated while you sit there, and it changes because you changed.

Investors and universities find this hard to believe from a description. That is the problem this project solves.

## 2. What Prism Live is

A **four-minute seated experience**. One person at a time.

They put on a heart rate armband and a VR headset, sit in a chair, and the system reads their pulse and composes sound and visuals from it. Throughout, they hear their **own heartbeat** as a low thump, in real time. Fast at the start. Slow by the end.

At the end they see a line: their heart rate across the whole four minutes.

The point is that nobody has to take anything on faith. They felt it.

## 3. The four segments

| # | Segment | Length | What happens |
|---|---|---|---|
| 1 | **Baseline** | 45 s | They sit and look around. Nothing is asked of them. The system learns their normal |
| 2 | **Load** | 75 s | An attention task. It gets harder. Their heart rate goes up |
| 3 | **Regulate** | 75 s, can extend to 105 s | The task is over. The system works on them. Sound and visuals withdraw. Their heart rate comes down |
| 4 | **Resolve** | 45 s | It ends. Their heart rate trace appears |

**Segment 2 exists to raise their heart rate.** Without a rise, nothing can fall later and the whole demo proves nothing. Someone walking off a loud exhibition floor into a quiet dark chair will feel calmer regardless, and that proves nothing. The rise is what makes the fall real.

## 4. Where it gets shown

Exhibitions and university stalls. About 50 people go through the chair in a day, seven minutes each. Several hundred more walk past and watch a large screen showing what is happening to the person in the chair.

First internal showing: **15 October 2026**, 20 outside guests.

---

# PART TWO: YOUR JOB

## 5. The split

The system has two halves that barely talk to each other.

| Half | Who | What it does |
|---|---|---|
| **The laptop** | Ridhwan | Reads the armband, works out the person's state, drives the Prism Engine, produces all the audio, runs the big spectator screen |
| **The headset** | **You** | Draws what the person sees, and runs the attention task |

**There is no audio in your half.** All sound comes from the laptop through a wired cable into the person's headphones. You never touch it.

## 6. What connects the two

A **WebSocket** on a local router. The laptop sends you messages. You send it a few back. That is the entire interface.

The exact format is in `docs/message-contract-v1.md`, which you get with this document. **It is frozen.** If you need a field that is not there, ask Ridhwan. Do not invent one.

What arrives:

| Message | How often | What it carries |
|---|---|---|
| `state` | every 2 seconds | Four numbers about the person, their confidences, the current segment, elapsed time |
| `beat` | every heartbeat | **A time in the future at which to flash.** Not "now" |
| `clock` | on request | Lets you work out the offset between your clock and the laptop's |

What you send:

| Message | When |
|---|---|
| `task_event` | Every split, lock, miss and abandon in the attention task |
| `hello` | Once on connect |

## 7. What you do not need to know

Do not read up on any of this. It is not your half.

Bluetooth, heart rate variability, the maths that turns a pulse into four numbers, the Prism Engine, C++, audio DSP, the spectator screen.

You receive seven numbers and a segment name. You draw.

---

# PART THREE: START HERE

## 8. Week one has exactly one task

**Get a cube onto the Quest and look at it.** Nothing to do with this project.

That is the entire first task, and it is a hard gate. It sounds trivial. It is not, because the toolchain has many steps and any one of them can eat a day.

**Deadline: 20 September.** If it takes longer than a day or two, say so immediately. That is useful information, not failure.

### Step by step

Assume nothing is installed.

**1. Meta developer organisation.** Sign in at the Meta developer site with **the same Meta account the Quest is signed into**. Create an organisation. It may ask for phone or card verification; nothing is charged. Twenty minutes, and this is the step people get stuck on, so do it first.

**2. Enable developer mode.** In the Meta Quest mobile app, find your headset, and developer mode now appears as a toggle. It does not appear until step 1 is done.

**3. Install Unity Hub**, then a **Unity 6 LTS** version. When choosing modules, tick **Android Build Support** and both its sub-items, **OpenJDK** and **Android SDK & NDK Tools**. Missing those means you cannot build for Quest at all. Expect a long download.

**4. Install Meta Quest Developer Hub (MQDH).** It bundles adb, handles device pairing, casts the headset view to your monitor, and streams logs. Do not fight raw adb.

**5. New Unity project, Universal 3D (URP) template.** Not the built-in renderer, not HDRP.

**6. Add the Meta XR All-in-One SDK** from the Asset Store. It ships a project setup tool that flags misconfiguration. Use it and fix everything it lists.

**7. Project Settings**, in this order:
- Platform: **Android** (File > Build Settings > Switch Platform)
- Color Space: **Linear**
- Scripting Backend: **IL2CPP**
- Target Architectures: **ARM64 only**
- Texture Compression: **ASTC**
- Graphics API: **Vulkan**
- XR Plug-in Management > Android tab > enable the **Meta/Oculus** provider

**8. Scene:** drop in an `OVRCameraRig` prefab, a cube, and a floor plane.

**9. Plug the Quest in by USB-C.** Accept the trust prompt **inside the headset**, which is easy to miss because you cannot see it on your monitor.

**10. Build and Run.**

**Success = you see the cube and can look around it.**

### After that, before anything else

Set up **Quest Link**. It runs your Unity scene live on the headset from the editor, no build, no deploy. Turns a three-minute iteration into ten seconds. Over three weeks the difference is enormous. It is Windows only.

---

# PART FOUR: WHAT YOU BUILD

## 9. The visual field

One continuous world for the whole four minutes. It never cuts. It never changes location. Only its properties move.

**It is deliberately minimal:** one colour field, volumetric fog, a single diffuse light source, a horizon line, slow gradient shifts. No objects, no particles, no detail.

That is not a compromise from lack of time. Restraint reads as premium here, and spectacle done badly reads as a student project. Resist every instinct to add things.

### The seven numbers

Everything you draw is controlled by exactly seven values. **The ten reference frames now exist
at `docs/design/field-frames.html`**, with exact values beside each. This is a Claude Design bundle:
extract the component from the last JSON-escaped script block, never run the bundle.

**The working reference for the Unity scene is `web/shared/`.** Port `field_mapping.js` exactly
for state → seven values, `field_pulse.js` for beat scheduling/envelope, and the appearance and
linear-light rails of `field_renderer.js`. Both browser screens use `field_view.js` to connect
those pieces. `docs/field-reference.md` records the formulas, cross-dissolve policy, safety limits,
and all ten side-by-side comparisons. There is no React or browser build step.

| Token | Min | Max | What it is |
|---|---|---|---|
| Fog density | 0.18 | 0.62 | Unity exponential fog density |
| Light intensity | 0.35 | 1.10 | Multiplier on the single diffuse source |
| Hue | 18° | 216° | HSV hue |
| Saturation | 0.08 | 0.36 | HSV saturation |
| Horizon position | 0.38 | 0.62 | **Measured from the top:** 0.50 = eye level; larger values lower the horizon and expose more sky. Light stays at (0.50, 0.40). Current segment horizon ≥ 0.44; retuning above the light (y < 0.40) would put it underground |
| Pulse amplitude | 0.00 | 0.22 | Peak-to-peak luminance modulation, as a fraction of base field luminance |
| Field motion rate | 0.004 | 0.045 | Normalised units per second of gradient and fog drift |

### What drives them, per segment

`arousal`, `cognitive_load`, `confidence` and **host-computed `authority`** arrive in the `state`
message. `heartbeat` comes from scheduled `beat` messages. Load/regulate use
`effective = 0.5 + (psv - 0.5) × authority`, once; do not multiply by a second load ceiling or
compute authority from confidence. The ranges below are endpoints, not permission to force a
low-authority reading to an extreme. In regulate high arousal uses the bright 46° end and low
arousal the dim 24° end. Baseline alone reads confidence directly for its learning animation.

All pulse-amplitude ranges below are the **authored 62–95 bpm endpoints, before the visual-only
rate taper in §10**. Clamp the authored interpolation at those endpoints, then multiply by
`clamp((120 − hr_bpm) / 25, 0, 1)`: unchanged through 95, half at 107.5, zero from 120 bpm.
This applies to all four segments, including resolve and its additional final fade.

**Baseline** (45 s, then held to 56 s while the laptop waits for a heart rate figure)
- **baseline confidence**, 0.0 to 1.0, drives: fog density 0.38 → 0.26, light intensity 0.45 → 0.62, horizon 0.44 → 0.50
- compute it from each `state` message as `min(1.0, (confidence.arousal + confidence.cognitive_load + confidence.readiness) / 3 / 0.393)`
- leave valence out: its confidence is always exactly 0.0, so a mean over all four could never get past 0.75
- do not use `signal.baseline_quality` here: it is 0.0 through all of baseline and its hold, and takes the baseline result's value, if there is one, only once load begins. Nothing uses it during baseline
- 0.393 is the most those three reach during baseline. A settled person usually reaches it by the end of the 45 seconds; at slow heart rates, around 48 bpm, it tops out near 0.94
- it does not rise evenly: see the curve below
- Fixed: hue 208°, saturation 0.12, field motion 0.008
- heartbeat → pulse amplitude 0.06 → 0.10

Draw the baseline frames against this curve, the median of 400 synthetic baselines at 8 heart rates:

| Baseline t | 0 s | 10 s | 20 s | 25 s | 30 s | 35 s | 40 s | 45 s |
|---|---|---|---|---|---|---|---|---|
| Mean of the three confidences | 0.000 | 0.030 | 0.100 | 0.137 | 0.219 | 0.298 | 0.374 | 0.393 |
| Baseline confidence, after ÷ 0.393 | 0.00 | 0.08 | 0.25 | 0.35 | 0.56 | 0.76 | 0.95 | 1.00 |

A 5 s artefact burst 30 s into the baseline leaves it around 0.58 at 45 s, and a person still settling gently around 0.77. Both are real readings, not faults. Baseline runs on to 56 s while the laptop waits for a heart rate figure; the contract allows up to 12 s over. The value usually stays where it is then, rises after an artefact, and can dip slightly for someone still settling, so keep driving the field from it until load begins. The recorded fixture (§16) shows this curve, from a synthetic heart.

The world **resolves as the system learns them**. That is the idea: it starts vague and becomes clear as confidence builds.

**Load** (75 s)
- arousal → hue 214° → 202°, saturation 0.16 → 0.30, light intensity 0.70 → 1.05
- cognitive load → fog density 0.20 → 0.30, field motion 0.010 → 0.004
- Fixed: horizon 0.50
- heartbeat → pulse amplitude 0.10 → 0.16

Note the field **slows** as load rises. Less to fight.

**Regulate** (75 s, can extend to 105 s)
- arousal → hue 46° → 24°, light intensity 0.95 → 0.38, fog density 0.34 → 0.58, field motion 0.030 → 0.008
- cognitive load → saturation 0.30 → 0.14, horizon 0.56 → 0.44
- heartbeat → pulse amplitude 0.16 → 0.08

Within the authored 62–95 bpm range the visual pulse shrinks as heart rate falls. Above 95,
the safety taper takes precedence; do not infer a physiological direction from its brightness.

**Resolve** (45 s)
- Everything eases to fixed values and holds: hue 30°, saturation 0.14, fog density 0.44, light intensity 0.50, field motion 0.006
- At T−20 s to T−12 s: horizon 0.44 → 0.58, the field opening as their trace is drawn
- heartbeat → pulse amplitude 0.08 held, then to 0.00 across the final 3 seconds

### The hue rule, which matters

Baseline and load are blue, around 202° to 214°. Regulate and resolve are orange, around 24° to 46°.

**Within a segment, interpolate hue directly.** Every within-segment range is under 25°, so it stays in one colour family.

**Between segments, cross-dissolve between the two complete frames over 10 seconds. Never sweep the hue.** Sweeping from 202° to 46° passes through green and yellow, which looks like a bug. Dissolve everywhere, including the small changes, for consistency.

## 10. The heartbeat pulse

Every `beat` message carries `t_play`, a **time in the future**.

You do not flash when the message arrives. An eligible visual pulse is **scheduled** for
`t_play`, converted into your local clock using the offset from the `clock` exchange.

This is because the audio travels down a wire and arrives instantly, while your message crosses WiFi and does not. If you flash on arrival, the thump and the flash land at different moments, and the whole point is that they are the same event.

### The safety rail, which is not negotiable

- Peak-to-peak luminance modulation from the pulse **never exceeds 0.22** of base field luminance
- It is **never applied as a full-field flash**. It modulates the fog and the diffuse light source only
- **90 ms rise time**

**22 September correction:** 95 bpm is only an authored endpoint, not the highest possible rate.
The **audio heartbeat is the evidence the demo rests on; the visual pulse is secondary**.
Unity must apply the same visual-only gain as the browser:

`visualRateGain(hr_bpm) = clamp((120 − hr_bpm) / 25, 0, 1)`

Multiply the segment's authored amplitude by this gain: full through **95 bpm**, half at
**107.5 bpm**, **exactly zero at and above 120 bpm (2 Hz)**. Unknown HR gives no visual pulse.
Resolve uses the same taper and its final-three-second equal-power fade, multiplied together.
Do not hold amplitude above 95 or create every-Nth-beat flashes. The audio keeps following
every eligible scheduled beat at every supported rate, with its levels and ending unchanged;
only visuals yield so their cadence cannot approach 180 bpm / 3 Hz.

Port both protections, not just the state formula:

- `field_mapping.js` applies the taper from `state.hr_bpm`.
- `field_pulse.js` derives a conservative per-beat rate: the maximum of the beat's `hr_bpm`,
  `60000 / rr_ms` and `60000 / planned_onset_interval_ms`. Measure the interval from the last
  valid, non-rejected future candidate, **even if it was suppressed visually**, not from the
  last visible flash. A rate at least 120 bpm or interval of 500 ms or less cannot flash.
- `field_view.js` caps the mapped amplitude by that active beat's tapered segment amplitude
  using `min`, **not a second multiplication by the taper**. A two-second-old slow-rate state
  must not brighten a newly arrived fast beat. Late, rejected, duplicate and wrong-session
  events cannot flash; a suppressed visual event is never replayed and never suppresses audio.

Keep the **90 ms raised-cosine rise**, **180 ms fall**, non-additive envelope, fog/light-only
modulation, and the renderer's **0.22 relative / 0.09 absolute linear-luminance caps**, including
8-bit quantization and palette cross-dissolves. Mandatory tests span **45–180 bpm**: original
amplitudes through 95, the taper and midpoint, exact zero from 120 upwards, rate disagreement
and stale-state cases, plus the same rendered-pixel/90-ms-rise checks as prompt 3.4.

These software bounds replace the old unsupported assertion of being below *any*
photosensitivity threshold. They are **not headset, medical or whole-experience safety
certification**. Test Unity's actual rendered output and read the ambient-field limits in
`docs/known-limits.md`; headset optics, display behaviour and combined task/UI flashes remain
unverified.

## 11. The attention task

This is segment 2, and it is what raises their heart rate. The whole demo depends on it working.

### Important: the Quest 3S has no eye tracking

Only the much more expensive Quest Pro does. So "look at it" means **head gaze**: a ray fired forward from the centre of the head pose, a reticle where it lands, and a timer that fills while it stays on a target.

The person turns their **head**. Which is good, because that is physical effort, and physical effort raises heart rate.

### The mechanic

An object drifts across their view. It splits into two halves. **One half keeps moving, one stops.** They must hold the reticle on the moving half until it locks.

### The ramp, linear across 75 seconds

| Parameter | Start | End |
|---|---|---|
| Dwell to select | 900 ms | 420 ms |
| Interval between splits | 5.5 s | 2.2 s |
| Distractor halves after each split | 1 | 3 |
| Object angular speed | 6 °/s | 19 °/s |

**Miss penalty:** each miss brings the next split 0.4 s sooner, compounding, with a floor of 1.8 s.

**These numbers are provisional.** They were written assuming eye control, and head control is harder at the same values. They get tuned against real people in October. Build them as parameters you can change without recompiling.

### What you send back

A `task_event` message for every `split`, `lock`, `miss` and `abandon`, with dwell time, split interval, and a difficulty value from 0.0 at the start to 1.0 at the end. The laptop uses these to work out how hard the person is working.

### Text in the headset

There are only two lines of text in the entire four minutes.

- **3 seconds before the task starts**, one line, then it clears: *"Follow the half that's still moving."*
- **At T−20 s in resolve**, as the trace draws: *"This is your heart rate, from the moment you sat down."*

Nothing else. No menus, no HUD, no instructions, no progress bars. Everything else the person needs to know is said out loud by the attendant before the headset goes on.

---

# PART FIVE: CONSTRAINTS

## 12. 90 frames per second, held

That is **11.1 milliseconds per frame**, total, for both eyes.

Below it, people feel unwell. This is not a polish target, it is the floor.

### What kills it, in order

**1. Transparent overdraw.** The single biggest cause of dropped frames on Quest. Every transparent surface stacked on another costs fill rate. Volumetric fog and particles are both made of transparency, which is why the visual direction is so restrained.

**2. Real volumetric fog.** Properly raymarched volumetrics are too expensive here. Use Unity's exponential fog plus a small number of large soft elements. Fake it. It will look better at 90fps than the real thing at 45.

**3. Post-processing.** Bloom in particular. Do not use the post-processing stack.

### What to turn on

- **Single Pass Instanced** rendering. Quest draws everything twice, once per eye. This setting makes that one pass instead of two. Wrong setting roughly halves your frame rate
- **MSAA 4x**. Cheap on mobile tile-based GPUs and a large visual improvement
- **Fixed Foveated Rendering**. Renders the edges of your view at lower resolution, where you cannot see detail anyway

Profile with the Meta XR performance tools. Do not guess at what is slow.

## 13. The lens, which shapes the art

The Quest 3S uses **Fresnel lenses**, not the pancake optics in the Quest 3. Lower clarity, smaller sweet spot, and noticeably more **glare on small bright elements against near-black backgrounds**.

That is precisely what a dark ambient scene would otherwise be made of.

So:
- **No pinpoint bright sources on near-black**
- Favour mid-luminance fields, soft gradients, large diffuse light
- The light intensity floor is **0.35, not 0.0**, deliberately. The field never goes to true black, so there is never a bright element sitting on one

Design to the lens from the first frame. Do not build it beautiful and then fix the glare.

## 14. Do not build any of these

Every one is a deadline killer if it creeps back in.

- Controllers. The person's hands stay in their lap
- Hand tracking
- Any locomotion. The camera never moves. They are seated and stay seated
- Multiplayer or shared sessions
- Passthrough or mixed reality
- An app store build
- Particles, or any elaborate effects work
- Audio of any kind
- Menus, settings screens, HUD elements

## 15. Comfort

A seated experience with a camera that never moves is the safest VR content there is. Keep it that way.

- The camera never translates or rotates on its own. Ever
- The horizon stays stable
- Tracking origin at eye level, with a recentre when the session starts, because everyone sits at a different height

---

# PART SIX: HOW TO WORK

## 16. You are not blocked by the other half

You will be given **`fake_sender`**, a small program that replays a recorded session over the WebSocket exactly as the real laptop would. Beats, states, segments, all of it.

Build everything against that. You do not need the armband, the laptop, the Prism Engine, or Ridhwan's code. The two halves meet once, near the end.

**The recorded session's heart is synthetic.** Since prompt 2.4 every value in it is real output of the laptop side: beats, segments, `psv`, `confidence` and `authority`. The heart it reads is a generated one, though, with no breathing and no real person behind it. Build against the stream's shape and timing: the segment sequence, the 2-second state cadence, beats scheduled ahead at `t_play`, and baseline and regulate both running past their nominal lengths. Do not tune anything to its numbers. A session recorded from the armband will follow.

## 17. Threading, which will catch you

**Unity's API is not thread safe.** Your WebSocket runs on a background thread. If you touch a Transform, a Material or anything else from it, Unity crashes in ways that look random.

The pattern: receive on the background thread, push into a `ConcurrentQueue`, drain the queue in `Update()` on the main thread. Do this from the first line of networking code, not after the first crash.

For the WebSocket itself, `NativeWebSocket` is the usual choice and works on Quest.

## 18. Never decide anything

The laptop owns segment, timing and authority. You display what you are told.

Specifically:
- Take segment and progress from `segment`, `segment_elapsed_ms` and `segment_nominal_ms` in the `state` message. **Never from a local timer.** Regulate is adaptive and can run 30 seconds over, and baseline can run 12 seconds over, so a local clock will be wrong. Idle has no length and sends 0 for both elapsed and nominal, so draw no progress there; reset lasts 20 seconds after a full session and 3 seconds after a stop or a failed baseline gate
- If the connection drops, **freeze on the last known state** and show a visible marker. Do not improvise, do not carry on, do not guess

## 19. Structure the code so the field is portable

Keep "apply seven numbers to the world" separate from "get numbers from the network" and separate again from "run the task."

There is a real chance the visual field also has to run in a browser as a fallback. Clean separation makes that a port rather than a rewrite.

---

# PART SEVEN: DATES AND HONESTY

## 20. The gates

| Date | Gate |
|---|---|
| **20 September** | Cube running on the Quest. Hard gate |
| **30 September** | Decision: is VR in the 15 October showing at all? |
| **7 October** | Scene responding to `fake_sender`, task working, 90fps held |
| **14 October** | Full dry run with the complete booth setup |
| **15 October** | Showing, 20 outside guests |

## 21. You should know this before you accept

**VR is currently a stretch goal, not a commitment.**

The audio half is the demo. A person hearing their own heartbeat rise and fall is the thing nobody else has, and it works without a headset. If the audio half is not finished by 30 September, or if you are not far enough along, **VR gets cut and the demo ships as a seated experience with the visuals on a monitor.**

That is not a judgement on you. It is written into the project plan, decided in advance, precisely so nobody has to make it emotionally at 11pm on 12 October.

So: work at a pace you can sustain, tell the truth about where you are, and do not quietly fall behind hoping to catch up. An honest "I am stuck" on 25 September is worth far more than a heroic effort on the 13th.

## 22. What you need from Ridhwan

Chase these. You cannot start some of them without.

| Thing | For |
|---|---|
| **The ten reference frames plus their seven token values — supplied** | `docs/design/field-frames.html`; extract the component, do not run the bundle. `web/shared/` is the working reference for the Unity scene; mapping and comparisons in `docs/field-reference.md` |
| `docs/message-contract-v1.md` | Comes with this document |
| `tools/fake_sender.py` | Your development input |
| The Quest 3S | Obviously |
| Access to the Meta developer org, under R13 Labs | Developer mode |
| Experience Script §2 and §4 | The full segment detail and token sheet |
| A signed IP assignment, if you are not employed by R13 | Before you write any code |

## 23. Things that will bite you, listed in advance

1. **The Meta developer org.** Twenty minutes if it goes well, a day if verification is awkward. Do it first
2. **Missing Android Build Support modules** in the Unity install. You will not find out until you try to build
3. **Forgetting to accept the trust prompt inside the headset.** It is invisible from your monitor
4. **Single Pass Instanced off.** Everything renders twice, frame rate halves, and it looks like a performance problem in your scene
5. **Touching Unity objects from the network thread.** Random crashes with no useful error
6. **Building volumetric fog properly.** Too expensive. Fake it
7. **Using a local timer for segment progress.** Works perfectly in testing, breaks on any session where regulate extends
8. **Flashing on beat arrival instead of at `t_play`.** Looks fine alone, wrong the moment it is next to the audio
9. **Assuming eye tracking.** The 3S does not have it
10. **Polishing the visuals before the frame rate holds.** Get to 90fps with an ugly scene first, then make it beautiful

---

## In one paragraph

You are building a Unity app for a Quest 3S that shows one person a slowly changing abstract field, controlled by seven numbers arriving over a WebSocket from a laptop, plus a 75-second attention task they play by turning their head. It must hold 90fps, it must flash in time with a heartbeat at a scheduled moment, and it must never decide anything for itself. The first thing you do is get a cube onto the headset, and everything else waits until that works.
