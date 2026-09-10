# Prism Live: Every Claude Code Prompt, In Order

Work top to bottom. Each prompt assumes the ones above it worked.

**Blockers are marked.** Do not skip past a gate.

---

# PHASE 0: SETUP

## 0.1 Git and GitHub

> Set up git in this folder and connect it to https://github.com/aaqibnp971/prism-live
>
> Write a .gitignore for a Python project, including logs/ and audio working files. Then make the first commit and push to main.
>
> If the remote already has a README or any commits, pull and rebase first rather than force pushing.

GitHub will open a browser to sign in. Approve it.

## 0.2 Confirm the handoff worked

> Read CLAUDE.md and everything in docs/, then tell me in three sentences what this project is and what the first thing to build is. Also list the five open questions from CLAUDE.md back to me.

**Check the answer.** It should say a four-minute heart rate demo, the beat scheduler first, and give you five questions. If it does not, something is in the wrong place. Stop and fix that.

## 0.3 The eyes edits

> Make these exact edits to the two documents in docs/. Do not change anything else.
>
> **In docs/experience-script.md, section 2, SEGMENT 2 LOAD:**
>
> 1. Find "select the correct half by gaze dwell" and replace with "select the correct half by holding the reticle on it"
>
> 2. Find "No controllers, no hand tracking, eyes only." and replace with "No controllers, no hand tracking. Reticle only, driven by head pose in VR or by pointer on the task screen. The Quest 3S has no eye tracking hardware."
>
> 3. Find "Look at the one that's still moving and hold your eyes on it until it locks." and replace with "Keep the one that's still moving in the centre and hold it there until it locks."
>
> 4. Immediately after the ramp values ending "floor 1.8 s", add this paragraph:
>
> "Note on the ramp. These values were authored assuming eye control, which the Quest 3S cannot provide. What is built is head-pose or pointer control, which is harder at the same numbers because the neck or hand has to track a target moving at up to 19 degrees per second. Treat every value here as provisional until Week E retunes them against real runs. The extra physical effort is not a problem: it raises heart rate, which is what this segment exists to do."
>
> **In docs/project-plan.md:**
>
> 5. Find "they follow it with their eyes" and replace with "they follow it"
>
> 6. Find "No controllers. Eyes only." and replace with "No controllers. Reticle only."
>
> 7. At the start of section 11, Message contract, add: "**SUPERSEDED as of 10 September 2026 by docs/message-contract-v1.md. Do not build against this section.**"
>
> 8. At the start of section 13, Full task list, add: "**SUPERSEDED as of 10 September 2026 by docs/solo-build-plan.md. This task list assumed two developers over six weeks. Do not follow it.**"
>
> The docx export may have odd spacing, so match on the nearest equivalent text rather than failing. Show me each change you made, and tell me if any string could not be found.

If anything comes back not found, paste me what it says.

## 0.4 Answer three open questions from the engine source

> The Prism Engine source is at D:\ANP\prism-core. Read it. Do not modify anything in it.
>
> Answer these from the actual code, quoting the relevant lines:
>
> 1. Is there a host-settable master output gain exposed through the C ABI? I need to ramp to silence over 3 seconds before calling prism_device_stop, to avoid the hard cut described in docs/experience-script.md section 6.5.
>
> 2. Does prism_crossfade_scene preserve loop playback phase for a stem that is byte-identical across two scene manifests, or does it restart the loop from sample zero?
>
> 3. Can the host inject a per-beat one-shot audio event into the engine's output? Look for a side-chain input, a one-shot trigger in the scene system, or any host-fed buffer. If none exists, say so.
>
> 4. Confirm the real struct field names at include/prism/prism_core.h lines 102-105 for the cadence and check interval settings.
>
> Write your findings into docs/engine-findings.md and update the open questions section of CLAUDE.md with the answers.

**These four answers shape everything after this.** Paste the results back to me before Phase 2.

---

# PHASE 1: WEEK A

Nothing here needs the armband or the engine.

