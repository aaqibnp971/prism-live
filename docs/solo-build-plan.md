# Prism Live: Solo Build Plan

> **PARTIALLY SUPERSEDED.** Where this document disagrees with CLAUDE.md or all-prompts.md, those win. Specifically: tasks A.7, A.8 and B.1 describe work inside the engine repo, which CLAUDE.md hard rule 1 forbids. Those items are host-side or blocked on open question 1. Week A does not require the armband; the BLE bridge moved to all-prompts.md prompt 2.8.

**Replaces:** Project Plan v1 §13 task list
**Reason:** one person is now Dev A, Dev B and Ridhwan. The original plan assumed three.
**Written:** 10 September 2026. Showing is 15 October 2026, five weeks out.

---

## The change in scope, stated plainly

The original plan budgeted roughly six weeks of two full-time developers plus a founder doing design and decisions. That is about eighteen person-weeks. What actually exists is **five weeks of one person who is also running a company**, plus Script §6 adding about a week of engine work nobody had planned.

That is not a small overrun. It is roughly four times the available capacity.

So this plan does not try to build Project Plan v1. It builds **the smallest thing that still proves the claim**, in an order where the riskiest item is settled first, and treats VR as a stretch goal that only starts if the audio half is finished early.

Your own plan already sanctioned this. Task 2.11: *"If it is not, we cut VR and ship the seated audio version. That decision is made on the date, not on optimism."*

---

## What survives the cut, and what waits

| Tier | What | Why |
|---|---|---|
| **1. Must exist** | Armband to heart rate. Beat scheduler. Baseline and HRV. PSV into the engine. Authority. Four-segment state machine. Audio out with a real fade. Trace reveal. Logging | This is the proof. Without any one of these there is no demo |
| **2. High value, cheap** | Spectator screen fed live. Browser load task | Several hundred people see the screen, about fifty wear a headset. The screen is where the commercial work happens |
| **3. Stretch** | Unity, Quest, in-headset visuals | The most expensive part, built by the person with the least experience in it |

**Tier 3 does not start until Tiers 1 and 2 are done.** If that is 30 September, VR is in. If it is later, the showing is a seated audio and screen demo, and VR is v1.1.

### How the load segment works without a headset

The script's load task is a drifting object that splits, selected by dwell. That runs perfectly well **in a browser, full screen, on a monitor in front of the chair**, with the reticle following the mouse or a trackpad instead of the head.

Same mechanic. Same ramp. Same difficulty curve. Same `task_event` messages back to the laptop. It raises heart rate the same way, because what raises heart rate is time pressure and failure, not the display technology.

And when Unity does happen, the browser version is the specification. You will have already tuned the numbers on real people.

---

## Week A: 10 to 16 September. Does the heartbeat feel like a heartbeat?

Everything after this week depends on one thing working, so build only that thing.

| # | Task | Notes |
|---|---|---|
| A.1 | **Freeze the message contract.** Done, see contract v1 | Today |
| A.2 | **Send the sound brief.** Placeholder stems requested for this week | Today |
| A.3 | **BLE bridge.** Separate process. Connect to Verity Sense, subscribe to Heart Rate Measurement `0x2A37`, print raw packets | Parse the flags byte first. RR values are in 1/1024 s, not ms |
| A.4 | **Confirm the RR-present flag is actually set.** If it is not, the whole HRV plan changes today rather than in Week B | Hard gate |
| A.5 | **Beat scheduler.** RR intervals to a scheduled beat timeline. Output a click track. Nothing else | The most important task in this document |
| A.6 | **Listen to A.5 for five minutes wearing the armband.** Walk up a flight of stairs. Sit down. Does it feel like your heartbeat, or like a stuttering metronome? | If it feels wrong, nothing later fixes it |
| A.7 | **§6.5, fade to silence** in `prism_device_stop` | A few hours. Do it while you are in the core |
| A.8 | **§6.1, cadence config:** `cadence_ms = 2000`, `check_interval_ms = 50`, `significant_delta = 0.05` | Ten minutes |
| A.9 | **Record one clean JSONL session** and keep it forever as the fake sender's input | Everything else gets built against this |

