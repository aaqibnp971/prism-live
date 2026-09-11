# CLAUDE.md

Context for every session in this repo. Read `docs/` before writing code that touches the engine, the message format, or audio.

---

## What this is

**Prism Live** is a four-minute seated demo. A person wears a heart rate armband, sits down, does a short attention task, and the Prism Engine composes sound in response to their body. They hear their own heartbeat throughout. At the end they see a trace of their heart rate.

It is shown at exhibitions and university stalls. It is **not a product**. No app store release, no shipping build.

**Target: 15 October 2026.** One developer. Five weeks.

---

## Hard rules

1. **Never commit to the `prism-engine` repo.** The engine is consumed as a built library through its C ABI, pinned to a tag. If something seems to require an engine change, stop and say so rather than working around it.
2. **36 Hz to 62 Hz belongs to the heartbeat layer alone.** Nothing else may put energy there. This is the demo's entire claim and it fails silently when broken.
3. **Beats are scheduled, never fired on arrival.** Every beat carries `t_play`, a time at least 300 ms in the future. Both audio and visuals render at `t_play`.
4. **Nothing allocates, locks, logs, or touches a file inside an audio callback.** Beat events cross into audio through a lock-free ring buffer.
5. **No health, wellness or efficacy claims in any user-facing string.** Describe what the system does, never what it achieves.
6. **The message contract is frozen.** See `docs/message-contract-v1.md`. A new field requires updating that document first.
7. **Clients never decide anything.** Segment, timing and authority are host decisions. A disconnected client freezes on its last state and shows a visible marker. It does not improvise.

---

## Architecture

```
Polar Verity Sense  --BLE-->  bridge (Python)
                                  |
                       beat scheduler, HRV, baseline,
                       PSV + confidence, authority,
                       session state machine
                                  |
                    +-------------+-------------+
                    |                           |
              Prism Engine                WebSocket server
           (built library, C ABI)               |
                    |                    +------+------+
              audio out (wired)          |             |
                                    task screen   spectator screen
```

**The host is this repo. The engine is a tool it calls.**

The engine never learns what a heartbeat is. The bridge computes the four PSV values and their confidences and hands them across the PSV interface to the engine's actuation input. The engine receives what it always receives.

### PSV

Four values, 0.0 to 1.0, each with its own confidence: `arousal`, `valence`, `cognitive_load`, `readiness`.

**`valence` confidence is exactly 0.0, always.** It is not obtainable from a pulse. This is deliberate and is a selling point, not a bug. Any code or UI that shows valence moving is wrong.

### Authority

`authority = min(confidence, segment_ceiling)` per dimension. Computed **once, host-side**, and sent to every client. Never recomputed by a client.

| Segment | arousal | valence | cognitive_load | readiness |
|---|---|---|---|---|
| baseline | 0.00 | 0.00 | 0.00 | 0.00 |
| load | 0.20 | 0.00 | 0.20 | 0.00 |
| regulate | 1.00 | 0.00 | 1.00 | 1.00 |
| resolve | taper entry value to 0.00 across T−45 s to T−12 s | | | |

### The four segments

| Segment | Duration | Notes |
|---|---|---|
| baseline | 45 s fixed | Engine observes, does not act |
| load | 75 s fixed | Attention task. Heart rate must rise. This segment carries the demo |
| regulate | 75 s, **adaptive**, up to +30 s | The payoff. Subtraction, not addition |
| resolve | 45 s | Last 7 s are the heartbeat alone, then a 3 s fade to silence |

**Regulate is adaptive.** Never derive segment state or progress from a local clock. Always use `segment`, `segment_elapsed_ms` and `segment_nominal_ms` from the `state` message.

---

## Language and layout

Python first. Drop to C++ only where the audio path genuinely requires it.