## 1.1 Python scaffold and synthetic heart rate

> Set up the Python side of this repo: a `bridge/` package with pyproject, ruff, and pytest.
>
> Then build `tools/synthetic_rr.py`, which imitates what a Polar Verity Sense sends over Bluetooth, so I can develop the whole pipeline before hardware arrives.
>
> The real device does NOT emit one message per heartbeat. It notifies roughly once per second, and each notification carries the RR intervals for beats that already happened since the last one. Reproduce that exactly:
>
> - Emit a packet about every 1000 ms, with jitter of plus or minus 80 ms
> - Each packet carries 0, 1 or 2 RR intervals depending on the current rate
> - RR intervals in the real device are in units of 1/1024 second, not milliseconds. Emit them in native units and convert on the parse side, so the conversion gets tested
> - Include a flags byte matching the Bluetooth Heart Rate Measurement characteristic, with the RR-present bit set
>
> Drive it from a scriptable heart rate curve. Default profile: 68 bpm at rest for 45 s, climbing to 105 over 75 s, falling back toward 75 over 75 s, settling for 45 s. Add small beat-to-beat variability that shrinks as the rate rises.
>
> Then add fault injection, switchable per run:
> - `dropped_packet`: skip a notification entirely
> - `doubled_beat`: emit an implausibly short RR, under 300 ms
> - `missed_beat`: emit an RR roughly double the current one
> - `artefact_burst`: 5 seconds of wildly varying RR, imitating arm movement
> - `disconnect`: stop emitting for N seconds, then resume
>
> Write pytest coverage for the packet format and each fault mode. Keep it small and readable. No CLI framework, argparse is fine.

## 1.2 The beat scheduler

The single most important piece of code in this project.

> Build `bridge/beat_scheduler.py`.
>
> The problem: the armband gives us packets at roughly 1 Hz carrying beats that already happened, up to 1200 ms in the past. If we play a sound when a packet arrives, the result is a stuttering 1 Hz metronome, not a heartbeat. We need to reconstruct a per-beat timeline and schedule each beat slightly in the future so audio and visuals can both land on it together.
>
> Requirements:
>
> 1. Consume packets from tools/synthetic_rr.py.
> 2. Reject artefacts BEFORE anything else uses the data. Discard any RR deviating more than 20 percent from the median of the recent accepted window. Emit rejected intervals as events tagged `rejected` so they can be logged, but never scheduled.
> 3. Place each accepted beat retrospectively on a timeline by working backwards from packet arrival using its RR interval.
> 4. Maintain a running estimate of the current interval.
> 5. Predict the next beat and schedule it. Every scheduled beat carries `t_play`, a monotonic millisecond timestamp at least 300 ms in the future.
> 6. Run behind a deliberate buffer so there is always scheduling headroom.
> 7. When a packet arrives and the prediction was off, correct the phase gently, a few percent per beat. Never jump. Jumps are audible, drift is not.
> 8. On a gap or disconnect, keep predicting from the last known interval for a short grace period, marking those beats `interpolated`. After the grace period, stop scheduling rather than inventing a heartbeat.
> 9. Output beat events matching the `beat` message in docs/message-contract-v1.md.
>
> Then build `tools/click_track.py`: consume the scheduler's output and play a short click at each `t_play` through the default audio device. Simplest thing that works, sounddevice is fine. This exists so I can listen to it, not for production.
>
> Unit tests for every fault mode from 1.1, plus: no two scheduled beats ever overlap, `t_play` is always in the future when emitted, and phase correction never produces a step larger than 5 percent of the current interval.

## GATE: STOP HERE AND LISTEN

Run the click track for five minutes on the default profile, then on each fault mode.

Then walk up a flight of stairs, sit down, and listen while your rate comes back down. That is the exact experience the demo is built on.

**Does it feel like your heartbeat, or like a machine?**

Tests cannot answer this. Only you can. If it feels wrong, come back and tell me what it sounds like: stuttering, mechanical, lagging behind your chest, too even. Each has a different fix and they are all in the scheduler.

