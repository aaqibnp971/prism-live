# CLAUDE.md

Context for every session in this repo. Read `docs/` before writing code that touches the engine, the message format, or audio.

---

## What this is

**Prism Live** is a four-minute seated demo. A person wears a heart rate armband, sits down, does a short attention task, and the Prism Engine composes sound in response to their body. They hear their own heartbeat throughout. At the end they see a trace of their heart rate.

It is shown at exhibitions and university stalls. It is **not a product**. No app store release, no shipping build.

**Target: 15 October 2026.** One developer. Five weeks.

---

## Hard rules

1. **Never commit to the `prism-engine` repo.** The engine is consumed as a built library through its C ABI, pinned to upstream commit `acbfd50` until a tag exists. If something seems to require an engine change, stop and say so rather than working around it.
2. **36 Hz to 62 Hz belongs to the heartbeat layer alone.** Nothing else may put energy there. This is the demo's entire claim and it fails silently when broken.
3. **Beats are scheduled, never fired on arrival.** Every beat carries `t_play`, a time at least 300 ms in the future. Both audio and visuals render at `t_play`.
4. **Nothing allocates, locks, logs, or touches a file inside an audio callback.** Beat events cross into audio through a lock-free ring buffer.
5. **No health, wellness or efficacy claims in any user-facing string.** Describe what the system does, never what it achieves.
6. **The message contract is frozen.** See `docs/message-contract-v1.md`. A new field requires updating that document first.
7. **Clients never decide anything.** Segment, timing and authority are host decisions. A disconnected client freezes on its last state and shows a visible marker. It does not improvise.
8. **prism-live owns the audio device.** The engine is pulled with `prism_render` from prism-live's own audio callback, in `native/`; `prism_device_start` and `prism_device_stop` are never called. That callback high-passes the engine buffer (10th-order Chebyshev II: at least 30 dB down from 62 Hz, within 1 dB from 69.35 Hz, so the sub's 73.4 Hz stays), mixes the heartbeat layer, applies the session gain, and true-peak limits to −1.0 dBTP as the last stage. The engine's own limiter is −3 dBFS sample-peak and enforces neither the ceiling nor the 36 to 62 Hz reservation.
9. **The attendant console is a local control on the laptop, never a WebSocket client.** The contract has no start or stop message, and a network round trip must not sit between the button and a session beginning. The console arms start, counts down to the pulse-aligned moment and calls `session.start` in the bridge's own loop (prompts 2.7, 2.9, 3.6).

---

## Architecture

```
Polar Verity Sense  --BLE-->  bridge (Python)
                                  |
                       beat scheduler, HRV, baseline,
                       PSV + confidence, authority,
                       PSV source (body or pose),
                       session state machine
                                  |
                    +-------------+-------------+
                    |                           |
              Prism Engine                WebSocket server
           (built library, C ABI)               |
                    |                    +------+------+
           native audio shim             |             |
      (owns the device, prism_render,  task screen   spectator screen
       62 Hz high-pass, heartbeat layer,
       session gain, true-peak limiter)
                    |
              audio out (wired)
```

**The host is this repo. The engine is a tool it calls.**

The engine never learns what a heartbeat is. Its only PSV input is `prism_set_mood_override`: four values and **one** confidence shared by all four. There is no per-dimension confidence input. The bridge therefore pre-blends: it sends each value as `0.5 + (v − 0.5) × authority`, valence as 0.5, confidence 1.0 and `mode_hint` NULL. The engine reads confidence only as `effective = 0.5 + (value − 0.5) × confidence`, so this reproduces per-dimension authority exactly. The engine's inference thread is never started (`prism_start` is not called); the bridge is the only PSV writer. Never read authority or confidence back from `prism_get_psv`; the `state` message is the only source.

### PSV

Four values, 0.0 to 1.0, each with its own confidence: `arousal`, `valence`, `cognitive_load`, `readiness`.

**`valence` confidence is exactly 0.0, always.** It is not obtainable from a pulse. This is deliberate and is a selling point, not a bug. Any code or UI that shows valence moving is wrong.

**Two PSV sources, switchable at runtime.** `body`: the authority-scaled values above; authority 0 is a neutral PSV, which is not silence. `pose`: a designed effective PSV per segment, with the body moving arousal inside a range. Which one ships is decided by listening in Week B (prompt 2.5), not on paper.

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
| baseline | 45 s capture, then held to 56 s, when load starts on a pulse boundary (prompts 2.4, 2.7) | Engine observes, does not act |
| load | 75 s fixed | Attention task. Heart rate must rise. This segment carries the demo |
| regulate | 75 s, **adaptive**, up to +30 s | The payoff. Subtraction, not addition |
| resolve | 45 s | Last 7 s are the heartbeat alone, then a 3 s fade to silence |

