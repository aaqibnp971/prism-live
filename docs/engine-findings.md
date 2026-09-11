# Engine findings

**Written:** 11 September 2026, for prompt 0.4 in `docs/all-prompts.md`.
**Read-only.** Nothing in `D:\ANP\prism-core` was changed. Upstream was read from a separate fresh clone.
**Checked how:** each answer was investigated from source on every revision below, then checked by two adversarial verifiers, one re-opening every quoted line and one trying to refute the answer. Nothing here has been run; runtime checks are listed where they matter.

---

## Which engine these answers are about

| Revision | Where it is | What it is |
|---|---|---|
| **`acbfd50`** | Upstream `RidhwanAhamed/prism-core`. Both `main` and `venues/win32-build` point here. 2 Aug 2026. `prism_version()` returns `"0.3.0"` | **The answers below are for this revision.** It contains both revisions below plus 6 newer commits, including `prism_crossfade_scene`. Every file:line citation in experience script §6 matches it |
| `a8ad35f` | The `D:\ANP\prism-core` checkout, fork branch `venues/win32-build`. 30 Jul 2026. `"0.2.0"` | The only engine library built on this machine (`build/shared/core/libprism_core.dll`) came from here. No `prism_crossfade_scene`. Has a PSV data race that upstream fixed in `dbf0f24` |
| `54ff0d2` | `D:\ANP\prism-core` local `main`. 15 Jul 2026 | Stale. No way at all for a host to supply a PSV |

Two consequences:

- **No tags exist on either remote**, so "pinned to a tag" (CLAUDE.md hard rule 1) is not possible yet. Pin to commit `acbfd50`, or ask the engine owner to tag it.
- **The DLL on this machine can't be used for the demo.** A library has to be built from `acbfd50`. The `D:\ANP\prism-core` clone has never been fetched.

---

## Q1. Is there a host-settable master output gain? **No.**

CLAUDE.md open question 1, prompt 0.4 item 1.

- The C ABI has no gain, volume, level or fade. `prism_config` (`include/prism/prism_core.h:95-108`) holds only `tz_offset_min`, `vertical`, `cadence_ms`, `check_interval_ms` and `significant_delta`. `prism_scene_swap` (`:232-246`) has no gain either.
- The engine's master gain is an internal constant, `PgaeOptions::master_gain = 0.7` (`pgae/include/pgae/engine.h:39`). The only master ramp is a 0.5 s fade-in at start (`engine.h:40`, `pgae/src/engine.cpp:269-275`).
- Stop is a hard cut. `prism_device_stop` is `ma_device_uninit` and nothing else (`core/src/prism_core.cpp:603-612`), and `prism_stop` calls it first (`:382-403`). The engine's own spec requires a shutdown fade (`docs/prism-pgae-consumption-spec.md:138`); it is not implemented.
- The mood override can't fade. The header says "It is NOT a volume or scene control" (`prism_core.h:182`), and bed and sub are always on (`pgae/src/mapping.cpp:69-70`).

**What works without an engine change: the pull model.** The header documents two ways to get sound, "mutually exclusive per handle" (`prism_core.h:266-271`). In the pull model the host owns the audio device and calls `prism_render` from its own callback. The host multiplies each buffer by its own gain ramp, then stops its own stream. `prism_device_start` and `prism_device_stop` are never called. This works on `acbfd50` and on `a8ad35f`.

The host gain stage is needed for more than shutdown. Experience script §2 RESOLVE tapers bed, sub and air to silence across T−22 s → T−10 s, leaving the heartbeat alone. No PSV can do that: bed and sub are always on with gains of at least 0.3 and 0.2 (`mapping.cpp:57-60`). The ending depends on the host's gain.

An `acbfd50`-only alternative is `prism_crossfade_scene` to a manifest whose `default_scene` holds one stem of digital silence, with `crossfade_ms = 3000`. An empty scene is rejected. It is not worth it: the host needs the pull model anyway (Q4).

**Plans this changes:** prompt 2.5 says to "ramp the master gain to zero over 3 seconds, then call prism_device_stop", and to stop and ask if no host-settable gain exists. None exists. Prompt 2.5 needs rewriting around the pull model. Solo plan A.7 (a fade inside `prism_device_stop`) is the engine change hard rule 1 forbids, and is not needed.