**Do not build anything else until this feels right.**

## 1.3 Transport and the fakes

> Build `bridge/server.py`, a WebSocket server implementing docs/message-contract-v1.md exactly. All four message types, both directions, schema version 1.
>
> Include the `clock` ping and pong exchange, and a client-side helper showing the offset calculation from the contract, since the web screens and Unity will both need it.
>
> Then two tools:
> - `tools/fake_sender.py` replays a recorded JSONL session at real speed over the WebSocket. Every client gets built against this.
> - `tools/fake_receiver.py` connects and prints each message with the wall-clock delta between `t_play` and arrival. This verifies scheduling headroom is real.
>
> Add `bridge/logging.py`: every message written to JSONL, one file per session, keyed by a session id. Never store a person's name.
>
> Record one clean session from the synthetic generator into tools/fixtures/ and make it the default input for fake_sender.

---

# PHASE 2: WEEK B

**Blocked on 0.4 being answered.** Do not start 2.5 or 2.6 without those findings.

## 2.1 HRV and baseline

> Build `bridge/hrv.py` and `bridge/baseline.py`, consuming accepted beats from the scheduler.
>
> hrv.py: RMSSD over a rolling 60 second window, plus mean HR. Also expose ln(rmssd), which behaves better for linear mapping. Do NOT implement LF/HF; it needs several minutes and its interpretation is contested, so claiming it off 60 seconds is indefensible.
>
> baseline.py: capture 45 seconds. Use the LAST 30 seconds for hr_base and rmssd_base, because the person has just walked off a loud floor and their rate is still falling. Fit a slope across the window and expose it as a baseline_quality score. Implement the quality gate from docs/experience-script.md section 2 BASELINE: at least 35 of 45 seconds of clean data and at least 30 accepted RR intervals, or the attendant re-seats the armband and restarts.
>
> Tests against the synthetic generator, including a profile where heart rate is still falling steeply through the whole baseline window.

## 2.2 PSV and confidence

> Build `bridge/psv.py`. Produce the four PSV values from HR delta and RMSSD delta against baseline, each with its own confidence.
>
> - arousal: HR delta plus inverse RMSSD, as z-scores against personal baseline. Can be genuinely confident
> - readiness: baseline HRV level plus HR recovery slope
> - cognitive_load: primarily from task events, HR secondary. Task events arrive later, so stub that input for now
> - valence: confidence hardcoded to exactly 0.0. Not derivable from a pulse. Never let it move
>
> Confidence rises with the fraction of RR intervals surviving rejection, window fill, time since the last dropout, and baseline_quality. It falls on dropout and when the sensor contact bit goes false.
>
> Add a test asserting valence confidence is exactly 0.0 under every input, including garbage.

## 2.3 Authority

> Build `bridge/authority.py`. One function: `min(confidence, segment_ceiling)` per dimension, using the ceiling table in CLAUDE.md including the resolve taper across T−45 s to T−12 s.
>
> This is computed once, host-side, and sent to every client. Nothing else in this repo or in any client may compute authority independently.
>
> Wire it into the `state` message.

## 2.4 Session state machine

> Build `bridge/session.py`. States: idle, baseline, load, regulate, resolve, reset.
>
> Durations from docs/experience-script.md: baseline 45 s fixed, load 75 s fixed, regulate 75 s adaptive with a +30 s cap, resolve 45 s.
>
> Every segment has a hard timeout. Regulate must proceed to resolve when the cap is hit regardless of physiology. Time-driven with physiological modulation, never physiology-driven with a hopeful timer. There is a queue of people and a seven-minute cycle.
>
> Implement the regulate success threshold: regulated when any 20 second rolling window has mean HR at or below HR_load minus max(5 bpm, 0.5 times rise), where rise = HR_load minus HR_base.
>
> Emit segment, segment_elapsed_ms and segment_nominal_ms in every state message.

## 2.5 Engine wrapper

**Needs 0.4 answer 4.**

