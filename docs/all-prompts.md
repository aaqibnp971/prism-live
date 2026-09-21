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

## 0.4 Answer open questions 1, 2 and 4 only, from the engine source

**Scope:** items 1, 2 and 3 below answer CLAUDE.md open questions 1, 2 and 4. Item 4 is a field-name check, not an open question. Q3 cannot be read from the code: it must be measured at runtime, in prompt 2.7. Q5 needs the armband in hand; see prompt 1.0.

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

Nothing here needs the armband or the engine, except 1.0, which runs the day the armband arrives.

## 1.0 RR-present check

**The day the armband arrives, before anything else.** Whatever else is in progress, stop and run this first.

> Write `tools/rr_flag_check.py`, a single throwaway script. It imports nothing from `bridge/` and has no dependency on the rest of ble.py. It exists to answer one question.
>
> Using bleak, scan for the Polar Verity Sense by matching a device name containing "Polar", and print every device found if nothing matches. Connect, and subscribe to the Heart Rate Measurement characteristic 0x2A37 on service 0x180D.
>
> For every notification, print:
> - the raw packet as hex
> - the flags byte in binary
> - whether the RR-present bit (bit 4, 0x10) is set
> - the sensor contact bits (bits 1 and 2), so a missing RR flag with no skin contact is not mistaken for the answer
>
> Run for 60 seconds, then print a summary: packets received, and how many had the RR-present bit set.
>
> This is twenty minutes of work. Do not build it out any further.

Put the armband on properly, sit still, and run it.

**If the RR-present flag is not set, stop and tell me immediately.** The HRV plan is wrong, and so is the synthetic generator's fixture, because prompt 1.1 builds it with that bit set. Everything after 1.1 would be developed against a device that does not behave that way. This answers CLAUDE.md open question 5.

**The rest of armband day.** Once 1.0 has answered, and before anything depends on constants tuned on the synthetic armband:

1. **Record a real seated session.** About five minutes with the armband on the upper arm: settling, a task that raises heart rate, and recovery. Save every raw notification with its arrival time, not only the contract messages, so the scheduler, the HRV cleaner and the PSV can be re-run on it. It becomes the real fixture in `tools/fixtures/`. The same run answers the contact questions in CLAUDE.md open question 5.
2. **Retune the ectopic threshold on it.** `ECTOPIC_QUARTILE_DEVIATIONS` in `bridge/hrv.py` is 12, tuned on synthetic variability that is random from beat to beat, where real variability follows the breath (docs/known-limits.md). Check the firing rate on clean stretches of the recording, and the detection of misplaced beats, at 5.2 and at 12. A clean recording has no known misplaced beats, so for detection inject some into it, or use an annotated public dataset with real ectopic beats. Set the threshold from those, then re-measure everything measured with 12: the leak and threshold tables in docs/known-limits.md, the rmssd_base error table behind the 20-difference rule, and the baseline confidence curve in docs/vr-handoff.md §9. Until then, treat 12 and every number measured with it as provisional.

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

**0.4 is answered.** Read docs/engine-findings.md before 2.5. Prompts 2.5 to 2.7 were rewritten on 11 September to match the engine as it is: prism-live owns the audio device, one scene for the whole session, no crossfades.

## 2.1 HRV and baseline