---

## Q2. Does `prism_crossfade_scene` preserve loop phase for an identical stem? **No.**

CLAUDE.md open question 2, prompt 0.4 item 2.

- The call takes a manifest path and always plays that manifest's `default_scene` (`prism_core.h:232-236`). It decodes every stem into new buffers, including stems byte-identical to ones already playing. Nothing checks for identical stems.
- The incoming scene gets its own deck, and its read position is set to zero unconditionally: `deck.phase = 0;` (`pgae/src/engine.cpp:57`). Every stem is read at `deck.phase % stem.loop_frames` (`engine.cpp:219-223`). The engine documents this as intended: "an incoming scene starts its loops from their beginning, part-way through the outgoing scene's" (`engine.h:127-133`).
- The two decks are summed with equal-power gains over `crossfade_ms` (`engine.cpp:248-262`).

**What the listener hears, with `align_to_loop_boundary = 0`** (what CLAUDE.md prescribes): two copies of the same bed at different offsets, summed for the length of the crossfade. Short offsets comb-filter, long ones sound doubled. That is the seam the experience script says must not be heard, at every segment boundary.

**With `align_to_loop_boundary = 1`**, the overlap starts on the outgoing bed's next loop boundary (`engine.cpp:100-112`), so the bed lines up, after a wait of up to one full 19 s loop. The 17 s sub still restarts out of phase. And where material does line up, equal-power gains add in amplitude, swelling about +3 dB at the midpoint; equal-power holds constant power only for uncorrelated material (`pgae/include/pgae/fade.h:11-24`).

Two more effects of any scene change:

- The incoming deck starts with pulse, lead and air closed and bed and sub at a default level (`engine.cpp:12`, `:64-68`). It does not inherit the PSV steering the outgoing deck. Only a PSV published after the swap is serviced reaches it, and gated stems then open only at their own first loop boundary plus 1.5 s.
- The engine's tests never check phase. The ABI test crossfades a manifest to itself and checks return codes; the render tests use constant stems (`tests/core/abi_test.cpp:288-290`, `tests/pgae/render_test.cpp:180-181`). The engine's own stems are all exactly 16.000 s (commit `ab7b381`), so coprime loops were never exercised.

**Decided 11 September: option 1, one scene, no crossfades.** The options that were considered, none needing an engine change:

1. **One scene for the whole session.** Never crossfade. All four stems stay on one deck with continuous coprime phase, and segments differ only through the PSV. Trade-off: no segment-specific stem sets, and a neutral PSV opens the pulse stem (see "Authority 0 is not silence"), so baseline can't be bed and sub only.
2. **The host plays bed and sub itself.** In the pull model the host loops bed and sub in its own callback, where it controls phase. Engine scenes carry only pulse and air, where a restart is a musical choice. Trade-off: more real-time code in the host's native audio shim.
3. **`align_to_loop_boundary = 1`, with every shared stem's length equal to or dividing the bed's.** Trade-off: loses the 19/17 coprime non-repetition, transitions wait up to one bed loop, and the +3 dB swell remains.

Making the crossfade itself phase-preserving would be an engine change.

---

## Q3. How long does `prism_crossfade_scene` block? **Moot since 11 September.**

CLAUDE.md open question 3. It is never called (decision 1 below). The only blocking call left is `prism_load_scene`, once per handle at startup; prompt 2.7 measures that. What follows is kept for the record.

From the code: the call blocks while it reads the manifest, parses the JSON and decodes every stem of the new scene, identical stems included (`core/src/prism_core.cpp:520-549`). It returns `PRISM_ERROR_BUSY` while a crossfade is in flight (`prism_core.h:257`). An armed swap waits until something renders, and `prism_stop` drops one that was never consumed (`:252-254`).

**One correction to the plan.** With `align_to_loop_boundary = 0` the overlap starts at the next render block after the decode finishes (`pgae/include/pgae/swap.h:33-37`). The host doesn't pick the start; it lands wherever the decode ends. CLAUDE.md's "set the pre-schedule lead to three times that" therefore starts the crossfade early, by roughly twice the decode time. Size the lead from the measurement to put the start where it should be.

---

## Q4. Can the host inject a per-beat audio event into the engine's output? **No.**

CLAUDE.md open question 4, prompt 0.4 item 3.