> Build `bridge/engine.py`: load the Prism Engine shared library with ctypes and wrap the C ABI calls we need. Read docs/engine-findings.md first.
>
> Set cadence_ms = 2000, check_interval_ms = 50, significant_delta = 0.05, using the real field names from the findings.
>
> Feed PSV and confidence across the actuation input. The engine never receives a heart rate.
>
> Implement stop as: ramp the master gain to zero over 3 seconds, then call prism_device_stop. If engine-findings.md says no host-settable gain exists, stop and tell me rather than working around it.
>
> Verify the cadence change by logging emission timestamps across a four-minute run. Expect roughly 120 emissions, not 8.

## 2.6 The per-beat audio path

**Needs 0.4 answer 3. The riskiest piece in the project.**

> Read docs/engine-findings.md answer 3, then propose how to get the per-beat heartbeat sound into the output. Do not write code yet, give me two options with trade-offs.
>
> Requirements either way:
> - 44 Hz fundamental, energy confined to 36 to 62 Hz, roll-off 24 dB/oct above 120 Hz
> - 8 ms attack, decay min(220 ms, 0.55 times current RR) so beats never overlap
> - Fires at `t_play`, never on arrival
> - Beat events cross into audio through a lock-free ring buffer
> - Nothing allocates, locks, logs or touches a file inside the audio callback
>
> This is called the heartbeat layer everywhere in code. Never call it "pulse", because the `pulse` stem is a different thing.

Paste the two options to me before choosing.

## 2.7 Scene manifests and crossfade scheduling

**Needs 0.4 answers 2 and 3.**

> Build `bridge/scenes.py`: four scene manifests, one per segment, from the audio spec in docs/experience-script.md section 2.
>
> Scene changes go through prism_crossfade_scene, which blocks for the decode. Call it ahead of the boundary on a dedicated control thread, never from a frame or audio loop. Pass align_to_loop_boundary = 0.
>
> Measure the actual block duration and set the pre-schedule lead to three times that, not a guess. Write the measurement into docs/engine-findings.md.
>
> If docs/engine-findings.md says loop phase is NOT preserved for identical stems across manifests, stop and tell me. That breaks "the seam the person must not hear" and needs an architectural decision, not a workaround.

## 2.8 BLE bridge

**Needs the armband in your hands.**

> Build `bridge/ble.py` using bleak. Connect to a Polar Verity Sense, subscribe to Heart Rate Measurement characteristic 0x2A37 on service 0x180D.
>
> Parse the flags byte FIRST, then index everything else from it. Do not hardcode offsets: the payload is variable length depending on 8 vs 16 bit HR, sensor contact support, energy expended, and RR presence.
>
> Convert RR from 1/1024 second units to milliseconds.
>
> Expose the sensor contact bit; it feeds the confidence model.
>
> Handle reconnection gracefully. Dropouts are common and must degrade rather than crash.
>
> Output must be drop-in compatible with tools/synthetic_rr.py so the rest of the pipeline is unchanged.
>
> First run: print raw packets and confirm the RR-present flag is set. Tell me if it is not.

## GATE, end of Week B

Armband on, press start, four minutes, no manual intervention. Sound changes with your body. You hear your pulse throughout.

**If this does not pass, VR is cancelled and the remaining weeks harden the audio version.**

---

# PHASE 3: WEEK C

## 3.1 Browser load task

> Build the load task as a plain HTML/JS page in `web/task/`. It runs full screen on a monitor in front of the seated person.
>
> Mechanic from docs/experience-script.md section 2 LOAD: an object drifts, splits in two, one half keeps moving and one stops. The person holds a reticle on the moving half until it locks.
>
> Ramp, linear over 75 s: dwell-to-select 900 to 420 ms; split interval 5.5 to 2.2 s; distractor halves after each split 1 to 3; object angular speed 6 to 19 degrees per second. Miss penalty: each miss brings the next split 0.4 s sooner, compounding, floor 1.8 s.
>
> Reticle follows the mouse. Structure it so the input source is swappable, because the Unity version drives the same reticle from head pose.
>
> Connect to the WebSocket server. Read segment from `state` and start only when segment becomes `load`.