> Read docs/known-limits.md first. The beat scheduler's accepted intervals have four limits this code must handle: artefacts that pass its filter, accepted intervals that are not adjacent, packet loss with no marker, and weak filtering of the first three intervals.
>
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
> Durations from docs/experience-script.md: baseline 45 s of capture plus the hold below, load 75 s fixed, regulate 75 s adaptive with a +30 s cap, resolve 45 s.
>
> Every segment has a hard timeout. Regulate must proceed to resolve when the cap is hit regardless of physiology. Time-driven with physiological modulation, never physiology-driven with a hopeful timer. There is a queue of people and a seven-minute cycle.
>
> Implement the regulate success threshold: regulated when any 20 second rolling window has mean HR at or below HR_load minus max(5 bpm, 0.5 times rise), where rise = HR_load minus HR_base.
>
> Emit segment, segment_elapsed_ms and segment_nominal_ms in every state message. Build every state message with `bridge/state.py`'s `StateStream`, one per session: it computes authority once, with `bridge/authority.py`, and remembers where resolve's taper starts.
>
> **End of baseline: wait for hr_base.** The baseline capture closes at 45 s, but its result arrives 3.4 to 10.2 s later, because every interval is classified 3 to 7.5 s late (docs/known-limits.md). A packet lost just after the window moves that to between 2.0 and 10.6 s. At the end of the 45 s, hold the session in baseline until `hr_base` is available, for at most 12 s, then enter load. If it is still not available at 12 s, enter load anyway and mark the baseline degraded: `signal.baseline_quality` goes out as 0.0, `hr_base` stays null, and the session log records why. The contract is frozen, so degraded is carried by those two values, not by a new field.
>
> Contract v1.3 records both consequences: a degraded session keeps `hr_base` null to the end, and baseline can run up to 12 s past its nominal 45 s.
>
> - CLAUDE.md and docs/experience-script.md §2 say so: the capture stays 45 s, and the segment runs 45 to 57 s.
> - `bridge/psv.py` owns the capture: `PsvModel.start_baseline(t)` when baseline begins, `PsvModel.baseline` for the result, `PsvModel.mark_baseline_degraded()` when load begins without `hr_base`.
> - While holding, `segment` stays `baseline`, `segment_nominal_ms` stays 45000 and `segment_elapsed_ms` keeps counting past it. Authority stays at the baseline ceilings, so nothing acts.
> - A result that is ready but fails the quality gate is not degraded. That is the experience script's re-seat and restart, unchanged.
> - 2.7 fixes the length of the hold: load starts on the pulse boundary at 56 s, degraded if `hr_base` has not come by then. Build the 12 s cap here, and let 2.7 shorten it to that boundary.
>
> **rmssd_base is optional everywhere downstream.** It can be None while the quality gate passes, after a few artefacts in the last 30 s of baseline. Nothing may assume it exists. Load's secondary criterion (RMSSD over its final 30 s at or below 0.80 × baseline) and regulate's recorded RMSSD return are skipped and logged as unavailable when it is None; the heart rate criteria decide alone.
>
> Decided 13 September:
>
> - With a degraded baseline there is no HR_base, so neither load's activation nor regulate's success threshold can be computed. Regulate then runs a plain 75 s with no extension, because the extension only waits for a threshold that cannot be judged, and the log records why.
> - An RMSSD criterion needs at least 20 clean differences in each of its 30 s windows, and is skipped otherwise. Below that, a 30 s RMSSD is more than 20 % off in about one run in five (docs/known-limits.md), enough to call a person activated when they were not.

## 2.5 Engine wrapper, the audio shim, and the PSV feed

**Needs a library built from prism-core `acbfd50`.** It is committed at `vendor/lib/libprism_core.dll`, with the recipe in `vendor/lib/README.md` (CLAUDE.md open question 7, closed 14 September). The older DLL in `D:\ANP\prism-core` will not do.

