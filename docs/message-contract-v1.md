# Prism Live: Message Contract v1.8

**Status:** FROZEN as of 10 September 2026. v1.8 (5 October 2026) adds the
host-calculated trace evidence required by the approved close; both browser clients and the host
change together. The schema version `v` remains `1` because no deployed external client exists.
**Owner:** Ridhwan
**Supersedes:** Project Plan v1 §11

Nothing is added or removed after this date without both sides updating together.

---

## 0. What changed from Plan §11, and why

| Change | Was | Now | Reason |
|---|---|---|---|
| `state` cadence | 30 s | **2 s** | The engine now infers at 2 s (Script §6.1). At 30 s the visuals get 8 updates in a whole session and 2 during regulate, so sound and picture cannot move together |
| `beat` timing | implied "now" | **`t_play`, a scheduled future time** | Wired audio and WiFi visuals share a playback target. The measured heartbeat is buffered; wired output does not mean instantaneous physiological playback |
| Direction | laptop to client only | **added client to laptop** | Plan task 3.5 requires task events to feed `cognitive_load`. There was no channel for that |
| Transport | UDP or WebSocket | **WebSocket** | One server, any number of clients, works in a browser and in Unity, no packet loss handling to write |
| Clock | not addressed | **`clock` ping/pong** | `t_play` is meaningless unless both sides agree what time it is |

---

## 1. Transport

- **WebSocket server on the booth laptop.** `ws://<laptop-ip>:8787/live`
- All messages are **JSON**, one object per frame, UTF-8.
- Every message has `type` and `v` (schema version, currently `1`).
- Clients: the task screen, the spectator screen, and later the Quest. All subscribe to the same stream. The laptop does not care how many are connected.
- **Why not UDP:** UDP means writing packet-loss and ordering handling twice, once per client. On a router you own, with four messages per second, TCP's cost is invisible and WebSocket works in a browser with no extra work.

### Numbers

- **No integer on the link is beyond ±(2^53 − 1)**, in either direction. Past that a JavaScript client cannot hold the number exactly.
- **Laptop timestamps are whole milliseconds:** `t_play`, and `t_engine` in `state` and in the `clock` pong.
- **Client times and durations may be fractional:** `t_client_sent`, `t_client`, `dwell_ms` and `split_interval_ms`. Browser clocks such as `performance.now()` are.

### Clock

All laptop timestamps are milliseconds from a single monotonic clock started at process launch. Call it `T_engine`.

Clients estimate their offset from `T_engine` with the `clock` exchange below and convert `t_play` into their own local time before scheduling anything.

---

## 2. Laptop to client

### Type `beat`

Sent once per detected heartbeat, **scheduled ahead**, not on arrival.

```json
{
  "type": "beat",
  "v": 1,
  "session": "S-20260915-0042",
  "seq": 318,
  "t_play": 184320,
  "rr_ms": 812,
  "hr_bpm": 73.9,
  "quality": "ok"
}
```

| Field | Type | Meaning |
|---|---|---|
| `session` | string | Session id. Changes on every reset. Never a person's name |
| `seq` | int | Increments per beat, per session. Lets a client spot a gap. `beat` has its own counter, separate from `state`'s |
| `t_play` | int ms | The moment on `T_engine` when this beat should sound and flash. Always in the future when sent |
| `rr_ms` | float | The interval this beat closed, in milliseconds |
| `hr_bpm` | float | Instantaneous rate implied by `rr_ms` |
| `quality` | enum | `ok`, `interpolated`, `rejected`. `rejected` beats are sent for logging and must not be rendered |

**Sequence rule.** `beat` and `state` each keep their own `seq`, per session. A gap is a missing number within one message type in one session. A new session starts both counters again, and that is not a gap. Rejected beats are sent, so they take a number like any other beat.

**Scheduling rule.** The laptop chooses `t_play` at least **300 ms** in the future. Both the audio thread and every client render at `t_play`, not on receipt. A client that receives a `beat` whose `t_play` has already passed drops it silently and does not catch up.

### Type `state`

Sent every **2000 ms**, and additionally at every segment boundary.