**Gate, end of Week A:** wearing the armband, you hear a click on every beat, in real time, and it feels like your pulse.

If A.5 and A.6 are not done by Monday 15 September, stop and re-scope the whole showing.

---

## Week B: 17 to 23 September. Close the loop, audio only.

This is the original Week 2 go/no-go, moved.

| # | Task | Notes |
|---|---|---|
| B.1 | **§6.2, biometric input into the PCE** | The big one. See the acceptance test below |
| B.2 | Artefact rejection, then HRV features. RMSSD on a rolling 60 s window | Rejection comes first, always. One bad interval wrecks RMSSD |
| B.3 | 45 s baseline capture, with a slope check on the last 30 s | Feed baseline quality into confidence rather than pretending it is clean |
| B.4 | Arousal and readiness mapping from HR delta and RMSSD against baseline | |
| B.5 | Confidence model. Valence pinned at exactly 0.0 | |
| B.6 | **§6.3, authority layer.** `min(confidence, segment_ceiling)`, computed once, laptop-side | Both actuators read the same value or the screen and sound disagree live |
| B.7 | Audio output path, and the per-beat sub-bass through a **lock-free ring buffer** | Never allocate, lock, log or touch a file inside the audio callback |
| B.8 | Placeholder stems in. Four scene manifests | |
| B.9 | **§6.6, pre-scheduled scene crossfade** on a control thread, `align_to_loop_boundary = 0` | Answer the two questions below first |
| B.10 | Four-segment state machine, with a hard timeout on every segment | Time-driven with physiological modulation, never physiology-driven with a hopeful timer |
| B.11 | Per-session JSONL logging of everything | Week E tuning is guesswork without this |

### §6.2 acceptance test, write it down before you start

1. Add the biometric contribution to `fuse_to_psv` wired in at **importance 0.0**.
2. Run the golden traces. Output must be **bit-identical** to today.
3. Only then raise importance.

The trap: `blend()` computes confidence as `Σ(conf·imp)/Σ(imp)`. A contribution shipped at confidence 0 but importance above 0 still sits in the denominator and drags every other dimension down by about 0.183, against a tolerance of 1e-6. Every fixture fails at once and it looks like an unrelated regression.

### Two questions to answer before B.9

1. **Does `prism_crossfade_scene` preserve loop playback phase** for a stem that is byte-identical across both manifests? If it restarts, the bed jumps to sample zero three times per session and "the seam the person must not hear" becomes the most obvious thing in the demo.
2. **How long does it actually block for the decode?** Measure it. Schedule the call three times that far ahead of the boundary. Do not guess.

**GATE, Friday 23 September.** Armband on, press start, four minutes, no manual intervention. The sound changes with your body and you can hear your own pulse throughout.

If this gate does not pass, VR is cancelled. Not deferred, cancelled, and the remaining three weeks go into making the audio version excellent.

---

## Week C: 24 to 30 September. The task and the screen.

| # | Task | Notes |
|---|---|---|
| C.1 | **Browser load task.** Drifting object splits, dwell to select, 75 s, ramp per script §2 LOAD | Dwell 900 to 420 ms, split interval 5.5 to 2.2 s, distractors 1 to 3, angular speed 6 to 19 °/s |
| C.2 | `task_event` messages back to the laptop | The reverse channel from contract v1 §3 |
| C.3 | Task events feed `cognitive_load` | Plan task 3.5 |
| C.4 | **Spectator screen fed live.** Replace `hrAt(t)` and `reads()` with the WebSocket feed | The design already exists. This is wiring, not design |
| C.5 | Apply the spectator display corrections: authority arrays, valence hard zero, second person copy, adaptive timeline | |
| C.6 | Ambient field on the task screen for regulate and resolve | 2D version of the visual language. Becomes the Unity brief later |
| C.7 | Trace reveal screen | Start against end, which dimensions held authority |
| C.8 | Final stems in, replacing placeholders | |
| C.9 | Single-button start, stop, reset | Plan task 4.2 |

**DECISION, Tuesday 30 September.** Is Unity in or out?

In only if C.1 to C.9 are genuinely done, not nearly done. Out means Week D and E go into hardening, and you show a very good seated demo instead of a shaky VR one.