- There is no side-chain, audio input, trigger, one-shot or host-fed buffer. `prism_render` is output-only (`prism_core.h:276-279`). The five stem roles all loop, and gated stems switch only at loop boundaries with 1.5 s fades (`engine.h:37`). The built-in device is playback-only and ignores input.
- The chain, in order: stems → per-stem level → sum → master low-pass → master gain 0.7 → peak limiter → out (`engine.cpp:269-275`).

**What works without an engine change: mix in the host's own callback.** One device, one clock. In each callback the host calls `prism_render` into its buffer and adds the `heartbeat_layer` samples, placed from `t_play`. The heartbeat then sits after the engine's low-pass, which can't touch it, and after its limiter.

What the host now owns:

- **The ceiling.** The engine buffer's sample peaks reach up to −3 dBFS (0.708 linear). −1.0 dBTP is 0.891. With no trim on the engine, a heartbeat peaking above 0.183 (−14.7 dBFS) can breach the ceiling when peaks coincide. The experience script asks for −13 to −9 dBFS. The host needs a static trim on the engine buffer, its own true-peak limiter as the final stage, or both.
- **36–62 Hz.** The engine doesn't keep that band clear: the spec's "prohibited frequency bands" rail (`docs/prism-pgae-consumption-spec.md:124`) is not implemented. The host can high-pass or notch the engine buffer across 36–62 Hz before adding the heartbeat, which enforces hard rule 2 in code instead of trusting the stems.

A second audio device just for the heartbeat also needs no engine change. It isn't recommended: two devices drift on separate clocks, the built-in device always opens the default endpoint, and the host never sees the engine's samples.

**The audio callback can't be Python.** CPython can't guarantee no allocation and no GIL inside a callback, which breaks hard rule 4. The callback needs a small native (C or C++) shim in prism-live. That is a host change, not an engine change, and it is the case CLAUDE.md anticipates: "Drop to C++ only where the audio path genuinely requires it."

---

## Prompt 0.4 item 4: the cadence field names

In `prism_config`, `include/prism/prism_core.h` on `acbfd50`:

| Line | Field | Type | 0 means |
|---|---|---|---|
| 103 | `cadence_ms` | `int64_t` | default, 30000 |
| 105 | `check_interval_ms` | `int64_t` | default, 5000 |
| 107 | `significant_delta` | `double` | default, 0.1 |

The struct is identical on `a8ad35f`, so one ctypes definition fits both. Set 2000, 50 and 0.05 explicitly. If the host ends up as the only PSV writer (below), these settings stop mattering.

---

## Also found: how the PSV gets in

Not a prompt 0.4 question, but the architecture depends on it.

CLAUDE.md says the bridge "hands [the four PSV values and their confidences] across the PSV interface to the engine's actuation input". **That can't be done literally.**

- The only host PSV input is `prism_set_mood_override` (`prism_core.h:177-220`). It takes four values and one confidence, "Applied to all four dimensions" (`:206`). No ABI call takes per-dimension confidences. Local `main` (`54ff0d2`) has no PSV input at all.
- The audio engine reads confidence only as `effective = 0.5 + (value − 0.5) × confidence` (`pgae/src/mapping.cpp:24-34`). So the bridge can reproduce per-dimension authority exactly: send each value as `0.5 + (v − 0.5) × authority`, valence as 0.5, and confidence 1.0. The engine's own `prism_get_psv` readout then shows confidence 1.0 everywhere, so nothing should read authority back from the engine. Authority comes from the host's `state` message, as the contract already says.
- The override replaces the PCE's output rather than blending with it, and takes effect on return (`prism_core.h:197-198`, `:210-213`).
- **Keep one writer.** On `a8ad35f`, the host's override and the engine's inference thread can publish at once, a race that `dbf0f24` fixed upstream (its commit message: "not a click, a quietly wrong room"). Even on `acbfd50` it is simplest for the host to be the only PSV writer. The code shows `prism_render` needs only a loaded scene, not `prism_start`, so one way is never to start inference. Verify in prompt 2.5. A pin set before `prism_start` also reuses sequence number 1 when `prism_start` publishes.
- Call it from one control thread, never the audio callback: it takes mutexes and can allocate.
- The override was designed for occasional manual pins in Prism Venues (`:178-180`). Driving it every 2 s is untested. The engine's own CLAUDE.md also puts confidence computation inside `pce/` and has hosts forward raw events only, while prism-live computes the PSV itself. Worth raising with the engine owner; it doesn't block the demo.