**Regulate is adaptive.** Never derive segment state or progress from a local clock. Always use `segment`, `segment_elapsed_ms` and `segment_nominal_ms` from the `state` message.

---

## Language and layout

Python first. C++ only in `native/`, because the audio callback cannot be Python (hard rule 4).

```
bridge/     Python. BLE, beat scheduler, HRV, PSV, authority,
            state machine, WebSocket server, logging.
            Calls the engine C ABI and the audio shim via ctypes.
native/     C++. The audio shim: owns the device, calls prism_render,
            62 Hz high-pass, heartbeat layer, session gain, true-peak
            limiter. The only audio callback in the project. Hard rule 8.
web/        Task screen and spectator screen. Plain HTML/JS.
            native/bin/ holds the built shim DLL, committed.
tools/      fake_sender, fake_receiver, synthetic RR generator.
vendor/     lib/libprism_core.dll, the engine built from acbfd50, committed
            (recipe in vendor/lib/README.md). prism-core/ is its source
            clone, gitignored.
assets/     The four audio stems.
docs/       Contract, script, sound brief, build plan.
unity/      Stretch goal. Does not exist until the audio half is done.
logs/       gitignored.
```

---

## Audio facts

- Engine is **mono float32 with no resampler**. It takes its rate from the first stem and rejects a stem at any other rate. This project's rate is **48 kHz**: every stem is delivered at 48 kHz, and the bridge refuses to start unless `prism_sample_rate()` is 48000.
- Four stems, coprime loops: `bed` 19 s, `sub` 17 s, `air` 13 s, `pulse` 11 s. Exact integer sample counts. No `lead`.
- **One scene for the whole session.** All four stems in one manifest scene, the `default_scene`. `prism_crossfade_scene` is never called: an incoming scene restarts every stem from sample 0, which puts an audible seam at every boundary. Segments differ only through the PSV.
- **Two different things are called "pulse."** The `pulse` **stem** is a musical rhythmic layer. The **per-beat layer** is the heartbeat sub-bass generated live at 44 Hz, in `native/`, never in the engine. Never confuse them in code or naming. Prefer `heartbeat_layer` for the second.
- **Stem gates follow the filter, not the clock.** The engine turns the PSV into a cutoff and per-stem gains through fixed curves the host cannot change. `pulse` sounds only at density ≥ 0.35 and `air` at ≥ 0.55, on one density number tied to the cutoff, so air can never sound while pulse is closed. A gate changes only at that stem's next loop boundary (every 11 s for pulse, 13 s for air), then fades over a fixed 1.5 s. The bridge tracks the engine's phase from frames rendered and times its PSVs against it. Numbers: `docs/engine-findings.md`.
- The engine's limiter is −3 dBFS sample-peak. It does not enforce −1.0 dBTP and does not keep 36 to 62 Hz clear. prism-live does both (hard rule 8).

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

Questions 1 to 4 were answered from the engine source on 11 Sep 2026, against upstream prism-core `acbfd50`. Evidence and options: `docs/engine-findings.md`. Nothing has been run yet.

Closed:

1. Does the engine expose a **host-settable master output gain** through the C ABI? **No.** prism-live fades its own buffer (hard rule 8). The resolve ending needs this too, because bed and sub cannot be silenced through the PSV.
2. Does `prism_crossfade_scene` **preserve loop playback phase** for a stem identical across two manifests? **No.** The incoming scene always starts at sample 0. Decision: one scene, no crossfades.
3. **How long does `prism_crossfade_scene` block?** **Moot.** It is never called. `prism_load_scene` blocks once per handle at startup; prompt 2.7 measures that.
4. Can the host **inject a per-beat audio event into the engine's output**? **No.** The heartbeat layer is mixed in prism-live's own callback, one device (hard rule 8).
7. **Where the `acbfd50` engine library gets built.** **Closed 14 Sep 2026.** It is built from upstream `acbfd50` and committed at `vendor/lib/libprism_core.dll`, with the recipe, toolchain, flags and SHA-256 in `vendor/lib/README.md`. The build is reproducible. The engine source clone at `vendor/prism-core/` is gitignored.

Still open. Do not guess at these. Flag them.

5. Does the Verity Sense set the **RR-present flag** in its Heart Rate Measurement packets in our configuration? Needs the armband, prompt 1.0. The same run answers two more: does it report **sensor contact** at all, and does it keep sending RR intervals while contact reads false? `bridge/psv.py` reads a sensor that reports no contact as in contact, and keeps intervals sent without contact out of heart rate and HRV.
6. **Body-derived or designed-pose PSV?** Under body-derived values the script's regulate comes out inverted (`docs/engine-findings.md`). Decided by listening in Week B, prompt 2.5.