> Read docs/engine-findings.md first, all of it. The engine has no master gain, no way to inject audio, and its only PSV input is `prism_set_mood_override`. prism-live owns the audio device (CLAUDE.md hard rule 8). Two pieces.
>
> **`native/`, the audio shim.** A small C++ shared library with its own C ABI, driven from Python by ctypes. It owns the output device, and its callback is the only place `prism_render` is ever called. Per block, in this order:
>
> 1. `prism_render(core, buf, n)` into a preallocated mono float32 buffer. 48 kHz, no resampling anywhere.
> 2. High-pass the engine buffer: a 10th-order Chebyshev type II, at least 30 dB down from 62 Hz and within 1 dB from 69.35 Hz, so the sub's D2 at 73.4 Hz stays. This is the 36 to 62 Hz reservation, enforced in code rather than trusted to the stems. (Decided 14 September. As first written, "62 Hz, 24 dB/oct" is only 3 dB down at 62 Hz and 12 dB at 44 Hz, and could never pass the band test below.)
> 3. Apply a static engine trim (start at −6 dB, tune in Week B) and the session gain: a smoothed value the bridge sets as a target plus a ramp time. It carries the resolve ending, bed, sub and air to silence from T−22 s to T−10 s, and the 3 s fade at stop. Between visitors it is 0.
> 4. Add the heartbeat layer (2.6). It has its own level envelope and is not under the session gain, so it holds while the engine material leaves.
> 5. True-peak limit at −1.0 dBTP. This is the last stage. The engine's own limiter is −3 dBFS sample-peak and protects nothing the host needs.
> 6. Write to the device, duplicated to stereo if the device wants it.
>
> Never call `prism_device_start` or `prism_device_stop`. Stop is: ramp the session gain and the heartbeat level to zero over 3 s, wait, then stop the shim's own stream. The engine keeps rendering between visitors so its phase keeps advancing; only the session gain sits at 0.
>
> Shim ABI: open, start, stop, close; `set_session_gain(target, ramp_ms)`; `push_beat` and `set_heartbeat_level` (2.6); `frames_rendered()`, the number of frames passed to `prism_render` since `prism_load_scene`. That count is the engine's phase, and 2.7 depends on it being exact. Everything the callback touches is preallocated. No allocation, no locks, no logging, no I/O (hard rule 4). miniaudio is fine for the device; the engine uses it too.
>
> **`bridge/engine.py`.** ctypes over the engine's C ABI, for the control thread only: `prism_create`, `prism_load_scene`, `prism_set_mood_override`, `prism_clear_mood_override`, `prism_sample_rate`, `prism_destroy`. ctypes over the shim as well. After loading the scene, refuse to start unless `prism_sample_rate()` is 48000. Set `cadence_ms = 2000`, `check_interval_ms = 50`, `significant_delta = 0.05` in `prism_config` (field names in the findings), but do not call `prism_start`: the bridge is the only PSV writer, which also avoids a two-writer race described in the findings. Test that audio renders and the override is honoured without `prism_start`. If it is not, tell me; the fallback is `prism_start` with `cadence_ms` huge and `significant_delta = 2.0`, and it carries a sequence-number quirk noted in the findings.
>
> **The PSV feed.** Every 2 s, and at every segment boundary, the bridge sends one `prism_mood_override` from its control thread: `mode_hint` NULL, `confidence` 1.0, valence 0.5, and arousal, cognitive_load and readiness from whichever source is active:
>
> - `body`: `0.5 + (v − 0.5) × authority` per dimension, v being the body-derived value from psv.py and authority the value the latest state message carries, from `bridge/state.py`. Read it from there; never call `bridge/authority.py` a second time. Authority 0 sends a neutral PSV, which the engine plays at 2,282 Hz with the pulse stem open.
> - `pose`: a designed effective PSV per segment from a small table in `bridge/poses.py`, with the body's arousal allowed to move the arousal input inside a per-segment range. Starting values are in docs/engine-findings.md.
>
> The source must switch at runtime without a restart, from a local control in the bridge (a key in the bridge console is fine). Not from a message on the WebSocket link, which is frozen. Log every update: the source, the values sent, and what the engine's mapping will do with them (cutoff, gates, gains), from a Python mirror of the mapping in the findings, used for logging and tests only. Until the Week B decision, the `state` message keeps reporting the body-derived `psv` and `confidence` whatever the engine is being fed. Never report a pose as if it were a reading.
>
> Hysteresis: hold the effective arousal and load inputs clear of the density thresholds 0.35 and 0.55 by a margin wide enough that a 2 s update cannot flip a gate during its 1.5 s ramp (2.7 explains the ramp). A flip mid-ramp parks the stem at a partial level until its next loop boundary.
>
> Tests: the pre-blend reproduces per-dimension authority exactly against the mapping formula; the shim's callback path is allocation-free under a tripwire allocator or ASan; a four-minute offline render through the shim with the heartbeat at its loudest scripted level never exceeds −1.0 dBTP; after the high-pass, the engine buffer's 36 to 62 Hz band is at least 24 dB down.

## 2.6 The heartbeat layer

**The riskiest piece in the project.** The engine cannot carry it (findings, Q4), so it lives in the shim.