## Also found: authority 0 is not silence

The plan treats authority 0 as "the engine observes and does not act". Through the mapping, authority 0 is a neutral PSV, every effective value 0.5, and that still makes sound. Density is 0.5 (`mapping.cpp:50`), which opens the pulse stem (active at 0.35 or more, `:71`). Bed and sub are always on; air stays closed below 0.55 (`:72`). So in baseline the pulse stem plays, unless the baseline scene has no pulse stem. Under Q2 option 1 there is only one scene, so baseline can't be bed and sub only.

Gated stems also change only at their own loop boundary, up to 11 s for pulse and 13 s for air, plus a 1.5 s fade. Updates every 2 s that keep crossing a threshold (0.35, 0.55, 0.72) will keep rescheduling that fade, so the host should add hysteresis near them.

**Not yet checked:** whether the experience script's per-segment numbers are reachable at all, for example the low-pass sweeping 1,400 → 3,600 Hz in load. The engine turns the PSV into cutoff and gains through its own fixed curves, ported verbatim from the probe (`mapping.cpp`). The host can't set them directly.

---

## The mapping: what one scene can reach

Added 11 September 2026, after the decision to use one scene. From reading `pgae/src/mapping.cpp`, `pgae/src/engine.cpp` and `psv/include/psv/rt.h` on `acbfd50`, checked by an independent derivation and two adversarial verifiers. Nothing has been run or listened to.

### The mapping

With `a`, `l`, `r` = effective arousal, cognitive_load, readiness minus 0.5 (`mapping.cpp:39-76`):

| Output | Formula | Notes |
|---|---|---|
| brightness | `clamp01(0.55 + 0.9a − 0.8l)` | cutoff = 300 × 40^brightness, 300 Hz to 12 kHz, log scale |
| density | `clamp01(0.5 + 0.9a − 1.0l)` | one number, gates every optional stem |
| bed gain | `clamp01(0.6 + 0.6l)` | always on; floor 0.3 |
| sub gain | `clamp01(0.5 − 0.6r)` | always on; floor 0.2 |
| pulse gain | `clamp01(0.5 + 0.7a − 0.6l)` | sounds only if density ≥ 0.35 |
| air gain | `clamp01(0.5 + 0.5a − 0.6l)` | sounds only if density ≥ 0.55 |

Level amplitude = 0.4 × gain^1.5 (`engine.cpp:18-21`), so a gain change g1 → g2 is 30·log10(g2/g1) dB. Valence and `mode_hint` are never read. Nothing but the PSV moves the cutoff or the gains inside a scene: `PgaeOptions` is default-constructed and unreachable from the ABI, the manifest `key` is parsed and never read, and `prism_config` only touches inference. Levels smooth with a 0.25 s one-pole (fully settled inside a 2 s update); the cutoff with 0.6 s (about 96% of the way by the next update). Before any PSV arrives the cutoff sits at 2,400 Hz (`engine.h:41`) with pulse and air closed (`engine.cpp:12`).

### The gates are nested, and tied to the filter

Air sounds only at density ≥ 0.55 and pulse at ≥ 0.35, on the same density number. **Air can never sound while pulse is closed.** And since brightness − density = 0.05 + 0.2·(load − 0.5), the gates follow the cutoff:

| Cutoff | Pulse | Air |
|---|---|---|
| below 907 Hz | forced off | forced off |
| 907 – 1,897 Hz | off only if the load input is high enough | forced off |
| 1,897 – 3,968 Hz | forced on | on only if the load input is low enough |
| above 3,968 Hz | forced on | forced on |

Pulse's gain is set by the cutoff to within ±0.011 (pulse = 0.5 + 0.78·(brightness − 0.55) + 0.02·l), so pulse level cannot move independently of the filter. While its gate is open, pulse gain never drops below 0.33 and air gain never below 0.51: no stem can be faded to nothing through its gain. A stem leaves through its gate, in one fixed 1.5 s fade.

### The filter points the script uses