```
bridge/     Python. BLE, beat scheduler, HRV, PSV, authority,
            state machine, WebSocket server, logging.
            Calls the engine C ABI via ctypes.
web/        Task screen and spectator screen. Plain HTML/JS.
tools/      fake_sender, fake_receiver, synthetic RR generator.
assets/     The four audio stems.
docs/       Contract, script, sound brief, build plan.
unity/      Stretch goal. Does not exist until the audio half is done.
logs/       gitignored.
```

---

## Audio facts

- Engine is **mono float32, 48 kHz, no resampler**. A file at any other rate is rejected outright.
- Four stems, coprime loops: `bed` 19 s, `sub` 17 s, `air` 13 s, `pulse` 11 s. Exact integer sample counts.
- **Two different things are called "pulse."** The `pulse` **stem** is a musical rhythmic layer. The **per-beat layer** is the heartbeat sub-bass generated live at 44 Hz. Never confuse them in code or naming. Prefer `heartbeat_layer` for the second.
- Scene changes go through `prism_crossfade_scene`, which **blocks for the decode**. Call it ahead of the boundary on a control thread, never from a frame or audio loop. Pass `align_to_loop_boundary = 0`.

---

## Testing

- Everything is built against `tools/fake_sender` first. No armband required for most of the project.
- One recorded real session lives in `tools/fixtures/` and is the canonical input for tests.
- The beat scheduler has unit tests covering: dropped packet, doubled beat, missed beat, artefact burst, and reconnect mid-session.

---

## Out of scope, and it stays that way

No haptics. No controllers. No multiplayer. No app store build. No engine port to the headset. No personalised email. No elaborate particle work. No eye tracking, because the Quest 3S does not have it.

---

## Open questions

Answered from the engine source on 11 Sep 2026, against upstream prism-core `acbfd50`. Evidence and options are in `docs/engine-findings.md`. Nothing has been run yet. Do not guess at the ones still open. Flag them.

1. Does the engine expose a **host-settable master output gain** through the C ABI? Needed to fade to silence without an engine change.
   **No.** Fade in the host instead: own the audio device (pull model, `prism_render`) and ramp the buffer. The resolve ending needs this too, because bed and sub can't be silenced through the PSV.
2. Does `prism_crossfade_scene` **preserve loop playback phase** for a stem identical across two manifests? If it restarts, the seam becomes audible three times per session.
   **No.** The incoming scene always starts at sample 0, so an identical bed or sub restarts and is crossfaded against itself. **Needs an architectural decision before prompt 2.7.**
3. **How long does `prism_crossfade_scene` block?** Measure it. Set the pre-schedule lead to three times that.
   **Still open**, measure in prompt 2.7. It decodes every stem of the new scene, identical ones included. With `align_to_loop_boundary = 0` the crossfade starts as soon as the decode finishes, so a lead of three times the block time starts it early.
4. Can the host **inject a per-beat audio event into the engine's output**, or does it need its own audio device alongside?
   **No injection.** Mix the `heartbeat_layer` into the engine's buffer in the host's own audio callback, one device. The host then owns the −1.0 dBTP ceiling and can filter 36–62 Hz out of the engine buffer first.
5. Does the Verity Sense set the **RR-present flag** in its Heart Rate Measurement packets in our configuration?
   **Still open.** Needs the armband, prompt 1.0.

Also found, not yet reflected elsewhere in this file:

- Pin to upstream `acbfd50`; no tags exist. The only library built on this machine is older: no `prism_crossfade_scene`, and a PSV race fixed upstream. Build one from `acbfd50`.
- The PSV goes in through `prism_set_mood_override`, which takes one confidence for all four values. Send each value as `0.5 + (v − 0.5) × authority`, with confidence 1.0.
- The engine's limiter is −3 dBFS sample-peak, not −1.0 dBTP. The engine does not enforce 36–62 Hz. Authority 0 is a neutral PSV, which opens the `pulse` stem.
- The audio callback can't be Python (hard rule 4). It needs a small native shim in this repo.