> Build the heartbeat layer inside `native/`, summed in the shim's callback at step 4 of 2.5: after the high-pass and the session gain, before the limiter. It never passes through the engine's filter or limiter.
>
> - Beat events come from Python through a lock-free single-producer single-consumer ring buffer: `t_play`, `rr_ms`, `quality`. Rejected beats are never pushed.
> - The shim converts `t_play` (T_engine, monotonic milliseconds) to a sample index in its own stream. Anchor the stream's first frame to T_engine at start, then correct the anchor against the device's reported position by slewing at no more than 1 ms per second of stream time, never in a step. Log the measured error between scheduled and actual onset.
> - A beat whose `t_play` has already passed when the callback sees it is dropped and counted, never played late.
> - Voice: 44 Hz fundamental, energy confined to 36 to 62 Hz, roll-off 24 dB/oct above 120 Hz. 8 ms attack. Decay min(220 ms, 0.55 × the beat's RR), so beats never overlap. Preallocated; nothing is allocated per beat.
> - Level: a target in dBFS peak plus a ramp, set by the bridge per segment from docs/experience-script.md §2: −18 → −13 across the first 12 s of baseline; −13 at HR_base rising to −9 at HR_base + 15 bpm in load, clamped, or −13 held through load when the baseline was degraded and there is no HR_base (decided 14 September); −9 → −11 in regulate; −11 held in resolve, then an equal-power fade to silence over the final 3 s. The bridge sets targets, the shim smooths.
>
> This is called `heartbeat_layer` everywhere in code. Never call it "pulse"; the `pulse` stem is a different thing.
>
> Tests, offline through the shim with the recorded fixture: from a fixed anchor, every beat lands on the right sample index; no two complete voices overlap; a late beat is dropped and counted rather than played. Measure the complete voice, not only the filter, at 120 Hz, 250 Hz and 1 kHz and pin bounds from those measurements; also measure how much energy stays in 36 to 62 Hz. The final 3 s fade is equal-power and ends at digital silence; the summed output never exceeds −1.0 dBTP. Device-anchor correction against the real device position cannot be established by an offline fixture and stays a hardware limit.

**What 2.5 left in place.** 2.5's ceiling test needed a heartbeat, so the shim already has the ABI (`pls_push_beat`, `pls_set_heartbeat_level`), the beat slots, and a minimal voice: a 44 Hz sine from phase 0, an 8 ms raised-cosine attack, a half-cosine decay over min(220 ms, 0.55 × RR), and a linear level ramp. A beat is placed from a T_engine anchor on the output frame and put into the chain 1,639 frames (34.1 ms, the limiter's latency) early, so it leaves the limiter at `t_play`. The device anchor is taken once, at the first callback after `pls_set_time_origin_ns`, from QueryPerformanceCounter plus one device period. Keep the ABI. Replace the internals: the anchor correction against the device's reported position, onset-error logging, the spectral shaping, and this prompt's tests. `EngineHost.stop` writes the heartbeat level too, so a per-segment level controller needs the same latch as `SessionGain` (`fade_out` and `resume`).

**Corrections before implementation, 15 September.** The original spectral threshold confused a fourth-order filter's 24 dB/octave slope above its 120 Hz corner with attenuation at the corner. It is superseded by the measured complete-voice tests above. The original under-1-ms onset test is also superseded: with no device position in an offline render it proved only the fixture's own anchor. The real-device correction belongs on the armband-day list.

**Implemented 15 September.** The complete voice uses fourth-order Butterworth edges at 36 and 62 Hz and a 20 ms windowed tail; maximum support is 248 ms, including when a long-RR beat is followed 250 ms later. Across RR 250, 300 and 400 ms, 97.939% to 98.778% of energy is inside 36 to 62 Hz. Worst measured components relative to 44 Hz are −55.195 dB at 120 Hz, −105.490 dB at 250 Hz and −147.185 dB at 1 kHz; the test also directly limits integrated energy above 62 Hz. Device positions are polled off the audio thread and cross through a bounded lock-free mailbox; each onset measurement retains its own `t_play` and error in a second preallocated queue for control-thread logging. A new time origin invalidates both queued and placed beats from the prior stream run. The final level command compensates the limiter's 1,639 frames, so the audible equal-power fade—not merely the pre-limiter ramp—runs from T−3 s to T. The real position-to-DAC relationship remains item 4 on the armband-day list.

## 2.7 One scene, placeholder stems, and gate timing

**Decided 11 September: one scene for the whole session, coprime loops kept, no crossfades.** Read docs/engine-findings.md Q2 and its mapping section first.