| Cutoff | Brightness | Gates at that cutoff |
|---|---|---|
| 1,400 Hz | 0.418 | pulse closed only if the load input > 0.588; air forced off |
| 3,600 Hz | 0.674 | pulse forced on; air on if the load input ≤ 0.868 |
| 620 Hz | 0.197 | both forced off |
| 900 Hz | 0.298 | both forced off, by 0.002 at load input 0 |
| neutral PSV | 0.55 → 2,282 Hz | pulse on, air off |

### Per segment: what some PSV can reach, and what none can

Values are effective arousal / load / readiness. The load input is held at 0.65 through baseline and load so bed does not step at that boundary; the price is a 0.012 margin under the pulse gate in baseline.

**Baseline. Reachable.** 0.486 / 0.65 / 0.50 gives 1,400 Hz, density 0.338, pulse and air closed, bed 0.69. Pulse is closed by any PSV with density below 0.35: a load input ≥ 0.65 alone, or arousal ≤ 0.33 alone. No PSV near neutral does it, and pulse's gain cannot close it. Cost: bed at least 1.1 dB above its neutral level (2.1 dB at this pose). If the first PSV after loading is this one, pulse and air never open at all.

**Load. Mostly reachable.** Load 0.65, arousal 0.486 → 0.771 sweeps 1,400 → 3,600 Hz; pulse opens at arousal 0.50 (about 1,470 Hz) and air at 0.722 (about 3,070 Hz); bed and sub unchanged. Unreachable: pulse rising 8 dB (at most +5.8, because its gain follows the cutoff); air falling after it opens (it rises with the cutoff); "transient density down" (there is no such parameter).

**Regulate. Partly reachable.** From the load end, move to 0.506 / 0.948 / 0.11: 620 Hz, both gates closed, bed +3.0 dB and sub +5.0 dB against the load end. Along the way air closes first (density < 0.55), then pulse (density < 0.35, at a cutoff of about 1,600 Hz). Unreachable: air still sounding while pulse closes (nested gates); air falling 10 dB while audible (1 to 3 dB at most, then the 1.5 s gate-out); an 18 s pulse fade (about 4 dB of gain glide, then a 1.5 s gate-out at an 11 s boundary; pulse gone also forces the cutoff below 1,897 Hz by then); bed and sub thickening in the script's own directions, because bed rises only when the load input rises and sub only when readiness falls.

**Resolve. Partly reachable.** Arousal 0.506 → 0.618 with load 0.948 and readiness 0.11 sweeps 620 → 900 Hz with bed and sub held and both gates closed. Unreachable: the 10 s air tail. Air is forced off below 1,897 Hz; the most it can do is a close left pending from regulate, which holds until air's next 13 s boundary and then fades in 1.5 s. Bed and sub cannot be silenced through any PSV; that is the host's session gain.

### At the script's own regulate targets, regulate comes out inverted

Arousal 0.32, cognitive_load 0.28, readiness 0.62 gives brightness 0.564 → **2,403 Hz**, density 0.558 → **pulse on and air on**, bed 0.47 and sub 0.43, both **thinner** than neutral. The script asks for 620 Hz, everything subtracted, bed and sub thicker. The body's own directions push the mapping the wrong way: falling load thins the bed and rising readiness thins the sub.

Under the authority plan as first written, no segment follows the script: baseline is a neutral PSV, 2,282 Hz with pulse opening at its first 11 s boundary; load at a 0.20 ceiling moves the cutoff about 50 Hz when arousal and load rise together and air never opens (the whole reachable span at 0.20 is 1,219 to 4,272 Hz, only with arousal and load at opposite extremes); regulate ends inverted as above; resolve tapers back to 2,282 Hz with pulse on. Whether the engine is fed body-derived values or a designed pose per segment is decided by listening in Week B (decision 3).

### Gate timing

A gate change is scheduled at the stem's next loop boundary counted from scene load, strictly after the phase at the start of the render block that consumed the PSV (`fade.h:22-24`, `engine.cpp:168-179`), then runs as a fixed 1.5 s equal-power ramp. So a crossing lands on a boundary only if its PSV was consumed at least one block before it, and a scripted instant is reachable only if a boundary falls there. The engine has no hysteresis: a threshold that flips back mid-ramp reschedules the ramp and parks the stem at a partial level until the next boundary (`engine.cpp:198`). The host must hold its inputs clear of 0.35 and 0.55 through every ramp.