## 3.2 Task events into cognitive_load

> Emit `task_event` messages from the task screen per docs/message-contract-v1.md section 3: split, lock, miss, abandon, with dwell_ms, split_interval_ms and a difficulty value from 0.0 to 1.0.
>
> On the bridge side, feed those into cognitive_load in psv.py, replacing the stub. Task events are the primary source; heart rate is secondary. Confidence on cognitive_load rises with the number of events received.

## 3.3 Spectator screen, fed live

> The spectator screen design exists but is a mock with hardcoded data. Rebuild it in `web/spectator/` reading the live WebSocket feed.
>
> Replace the synthetic heart rate curve and the hardcoded PSV arrays with the real feed. Take segment boundaries and progress from segment, segment_elapsed_ms and segment_nominal_ms, never from a local clock, because regulate is adaptive and can run 30 s over.
>
> Corrections from the design review:
> - valence confidence and authority render as exactly 0.00 and a zero-width bar, labelled so it reads as deliberate
> - valence's unknown chip says NOT READABLE, not NO SIGNAL
> - all copy in second person: YOUR HEART, YOUR RESTING RATE, YOUR FOUR MINUTES
> - the field mirror is labelled "THE SAME NUMBERS, DRAWN HERE", not "LIVE FROM THE HEADSET". The laptop never receives the headset's frames
> - the regulate segment verb is TAKING THINGS AWAY, not GUIDING HER DOWN
> - heart rate range auto-scales rather than clamping at 50 to 155
> - the trace samples per beat, not once per second
>
> No simulation mode in this build. None. A screen that looks live while playing a canned curve is undetectable from the room and is the worst failure this project can have.

## 3.4 Ambient field for regulate and resolve

> Build the ambient visual field in `web/task/` for the regulate and resolve segments, using the token sheet in docs/experience-script.md section 4.
>
> Seven tokens: fog density, light intensity, hue, saturation, horizon position, pulse amplitude, field motion rate. Driven by the state message and the beat message.
>
> Hard rail: peak-to-peak luminance modulation from the heartbeat never exceeds 0.22 of base field luminance, never applied as a full-field flash, 90 ms rise. This is a photosensitivity limit, not a style choice.
>
> Between segments, cross-dissolve over 10 s. Never sweep hue between segments.
>
> This becomes the specification for the Unity scene later, so keep the token application logic separate from the rendering.

## 3.5 Trace reveal

> Build the trace reveal, shown on the spectator screen at T−20 s of the resolve segment.
>
> Their heart rate curve for the whole session, sat down at / peaked at / left at as three large numbers, and which dimensions held authority.
>
> Held for 20 seconds after the session ends so they can photograph it. Large type, readable from several metres.

## 3.6 Attendant controls and crash recovery

> Single-button start, stop and reset for the attendant.
>
> Crash recovery: if any component dies, the attendant is back up in under 30 seconds. That means every process restarts clean, reconnects, and no manual steps.
>
> Test by killing each process mid-session and timing the recovery.

---

# PHASE 4: UNITY

**Only if Phases 1 to 3 are done by 30 September.** Otherwise this does not happen and the showing is the seated version.

Prompts for this phase come later. Unity Editor work cannot be done by Claude Code, so this phase is mostly you, with Claude Code writing C# scripts.

---

# RUNNING PROMPTS

Use these throughout.

**End of every session:**

> Run the tests, then commit and push with a message naming what we built.

**When something breaks:**

> This is failing: [paste the error]. Do not guess. Read the relevant code and tell me what is actually happening before proposing a fix.

**When it proposes something that sounds like scope:**

> Check that against the "Out of scope" section in CLAUDE.md before we build it.

**Weekly:**

> Read docs/solo-build-plan.md and tell me where we are against it, what is behind, and what the next gate is.