> Build `bridge/scene.py` and `assets/scenes.json`: one manifest, one scene, and it must be the `default_scene`. Four stems: bed, sub, pulse, air. No `lead`. Load it once per handle with `prism_load_scene` on the control thread. It blocks for the decode; measure how long and write the number into docs/engine-findings.md. It only ever happens at startup or after a crash, never during a session. `prism_crossfade_scene` is never called.
>
> Placeholder stems, until the real ones arrive: `tools/make_placeholder_stems.py`. Mono, 48 kHz, 32-bit float, exact sample counts 912,000 / 816,000 / 624,000 / 528,000, seamless loops, D minor, all high-passed at 62 Hz, 24 dB/oct, in the file. Bed with harmonic content to at least 6 kHz so the filter has something to work on. Verify the counts and the 36 to 62 Hz band on the files, not by ear. docs/sound-brief-v1.md is the spec.
>
> **Added 18 September:** the bed's energy must fall smoothly while remaining present through at least 6 kHz; a near-pure tone cannot exercise the engine's 620 Hz to 3,600 Hz master-filter arc. Build `tools/check_stems.py` as a separate file-based acceptance test, independent of the generator. It accepts any subset of role-labelled stems and checks exact counts, mono 48 kHz IEEE float32, endpoint value and first-derivative continuity within 1e-6, the 36 to 62 Hz reservation, true peak below −1.0 dBTP, and the bed's smoothly falling reach through 6 kHz. Run the same checker unchanged on the composer's October delivery. Generated placeholders live under `assets/placeholders/` and are gitignored.
>
> **Done 18 September in `be9f4f9`.** Both scripts are committed, the four generated files live under the gitignored `assets/placeholders/`, and `tools/check_stems.py` passes them. Do not rebuild this section in later prompts.
>
> Gate timing, from the findings: the engine opens or closes pulse and air only at that stem's next loop boundary counted from scene load (every 11 s for pulse, every 13 s for air), then fades over a fixed 1.5 s, and it takes a boundary only if the crossing PSV was consumed at least one render block before it. Build `bridge/phase.py` on the shim's `frames_rendered()`: every stem's phase, its next boundary, and when to send a gate-crossing PSV so it lands one block before a chosen boundary.
>
> **Decided 13 September: load starts on the pulse boundary 56 s after baseline begins.** 2.4 holds baseline past 45 s until `hr_base` arrives. The result needs an interval from after the window, so it can never be ready at 45 s, and a boundary aligned there would always be missed. So align the boundary to 56 s and hold to it: every measured wait for `hr_base` ends within 10.6 s, even with a packet lost just after the window. At 56 s, enter load on the boundary; if `hr_base` has still not come, enter it degraded as 2.4 describes, which makes the effective cap on the hold 11 s.
>
> Why not let pulse open at its next boundary after load t=0, as air does: that would cost up to 11 s of a 75 s load segment with no pulse pressure, different for every visitor, and load is the segment the whole demo depends on.
>
> Session start alignment, **decided 14 September: the wait lives in the attendant console, not in the session.** Baseline must begin when the engine's phase puts a pulse boundary exactly at load t=0, 56 s later, i.e. phase mod 11 s = 10 s. That is the same condition that would put one at 45 s, since 56 = 45 + 11. `bridge/phase.py` gives that moment. The start button arms on the press, shows a countdown to it, and fires `session.start(now)` at it, a wait of up to 11 s; log the press, the wait and the firing. The session never waits: it stays in idle through the countdown, and baseline begins when start fires. The countdown is not part of the session, so `t_session` and the 4:45 hard cap both count from when start fires (docs/experience-script.md §0, contract §2); the worst case is 281 s, 4:41. 3.6 builds the button. Air's 13 s boundaries cannot be aligned at the same time as pulse's; its open in load and its close in regulate land on the nearest boundary, up to 6.5 s from the scripted moment, and that is accepted. At reset, send the baseline pose so both gates are closed before the next person sits down.
>
> `bridge/session.py` already has the hold: with `Timings(hold_ms=11_000, hold_to_end=True)`, the session enters load 56 s after baseline begins, ready or degraded. **A failed gate ends baseline at its arrival** (decided 14 September): a result that fails the quality gate goes to reset as soon as it comes, from 45 s on, and with no result by 56 s the gate is judged on what had been classified by then, and a failure goes to reset at 56 s. `Session.schedule` gives baseline's earliest end as 45 s and its latest as 56 s.
>
> The air moves in the script have been re-scripted to fit the engine: air gates out before pulse, in one 1.5 s fade, and there is no air tail in resolve. Do not try to build either.
>
> **What 2.5 leaves for this prompt** (`bridge/engine_feed.py`, `docs/known-limits.md`):
>
> - **Air can outlast pulse.** The regulate pose closes both gates in the same PSV, and each fades at its own next loop boundary. So air fades after pulse in about 85 % of sessions, which breaks "air gates out before pulse" and the test "air never sounds while pulse is closed". Send air's crossing so that it lands on a boundary before pulse's.
> - **The gate hold ignores phase.** After a gate flips, `GateHysteresis` holds it for the stem's loop plus 1.5 s, because without phase it cannot tell when the 1.5 s ramp runs. That is stricter than needed: a reversal consumed before the boundary cancels cleanly. A gate that flips late in load holds back regulate's closing for the rest of its hold, up to 14.5 s, and pulse then misses its first boundary 2 s into regulate. With `bridge/phase.py`, hold only across the ramp.
> - **The body source sends a neutral PSV in idle and reset**, which opens pulse between visitors. The baseline pose at reset applies to the pose source only until this prompt sends it for both.
>
> Tests, offline through the shim with the PSV log of a fixture session: pulse is silent for the whole of baseline and opens within one block of the aligned boundary at load t=0; pulse is gone within 1.5 s of its first boundary after the regulate PSV; air never sounds while pulse is closed; a jittering input inside the hysteresis band never flips a gate; loop phase is continuous from the first block to the last, checked by cross-correlating the bed in the output against the stem file at the end of the session.

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