In the pull model the host knows every stem's phase exactly: frames passed to `prism_render` since `prism_load_scene`, modulo the stem's length. Only `prism_load_scene` and a scene crossfade reset it. Stopping the host stream pauses phase without resetting it.

### Session start must align with the engine's phase

Boundaries sit at fixed multiples of 11 s (pulse) and 13 s (air) from scene load, not from the session. For pulse to open exactly at load t=0, a pulse boundary must fall 45 s after baseline starts, so baseline must start when **phase mod 11 s = 10 s**: a wait of up to 11 s after the attendant presses start. Started at an arbitrary phase, pulse is either audible about 1 s before load or up to 10 s late. Pulse and air cannot be aligned at the same time (11 and 13 are coprime, by design); air lands on its nearest boundary, up to 6.5 s from the scripted moment. A fresh handle per visitor does not help: phase 0 puts pulse boundaries at 44 and 55 s.

### Starting poses for the `pose` source

For prompt 2.5, as a starting point only; Week B tunes by ear. Effective values. The body may move arousal inside each segment's range.

| Segment | arousal | load | readiness | Engine state |
|---|---|---|---|---|
| baseline | 0.486 | 0.65 | 0.50 | 1,400 Hz, both gates closed, bed 0.69 |
| load | 0.486 → 0.771 | 0.65 | 0.50 | 1,400 → 3,600 Hz; pulse opens at 0.50 (~1,470 Hz), air at 0.722 (~3,070 Hz); bed unchanged |
| regulate | 0.771 → 0.506 | 0.65 → 0.948 | 0.50 → 0.11 | 3,600 → 620 Hz; air then pulse gate out; bed +3 dB, sub +5 dB. Front-load the first part so pulse's crossing is consumed before its first boundary in regulate |
| resolve | 0.506 → 0.618 | 0.948 | 0.11 | 620 → 900 Hz; bed and sub held; the host's session gain does the ending |

---

## Corrections to other prism-live documents

| Document says | Source shows |
|---|---|
| The engine's master limiter enforces −1.0 dBTP true peak as the last, unbypassable stage (experience script §0, sound brief §7) | The limiter is a sample-peak envelope follower with a hard clamp at −3.0 dBFS and no oversampling (`pgae/include/pgae/detail/dsp.h:61-87`, `engine.h:43`). In the pull model the host's output stage is last, and the host must enforce −1.0 dBTP |
| A file at any rate other than 48 kHz is rejected outright (CLAUDE.md "Audio facts", sound brief §3) | The first stem sets the engine's rate and a stem that differs is rejected (`pgae/src/scene.cpp:146-148`). A crossfade to a scene at a different rate is rejected (`core/src/prism_core.cpp:530`). 48 kHz is this project's choice, not an engine limit, and the instruction to deliver 48 kHz stands |
| 36–62 Hz belongs to the heartbeat layer | True as a rule, but nothing in the engine enforces it (spec line 124, unimplemented). Only the stems and the host can |
| Set the pre-schedule lead to three times the block time (CLAUDE.md open question 3) | With `align_to_loop_boundary = 0` that starts the crossfade early. See Q3 |
| Scene changes go through `prism_crossfade_scene` (CLAUDE.md "Audio facts") | True on `acbfd50` only. It is not in the library built on this machine |

CLAUDE.md was corrected on 11 September. The experience script and sound brief rows still stand; the script carries a note in §2.

---

## Decisions, 11 September 2026

1. **One scene, all four stems, coprime loops kept. No crossfades.** Closes open question 2 and makes open question 3 moot.
2. **Air is re-scripted to fit the engine:** it gates out before pulse in one 1.5 s fade, and there is no air tail in resolve. Nothing moves out of the engine.
3. **Body-derived versus designed-pose PSV is deferred to Week B**, to be decided by listening, not on paper. Prompt 2.5 makes the source switchable at runtime.
4. **prism-live owns the audio device** (CLAUDE.md hard rule 8) through a native shim: pull with `prism_render`, 62 Hz high-pass, heartbeat layer, session gain, true-peak limiter. Prompts 2.5 to 2.7 were rewritten to match.

Still open: **where the `acbfd50` library gets built.** The clone this document was written from was deleted on 11 September; `D:\ANP\prism-core` has never been fetched and holds an older revision. No tags exist on either remote.