---

## Week D: 1 to 7 October. Two possible weeks.

### Path 1, VR is out: harden

| # | Task |
|---|---|
| D.1 | Crash recovery. Any component dies, back up in under 30 seconds |
| D.2 | First five internal runs on team members |
| D.3 | Tune the load segment until heart rate reliably rises |
| D.4 | Tune the regulate segment until heart rate reliably falls |
| D.5 | Begin the 20 outside runs early. More data, better tuning |
| D.6 | Decide the first event, order booth items |

### Path 2, VR is in: the Unity sprint

| # | Task |
|---|---|
| D.1 | Hello-world APK sideloaded to the Quest. **One day, hard gate** |
| D.2 | Unity project, URP, XR Plugin Management, Meta XR SDK, build pipeline |
| D.3 | WebSocket receiver, background thread, marshalled to the main thread |
| D.4 | Seated XR rig, eye-level tracking origin, recentre on start |
| D.5 | Port the browser ambient field to a Unity scene. Fog volume, one diffuse light, horizon |
| D.6 | Head-gaze raycast, reticle, dwell timer, port the C.1 task |
| D.7 | Performance pass. Hold 90 fps, audit transparent overdraw, no bright points on near-black |

If D.1 takes more than a day, VR is out again and you fall back to Path 1 with four days lost. That is the cost of trying, and it is affordable.

---

## Week E: 8 to 14 October. Real people.

| # | Task | Notes |
|---|---|---|
| E.1 | **20 runs on people who do not work here** | Start recruiting in Week C, not this week |
| E.2 | After each run, ask **"Did you notice anything change in you, and what?"** and write down the exact words | The primary success measure |
| E.2a | After each run, also ask **"Did it feel too long?"** and write the exact words down beside the answer to E.2 | Decides whether the 4:00 run is shortened (experience script §0) |
| E.3 | Fix whatever breaks across the 20 runs | |
| E.4 | Noise test: 80 dB crowd noise beside the chair, full run, repeat | |
| E.5 | One-page attendant runbook | Must say: seat the visitor at the greet, so the pitch happens sitting down. That, not the armband order, buys settling time before the baseline (project plan §8) |
| E.6 | Hygiene kit, packing list, case loadout | |
| E.7 | Full dry run with the complete booth setup | |
| E.8 | Consent and exclusion script finalised | See below |

**Gate:** 20 consecutive runs with no crash, and a majority naming a change unprompted. If the second half fails, the fix is the arc, not the code.

---

## Still unowned, and still needed

| Item | Why it matters | Owner |
|---|---|---|
| **Exclusion script** at the greet: pregnancy, heart conditions, epilepsy or photosensitivity, severe motion sickness | You deliberately raise strangers' heart rates | ? |
| **Consent line** for heart rate capture, plus a retention decision for the JSONL logs | Physiological data, UAE PDPL, sensitive category | Zayaan |
| **Age policy** | Meta guidance is 13+ | ? |
| Second Verity Sense armband | Single point of failure for the entire closed loop, ~AED 380 | You |
| Headset power for a 50-person day | Quest 3S runs about 2.5 hours. A full day is over 4 hours of headset-on time | You |
| Travel router, 5 GHz | Exhibition halls destroy 2.4 GHz. Never use venue WiFi | You |
| 20 outside test subjects | Two weeks of lead time. Start asking in Week C | You |
| First event decision | Booth applications often close 6 to 8 weeks ahead. This may already be late | You |

---

## The ladder, if you fall behind

Cut in this order. Each step below is still a demo worth showing.

1. **Full**: VR visuals, browser spectator screen, audio, armband.
2. **Cut VR.** Task and field on a monitor, spectator screen, audio, armband. *This is the realistic target.*
3. **Cut the spectator screen to static.** Show the trace reveal only, narrator talks over it.
4. **Cut the visual field.** Load task on screen, then screen goes dark, audio only for regulate and resolve. The heartbeat still lands.
5. **Cut the load task.** Baseline, then regulate, then resolve. Weaker, because the person never goes up first, but they still hear their own pulse slow.

Below step 5 there is no demo, because the person no longer hears their own heartbeat, and that is the only thing here that nobody else has.