## 2.9 The live bridge loop

**Nothing drives `bridge/session.py` outside tests yet.** Only the tests (`tests/conftest.py` `run_live` and `tests/test_session.py`) and the fixture recorder `tools/record_fixture.py` construct a `Session`, all on a simulated clock, and `bridge/server.py` `main()` publishes nothing. The Week B gate and 3.6 both need this loop.

**Added 18 September:** default the packet source to `tools/synthetic_rr.py`, because
`bridge/ble.py` does not exist until 2.8. Both expose the same async raw-packet interface, so the
real armband replaces one construction line. During a complete session on the real clock, measure
actual tick interval (median, p95, max) against 20 to 100 ms and beat lead at publish (median, p95,
min) against the 300 ms floor. Record the results in `docs/known-limits.md`.

> Build the live loop in the bridge: one asyncio loop on `T_engine`, the one thread `Session` is used from. It owns five things.
>
> 1. **Packets.** Every Heart Rate Measurement packet, from `bridge/ble.py` (2.8) or from `tools/synthetic_rr.py` through the same interface, goes to the beat scheduler first and then to the model: `result = scheduler.on_packet(now, payload)`, then `model.on_packet(now, result)`. Never the other way round, and never a packet fed twice: a doubled packet changes the baseline without a trace.
> 2. **Ticks.** Every 20 to 100 ms: `scheduler.tick(now)`, then `session.tick(now)`. Tick whether or not packets are arriving; a dead armband must still reach its boundaries.
> 3. **Beats.** Every beat event, from a packet or from a tick, is published as `session.beat_message(event)`, never as the scheduler's own message, so `seq` restarts at 1 in every session (contract §2). State messages leave through the `publish` callback given to `Session`; the loop passes the server's publish.
> 4. **Attendant start and stop.** Route them to `session.start(now)` and `session.stop(now)`, and treat anything `start` returns other than None as a refusal to show the attendant: `running`, `resetting`, `no_signal`, or `bad_time`, which means the loop passed a time the link cannot carry and is a loop fault. The session logs every refusal but `bad_time`. They come from a local control on the laptop, never from a message on the WebSocket link, which is frozen and carries none. Until 3.6 builds the console, a key in the bridge console is fine, and it arms and fires start the same way 2.7 describes.
> 5. **Restart.** After a crash or a restart, come up clean in idle: a new `PsvModel`, `BeatScheduler`, `SessionLog` and `Session`, and a fresh session id. `SessionLog.start_session` takes the next free id for the day, so it never reuses a file. Nothing resumes the session that was running, and a visitor who was mid-run starts again. `T_engine` restarts at 0, and clients rebuild their clock estimate on reconnect (contract §2).
>
> The wiring is in the `bridge/session.py` module docstring.
>
> **The engine side, from 2.5.** Every state message also goes to `PsvFeed.on_state` and `SessionGain.on_state` (`bridge/engine_feed.py`), from this same loop, the engine's one control thread. A local key calls `PsvFeed.set_source("body" | "pose")`. The engine opens once at startup with `EngineHost.open` and `start(gain)`, keeps rendering between visitors, and closes on shutdown with `await EngineHost.stop(gain)` and then `close`. Once a `SessionGain` exists, nothing else calls `set_session_gain`. Nothing calls any of this yet.
>
> Tests, against the synthetic armband through the real loop on a real clock: a whole session from start to the end of reset, every message valid and every beat at least 300 ms ahead when sent; a double press and a stop mid-run; a packet source that goes silent in baseline reaches the gate at the cap; killing the loop mid-session and starting it again comes up in idle with a new session id, and nothing from the old session is sent again.

**Implemented 18 September.** `bridge/live.py` owns the one-thread asyncio wiring; the server now
runs it with `SyntheticPacketSource()` by default and a local keyboard attendant. State, beat,
engine lifecycle, aligned start, clean restart and silent-source paths have real-loop tests. The
full production-clock run and the cadence correction it exposed are recorded in
`docs/known-limits.md`.

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

**Added 19 September.** Convert the 6 to 19 degrees-per-second ramp through configurable viewing
geometry rather than treating it as pixels: viewing distance and physical screen width, defaulting
to a 27-inch 16:9 monitor at 60 cm. Show the assumption in a debug corner and record the conversion
in `docs/known-limits.md`, so the browser and Unity versions can use the same angular maths. Add a
standalone URL mode that runs the full 75 s without a bridge, uses the identical ramp and timing
model as live mode, and writes its contract-shaped task events to the browser console. The source
header must retain the warning that the values were authored for eye control, unavailable on Quest
3S, and remain provisional for pointer and head control until Week E.

