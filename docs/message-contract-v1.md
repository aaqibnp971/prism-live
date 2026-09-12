# Prism Live: Message Contract v1.1

**Status:** FROZEN as of 10 September 2026. v1.1 (13 September 2026) turns five gaps into explicit rules; nothing was added or removed, and the schema version `v` is still `1`.
**Owner:** Ridhwan
**Supersedes:** Project Plan v1 §11

Nothing is added or removed after this date without both sides updating together.

---

## 0. What changed from Plan §11, and why

| Change | Was | Now | Reason |
|---|---|---|---|
| `state` cadence | 30 s | **2 s** | The engine now infers at 2 s (Script §6.1). At 30 s the visuals get 8 updates in a whole session and 2 during regulate, so sound and picture cannot move together |
| `beat` timing | implied "now" | **`t_play`, a scheduled future time** | Audio is wired and instant, visuals cross WiFi and are not. Without a shared target time the heartbeat sound and the visual pulse land at different moments |
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
| `seq` | int | Increments per beat, per session. Lets a client spot a gap |
| `t_play` | int ms | The moment on `T_engine` when this beat should sound and flash. Always in the future when sent |
| `rr_ms` | float | The interval this beat closed, in milliseconds |
| `hr_bpm` | float | Instantaneous rate implied by `rr_ms` |
| `quality` | enum | `ok`, `interpolated`, `rejected`. `rejected` beats are sent for logging and must not be rendered |

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
  "signal": { "contact": true, "rr_accepted_pct": 0.94, "baseline_quality": 0.81 }
}
```

| Field | Notes |
|---|---|
| `t_session` | Milliseconds since the attendant pressed start. `null` when `segment` is `idle` |
| `segment` | `idle`, `baseline`, `load`, `regulate`, `resolve`, `reset` |
| `segment_elapsed_ms` / `segment_nominal_ms` | **Clients must draw progress from these, never from a local clock.** Regulate is adaptive and can run 30 s over |
| `psv` | Four values, 0.0 to 1.0 |
| `confidence` | 0.0 to 1.0 per dimension. `valence` is always exactly `0.0` (Script §6.4) |
| `authority` | `min(confidence, segment_ceiling)`. Computed once on the laptop so audio and visuals can never disagree |
| `hr_bpm` | Current heart rate. `null` before the first accepted interval |
| `hr_base` | Heart rate over the last 30 s of baseline. `null` until baseline has ended |
| `signal` | Feeds the honesty display. `contact` comes from the armband's own sensor-contact bit |

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

Record one good JSONL session in Week A and use it as the fake sender's input for the rest of the project.

---

## Changelog

| Version | Date | Change |
|---|---|---|
| 1.0 | 10 Sep 2026 | Frozen. State cadence 30 s to 2 s. Beat scheduling via `t_play`. Added `task_event`, `hello`, `clock`. Transport fixed to WebSocket. Authority moved laptop-side. |
| 1.1 | 13 Sep 2026 | Five gaps made explicit, all as already implemented in `bridge/contract.py` and `bridge/clock_sync.py`. No field added or removed; `v` stays `1`. §1: no integer past 2^53 − 1; laptop timestamps are whole ms, client times may be fractional. §2 `state`: `hr_bpm` and `hr_base` may be `null`, and when. §2 ceilings: `idle` and `reset` are 0. §2 `clock`: the median rtt covers every observed rtt, and twice the median is never under 4 ms. |