```json
{
  "type": "state",
  "v": 1,
  "session": "S-20260915-0042",
  "seq": 92,
  "t_engine": 184000,
  "t_session": 121400,
  "segment": "regulate",
  "segment_elapsed_ms": 1400,
  "segment_nominal_ms": 75000,
  "psv":        { "arousal": 0.61, "valence": 0.50, "cognitive_load": 0.44, "readiness": 0.55 },
  "confidence": { "arousal": 0.88, "valence": 0.00, "cognitive_load": 0.71, "readiness": 0.63 },
  "authority":  { "arousal": 0.88, "valence": 0.00, "cognitive_load": 0.71, "readiness": 0.63 },
  "hr_bpm": 96.2,
  "hr_base": 71.0,
  "signal": { "contact": true, "rr_accepted_pct": 0.94, "baseline_quality": 0.81 },
  "trace": {
    "at_rest_bpm": 71.0,
    "highest_task_bpm": 80.4,
    "after_task_bpm": 74.2,
    "spoken_n_bpm": 6.2,
    "average_30s": [
      { "t_play": 43120, "hr_bpm": 72.1 },
      { "t_play": 43620, "hr_bpm": null },
      { "t_play": 44120, "hr_bpm": 72.4 }
    ]
  }
}
```

| Field | Notes |
|---|---|
| `seq` | Increments per `state`, per session. `state` has its own counter, separate from `beat`'s |
| `t_session` | Milliseconds since start fired, which is when baseline began. The attendant console's countdown to the pulse-aligned moment, up to 11 s, comes before start fires and is not part of the session. `null` when `segment` is `idle`, the countdown included |
| `segment` | `idle`, `baseline`, `load`, `regulate`, `resolve`, `reset` |
| `segment_elapsed_ms` / `segment_nominal_ms` | **Clients must draw progress from these, never from a local clock.** Regulate is adaptive and can run 30 s over. Baseline can run up to 12 s over while the laptop waits for `hr_base`, so `segment_elapsed_ms` can exceed `segment_nominal_ms` in baseline too. Idle has no length: `segment_elapsed_ms` and `segment_nominal_ms` are both 0 there, so draw no progress. Reset lasts 20 s after a session that ran to the end, while the trace is photographed, and 3 s after a stop or a failed baseline gate; `segment_nominal_ms` says which. The session id changes when reset ends |
| `psv` | Four values, 0.0 to 1.0 |
| `confidence` | 0.0 to 1.0 per dimension. `valence` is always exactly `0.0` (Script §6.4) |
| `authority` | `min(confidence, segment_ceiling)`. Computed once on the laptop so audio and visuals can never disagree |
| `hr_bpm` | Current heart rate. `null` before the first accepted interval |
| `hr_base` | Heart rate over the last 30 s of baseline. `null` until baseline has ended. **A degraded session keeps it `null` to the end:** when the baseline result does not come within 12 s of the baseline window closing, load starts without it, and every later `state` in that session sends `hr_base` `null` and `signal.baseline_quality` 0.0 |
| `signal` | `contact` is a deprecated diagnostic, **never wear evidence**. Legacy HRM may forward its bit; unknown/unreliable contact (including all Verity Sense PPI) sends `false`. Neither value may label the armband worn or unworn, or gate PPI confidence. `baseline_quality` is 0.0 through baseline and its hold, and takes the baseline result's value from the first `load` message. Nothing uses it during baseline: the baseline visual is driven by confidence (VR handoff §9) |
| `trace` | Host-calculated evidence for the trace reveal. Its exact rules are below. Clients display it and never derive, substitute or reclassify these values. |

#### Host-calculated trace evidence

`trace.at_rest_bpm`, `trace.highest_task_bpm` and `trace.after_task_bpm` are nullable,
one-decimal heart rates. Every eligible window uses accepted, non-bootstrap intervals on their
**reconstructed measurement timeline**, never their delayed playback time:

```
mean bpm = 60,000 * interval count / sum(interval milliseconds)
```

Every 30 s window requires at least 22.5 s of interval coverage. If it does not have that
coverage, its value is `null`; the host never substitutes a neighbouring window.

- `at_rest_bpm` is baseline seconds 15–45.
- `highest_task_bpm` is the highest eligible 30 s window wholly inside load, with candidate
  window ends on the 500 ms grid anchored to load entry.
- `after_task_bpm` is always regulate seconds 45–75. A regulate extension never moves this
  window to a more favourable endpoint.
- `spoken_n_bpm` is `highest_task_bpm - after_task_bpm`, subtracting the displayed one-decimal
  values. It is non-null only when all three cards exist, the sustained heart-rate activation
  test passed, and the fall is at least 3.0 bpm. Otherwise it is `null` and no N is shown.

The host waits until the source-specific packet-settlement interval has passed each window end.
In particular, `after_task_bpm` stays `null` until the last intervals from regulate second 75
have arrived. With measured PPI that occurs early in resolve, before the reveal at resolve T−20 s.