**Implemented 19 September.** `web/task/` contains the plain browser task. Motion is integrated in
visual degrees and projected onto the configured monitor plane; the on-screen debug corner shows
the geometry and instantaneous conversion. Live mode starts and stops only from host `state`,
freezes visibly on disconnect, and sends `task_event` messages. `?standalone=1` uses the same
`LoadTask` instance type and event builder with a console sink, and can be restarted for repeated
difficulty judging. Geometry, ramp, miss penalty, motion, events and state gating have deterministic
JavaScript tests run through pytest.

## 3.2 Task events into cognitive_load

> Emit `task_event` messages from the task screen per docs/message-contract-v1.md section 3: split, lock, miss, abandon, with dwell_ms, split_interval_ms and a difficulty value from 0.0 to 1.0.
>
> On the bridge side, feed those into cognitive_load in psv.py, replacing the stub. Task events are the primary source; heart rate is secondary. Confidence on cognitive_load rises with the number of events received.
>
> The stub is in place: `PsvModel.add_task_event(t_engine_ms, event, difficulty, dwell_ms, split_interval_ms)` stores events, `PsvModel._task_load` returns nothing yet, and `blend_cognitive_load` already combines a `TaskLoad` with heart rate so that heart rate never adds confidence alone. Stamp each event with its arrival time on T_engine: `task_event` carries only the client's clock, and the server's `on_task_event(msg, client)` callback does not pass the arrival time yet.

**Added 20 September.** A miss alone is ambiguous: struggling while still chasing is load, while
having stopped trying is not. Use `abandon` and the gap between participant-driven events to
separate them, state the rule in code and `docs/known-limits.md`, and keep cognitive-load confidence
at exactly zero until task events arrive. The browser currently supplies mouse events, while the
shipping VR input is head pose; do not tune the mapping to raw mouse dwell and abandon counts, and
record that transfer risk in `docs/known-limits.md`.

**Implemented 20 September.** `bridge/server.py` stamps each valid event with its arrival on
`T_engine`; `bridge/live.py` accepts it only for the current session during LOAD; and
`bridge/psv.py` groups events into split opportunities and applies the documented engagement rule.
Task evidence carries 80 % of the value and heart rate 20 %, but heart rate cannot create task
confidence. Confidence grows over distinct opportunities and falls to zero if the event stream
stops. One configured task screen owns the event stream at a time, a reconnect can take over after
it closes, and a handler fault visibly disconnects the screen rather than silently discarding load
evidence. The inference, exact zero-confidence gate, session reset, arrival stamping and live-loop
scope have deterministic tests. The chosen rule and its transfer limits are recorded below.

## 3.3 Spectator screen, fed live

> The spectator screen design exists but is a mock with hardcoded data. Rebuild it in `web/spectator/` reading the live WebSocket feed.
>
> Replace the synthetic heart rate curve and the hardcoded PSV arrays with the real feed. Take segment boundaries and progress from segment, segment_elapsed_ms and segment_nominal_ms, never from a local clock, because regulate is adaptive and can run 30 s over. Idle sends 0 for both, so draw no progress there (contract v1.5).
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

**Added 20 September.** Idle is a designed exhibition state, not an empty dashboard: cold idle has
no progress, trace or previous visitor readings, while idle after a completed session keeps only the
last trace as required by prompt 3.5. The model resets between visitors, so no PSV confidence is
carried over. A lost live feed freezes the last verified view but adds an unmistakable hall-visible
marker within a couple of seconds.

**Implemented 20 September.** `web/spectator/` is a live-only, build-free WebSocket client. Host
state alone selects the segment and progress; accepted beat messages add one trace sample at their
scheduled `t_play`; the heart-rate axis follows the observed data; and valence remains neutral,
zero-confidence, zero-authority and labelled NOT READABLE. It has separate cold and held-trace idle
views, clears the held trace only on the next host-declared baseline, and never exposes stale live
readings in idle. A socket failure, invalid message, or 2.5 s without a state freezes the view under
a full-width red lost-feed banner and reconnects without synthesising data. Deterministic JavaScript
tests run through pytest; operating notes are in `web/spectator/README.md`.