`trace.average_30s` is the host-generated 30 s average line. It uses the same estimator and
22.5 s coverage rule at 500 ms steps. Each point has exactly `t_play` and `hr_bpm`. A null
`hr_bpm` is an explicit line break for insufficient coverage. The calculation window ends on
reconstructed measurement time; `t_play` is that end shifted by the source's host-known playback
buffer so it overlays the scheduled beat trace. The browser uses this supplied coordinate and
must not subtract or guess a buffer. Points are strictly increasing and the complete history so
far is repeated in each state, allowing a reconnecting spectator to recover the averaged line.

**Segment ceilings**, applied laptop-side before sending:

| Segment | arousal | valence | cognitive_load | readiness |
|---|---|---|---|---|
| `idle` | 0.00 | 0.00 | 0.00 | 0.00 |
| `baseline` | 0.00 | 0.00 | 0.00 | 0.00 |
| `load` | 0.20 | 0.00 | 0.20 | 0.00 |
| `regulate` | 1.00 | 0.00 | 1.00 | 1.00 |
| `resolve` | taper entry value to 0.00 linearly across T−45 s to T−12 s | | | |
| `reset` | 0.00 | 0.00 | 0.00 | 0.00 |

Nothing acts outside a session, so `idle` and `reset` are 0 on every dimension.

### Type `clock`

```json
{ "type": "clock", "v": 1, "role": "pong", "t_client_sent": 40219, "t_engine": 184001 }
```

Client sends `{"type":"clock","v":1,"role":"ping","t_client_sent":<local ms>}`.
Laptop replies immediately with `role:"pong"`, echoing `t_client_sent` and adding `t_engine`.

Client computes:

```
rtt    = t_client_now - t_client_sent
offset = t_engine + rtt/2 - t_client_now
```

Keep a rolling median of the last 9 offsets. Discard any sample whose `rtt` is more than twice the median rtt. Ping every 2 seconds.

Two rules keep the estimate from getting stuck:

- **The median rtt is taken over the last 9 rtts observed, discarded samples included.** Over kept samples only, a lasting rise in network delay would get every later sample discarded, forever.
- **"Twice the median rtt" is never less than 4 ms.** On localhost the median rtt is a fraction of a millisecond, and twice nearly nothing would discard almost every sample.

**An estimate belongs to one connection.** `T_engine` starts again at 0 whenever the laptop's process restarts, so an estimate from before a reconnect can be wrong by the laptop's whole uptime. A client builds a new estimate on every connect and never carries one across a reconnect. A new estimate is usable after its first pong.

To schedule a beat locally: `t_local = t_play - offset`.

---

## 3. Client to laptop

### Type `task_event`

Sent by whichever surface is running the load task.

```json
{
  "type": "task_event",
  "v": 1,
  "session": "S-20260915-0042",
  "t_client": 40530,
  "event": "miss",
  "dwell_ms": 480,
  "split_interval_ms": 3100,
  "difficulty": 0.62
}
```

| `event` | When |
|---|---|
| `split` | The object divided |
| `lock` | Dwell completed on the correct half |
| `miss` | Dwell completed on the wrong half, or the split interval expired with no lock |
| `abandon` | Reticle left the target before the dwell completed |

`difficulty` is 0.0 at the start of the load segment and 1.0 at the end, so the laptop can weight events without knowing the ramp.

### Type `hello`

Sent once on connect.

```json
{ "type": "hello", "v": 1, "client": "task-screen", "build": "0.4.2" }
```

`client` is one of `task-screen`, `spectator`, `quest`. The laptop logs it and refuses any client whose `v` does not match.

---

## 4. Rules

1. **Nothing else goes over this link.** If a field is needed, it goes through Ridhwan and both sides update together.
2. **Clients never decide anything.** Segment, timing and authority are laptop decisions. A client that loses the connection freezes on its last known state and shows a visible disconnected marker. It does not improvise.
3. **`t_play` is always honoured, never approximated.** This is the one field the whole demo's credibility rests on.
4. **Every message is logged to disk as received**, JSONL, one file per session, keyed by `session` and never by a person's name.
5. **Version bumps are breaking.** If `v` changes, every client changes the same day.

---

## 5. Build against fakes first

Two throwaway tools, both worth an hour:

- **`fake_sender`** replays a recorded JSONL session at real speed. Every client is built and tested against this, with no armband and no engine.
- **`fake_receiver`** connects to the real laptop and prints every message with the wall-clock delta between `t_play` and arrival. This is how you verify scheduling headroom is real before anything renders.