**Visual design port, 21 September.** The component inside `docs/design/spectator-v3.html` now supplies
the layout, type, palette, uncertainty/authority bars and baseline learning fill. The live plain-JS
architecture and tests remain; no framework, build step or simulation mode was imported. Fonts are
local. Header overlap and trace overflow are fixed, with numerical and real-browser bounds tests.
The field remains a placeholder for 3.4; the v3 reveal is implemented separately in 3.5 below.

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
> Held through the 20 s reset after the session ends so they can photograph it, and then through idle (below). Large type, readable from several metres.
>
> **Hold the last trace through idle** (decided 14 September). When reset ends, the session id changes and `segment` goes to `idle`, before the attendant has spoken the close. Keep the last session's trace and its three numbers on screen through idle, and clear them only when the next session's baseline begins. Holding what was last shown is not a decision; the screen still takes segment and timing from the `state` message.
>
> The attendant's close reads its N off this screen: peaked at minus left at (docs/experience-script.md §3). Make that difference easy to read at a glance, from the same three numbers. Never show the session's `drop_bpm` as N.

**Implemented 21 September.** The extracted v3 reveal supplies the three large IBM Plex Mono
numbers, photograph heading and expanded trace, with corrected second-person copy and no framework
or simulation code. It appears when the host's resolve elapsed/nominal fields report at most 20 s
remaining. Peaked at is strictly the load-only maximum; sat down at is the first plotted baseline
reading and left at updates with resolve beats until the completed reset freezes it. Their visible
one-decimal subtraction is shown without a verdict. The right rail records the highest observed
host authority per dimension, not a client ceiling; valence stays exactly zero and NOT READABLE.
The full trace, numbers, authority history and resting reference survive reset and the new idle
session id, clearing only on the next baseline. The resting line uses `state.hr_base` only and is
absent when null, including degraded sessions. Missing history is not fabricated. Model and browser
tests cover reveal timing, elevated-baseline/load-only peak, negative/missing N, null references,
geometry and reset/idle retention. See `web/spectator/README.md` for display definitions and limits.

## 3.6 Attendant controls and crash recovery

**Implementation, 21 September:** `bridge/console.py` supplies the in-process terminal button;
`tools/launch.py` supervises the bridge and both live browser screens. Idle press arms, countdown
press cancels, running press stops through the 3 s reset. No control HTTP/WebSocket is added.
The confirmed booth has two displays: the laptop tiles task and console, while the external
spectator is full screen. See `docs/launcher.md` for the command and configuration, and
`docs/known-limits.md` for measured recovery and outstanding hardware/display verification.

> Single-button start, stop and reset for the attendant.
>
> The console is a local control on the laptop, driven through the live loop (2.9), never a client on the WebSocket link (CLAUDE.md hard rule 9): the frozen contract carries no start or stop, and no network round trip may sit between the button and a session beginning.
>
> `bridge/session.py` has the hooks: `start(now)` returns why it refused (`running`, `resetting`, `no_signal`, or `bad_time` when the loop passes a time the link cannot carry), `stop(now)` ends a run through a 3 s reset, and `schedule`, `signal_lost` and `regulate_result` are there for the console.
>
> **`regulate_result` is never the close.** The console must not show its `outcome` or `drop_bpm` as a verdict or as N, nor use either to pick a close. The machine produces no verdict. The attendant reads N, peaked at minus left at, off the trace screen (3.5, docs/experience-script.md §3).
>
> **Start arms, counts down, then fires** (decided 14 September, prompt 2.7). The press arms the button and shows a countdown to the pulse-aligned moment from `bridge/phase.py`, up to 11 s. At that moment the console calls `session.start(now)`. If start is refused then, show why and disarm. The countdown is not part of the session: `t_session` and the 4:45 hard cap count from when start fires. Log the press, the wait and the firing.
>
> **Signal loss must be unmissable on the console.** `signal_lost` turns true 6.2 s after the last accepted beat. Show it so an attendant looking elsewhere still sees it: large, coloured, and on the whole console, not a small icon. **There is no auto-stop** (decided 14 September). An armband lost for good during the capture fails the quality gate at the end of the hold, and the session goes to reset on its own (docs/known-limits.md has the exact edge). Lost later, the session runs on without a heartbeat. Load and resolve keep their lengths. Regulate takes no extension it cannot judge: a plain 75 s when HR_load is too thin, or "unjudged" 6.2 s after the last accepted beat, never before 75 s. Stopping it is the attendant's call.
>
> **Flag: `baseline_end` logs a slope-based `baseline_quality` on a failed gate.** A result that was ready but failed the gate can log `baseline_quality` 1.0 next to `outcome` "failed" (a contact loss 5 to 30 s into the capture does). Anything on the console that reads the log must go by `outcome` and `problems`, never show that value as a good baseline.
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