Record one good **synthetic** JSONL session in Week A and use it as the fake sender's input for
the rest of the project. Real physiological recordings and derived session logs stay local in
gitignored storage; they are never committed fixtures or required test inputs.

---

## Changelog

v1.8 (5 October 2026): `state` adds the required `trace` object. The host now owns all three
30-second card averages, the activation-gated spoken N and the coverage-broken 30-second average
line. Calculations use reconstructed measurement time; clients receive plot coordinates and do
no playback-buffer arithmetic. The fixed ending window is regulate seconds 45–75, even when
regulate extends. Both browser clients and the host update together; wire `v` remains `1` because
there is no deployed external v1 client.

v1.7 (23 September 2026): no schema change. The MQTT PPI route schedules accepted measured
intervals with a 12 s reconstructed-time buffer; it never creates interpolated replacement
beats. `rr_ms` remains the original measured interval, even across a rejected or skipped beat;
`t_play` is playback time, not acquisition time. Gentle phase correction can slightly change
playback spacing. PPI has no per-sample acquisition timestamp. `signal.contact` is deprecated
as wear evidence; unreliable/unknown sends false. HRV delay no longer forces a degraded
baseline when accepted-only gate and `hr_base` are available by the unchanged hold cap.

| Version | Date | Change |
|---|---|---|
| 1.0 | 10 Sep 2026 | Frozen. State cadence 30 s to 2 s. Beat scheduling via `t_play`. Added `task_event`, `hello`, `clock`. Transport fixed to WebSocket. Authority moved laptop-side. |
| 1.1 | 13 Sep 2026 | Five gaps made explicit, all as already implemented in `bridge/contract.py` and `bridge/clock_sync.py`. No field added or removed; `v` stays `1`. §1: no integer past 2^53 − 1; laptop timestamps are whole ms, client times may be fractional. §2 `state`: `hr_bpm` and `hr_base` may be `null`, and when. §2 ceilings: `idle` and `reset` are 0. §2 `clock`: the median rtt covers every observed rtt, and twice the median is never under 4 ms. |
| 1.2 | 13 Sep 2026 | Two more gaps made explicit, both as already implemented. No field added or removed; `v` stays `1`. §2 `beat` and `state`: each message type keeps its own `seq` counter per session; a gap is a missing number within one type in one session, and a new session is not a gap. §2 `clock`: an offset estimate belongs to one connection and is rebuilt on every reconnect, because `T_engine` restarts with the laptop's process. |
| 1.3 | 13 Sep 2026 | Two consequences of the end-of-baseline hold made explicit. No field added or removed; `v` stays `1`. §2 `state`: baseline, like regulate, can run past its nominal duration, by up to 12 s. A degraded session, whose baseline result did not come within 12 s, sends `hr_base` `null` and `signal.baseline_quality` 0.0 for the rest of the session. |
| 1.4 | 13 Sep 2026 | Three gaps made explicit, as implemented in `bridge/session.py` (prompt 2.4). No field added or removed; `v` stays `1`. §2 `state`: idle sends `segment_nominal_ms` 0, having no length. Reset lasts 20 s after a completed session and 3 s after a stop or a failed baseline gate, with `segment_nominal_ms` to match, and the session id changes when it ends. |
| 1.5 | 14 Sep 2026 | Two gaps made explicit, as implemented in `bridge/session.py` and `bridge/contract.py`. No field added or removed; `v` stays `1`. §2 `state`: idle sends `segment_elapsed_ms` 0 as well as `segment_nominal_ms` 0. `signal.baseline_quality` is 0.0 through baseline and its hold, and nothing uses it there. |
| 1.6 | 14 Sep 2026 | One wording brought up to date. No field added or removed; `v` stays `1`. §2 `state`: `t_session` counts from when start fires, which is when baseline begins, as `bridge/session.py` already does. The old wording, "since the attendant pressed start", predates the attendant console's countdown (prompt 2.7), from when the press and the start were the same moment. The countdown of up to 11 s now sits before start fires, is not part of the session, and sends `idle`. |
| 1.7 | 23 Sep 2026 | No schema change. Documented buffered measured-PPI playback and deprecated `signal.contact` as wear evidence. |
| 1.8 | 5 Oct 2026 | Added host-owned `state.trace`: three fixed 30 s averages, activation-gated spoken N, and the host-calculated 30 s average line. Wire `v` stays `1`; host and both browser clients update together before deployment. |
