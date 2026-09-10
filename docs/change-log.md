# Prism Live: Change Log and Errata

**Date:** 10 September 2026
**Covers:** Project Plan v1 (2 Sep) and Experience Script v1.0 (7 Sep)

Part One is the edits to apply now. Part Two is everything that has changed, in bullets.

---

# PART ONE: EDITS TO APPLY

## A. Experience Script v1 → v1.1, §2 LOAD

### Edit A1: Person does, first line

**Find:**
> Drifting object splits, select the correct half by gaze dwell, difficulty ramps.

**Replace with:**
> Drifting object splits, select the correct half by holding the reticle on it, difficulty ramps.

### Edit A2: Person does, last line

**Find:**
> No controllers, no hand tracking, eyes only.

**Replace with:**
> No controllers, no hand tracking. Reticle only, driven by head pose in VR or by pointer on the task screen. The Quest 3S has no eye tracking hardware.

### Edit A3: Attendant line

**Find:**
> Look at the one that's still moving and hold your eyes on it until it locks.

**Replace with:**
> Keep the one that's still moving in the centre and hold it there until it locks.

### Edit A4: In-headset line

**No change.** "Follow the half that's still moving" is already accurate for head or pointer control.

### Edit A5: New note, add directly under the ramp values

> **Note on the ramp.** These values were authored assuming eye control, which the Quest 3S cannot provide. What is built is head-pose or pointer control, which is harder at the same numbers because the neck or hand has to track a target moving at up to 19 °/s. Treat every value here as provisional until Week E retunes them against real runs. The extra physical effort is not a problem: it raises heart rate, which is what this segment exists to do.

## B. Project Plan v1 → v1.1

### Edit B1: §4, the experience table, row 0:45 to 2:00

**Find:**
> A light attention task. Something drifts, splits, they follow it with their eyes. It gets harder.

**Replace with:**
> A light attention task. Something drifts, splits, they follow it. It gets harder.

### Edit B2: §6, item 6

**Find:**
> No controllers. Eyes only.

**Replace with:**
> No controllers. Reticle only.

### Edit B3: §11, add at the top of the section

> **SUPERSEDED as of 10 September 2026 by Prism Live: Message Contract v1.** Do not build against this section.

### Edit B4: §13, add at the top of the section

> **SUPERSEDED as of 10 September 2026 by Prism Live: Solo Build Plan.** This task list assumed two developers over six weeks. Do not follow it.

### Edit B5: §7, add to the budget

| Item | AED | Note |
|---|---|---|
| Second Polar Verity Sense | ~380 | Single point of failure for the closed loop. Swap and charge |
| Travel router, 5 GHz | ~200 | Never use venue WiFi |
| Long USB-C cable and power bank | ~200 | Quest 3S runs ~2.5 h. A 50-person day is over 4 h of headset-on time |
| Spare XM5 ear pads | ~150 | Already flagged in §12, never budgeted |
| Gaffer tape, cable management | ~30 | A blindfolded person, a headphone cable and a power cable |
| **New committed total** | **~2,698** | Was ~1,738 |

---

# PART TWO: EVERYTHING THAT HAS CHANGED

## Superseded

- **Plan §11, the message contract** is replaced by Message Contract v1
- **Plan §13, the six-week task list** is replaced by the Solo Build Plan
- Everything else in both documents still stands

## The team changed

- Plan assumed **Dev A, Dev B and Ridhwan**, roughly eighteen person-weeks
- Reality is **one person doing all three roles**, five weeks left
- Plan open item "Which two developers" is now answered: you are both
- Plan risk "Dev B has not shipped Unity on Quest" and risk "Ridhwan's time gets eaten by other workstreams" are now the **same risk**, at roughly double severity

## The scope changed

- **VR is demoted from the plan to a stretch goal.** It starts only if the audio half is finished by 30 September
- The realistic target for 15 October is the **seated version**: armband, engine, audio, own heartbeat, load task and ambient field on a monitor, spectator screen fed live
- The load task moves from Unity to a **browser, full screen, on a monitor in front of the chair**, with the reticle on the mouse. Same mechanic, same ramp, same numbers
- The browser version becomes the specification for Unity later, already tuned on real people
- A **five-step cut ladder** now exists, so falling behind is a decision rather than a panic

## The message contract changed

- `state` cadence **30 s to 2 s**. At 30 s the headset gets eight updates in a whole session and the regulate segment gets two, so sound and picture cannot move together
- `beat` now carries **`t_play`, a scheduled future time**, instead of firing on arrival. Audio is wired and instant, visuals cross WiFi and are not
- Transport fixed to **WebSocket** rather than "UDP or WebSocket". One server, any number of clients, works in a browser, no packet-loss handling to write twice
- **NEW message `task_event`**, client to laptop. Plan task 3.5 required task events to feed `cognitive_load` and there was no channel for it. The original contract was one-directional
- **NEW message `clock`**, ping and pong, so both sides agree what time it is. `t_play` is meaningless without it
- **NEW message `hello`**, so the laptop can reject a client on the wrong schema version
- **Authority is now computed laptop-side** and sent, rather than computed independently by each client. Two clients computing it separately will disagree in front of a crowd
- **NEW fields:** `session`, `seq`, `quality`, `hr_base`, `segment_elapsed_ms`, `segment_nominal_ms`, and a `signal` block carrying sensor contact, RR acceptance rate and baseline quality
- Clients must draw progress from `segment_elapsed_ms`, **never from a local clock**, because regulate is adaptive and can run 30 s over

## New technical findings, none of which were in either document

- **The Quest 3S has no eye tracking.** Only the Quest Pro does. "Gaze dwell" and "eyes only" describe hardware you do not have
- **Bluetooth heart rate does not give you a per-beat event stream.** The standard service notifies about once per second and carries the intervals for beats that already happened. Firing a sound on packet arrival produces a 1 Hz metronome, not a heartbeat. A **beat scheduler** is needed and it is not in the plan
- **Two different things are called "pulse":** the musical `pulse` stem, and the engine's per-beat heartbeat layer. A composer reading the script cold will confuse them and put the stem in the reserved 36 to 62 Hz band
- **The master low-pass filter is the main expressive tool**, sweeping 620 Hz to 3,600 Hz. If the stems have no content in that band, the whole arc is inaudible movement. Nothing in the script says this to whoever makes the sound
- **The four scene manifests may not preserve loop phase.** §2 REGULATE requires the same loops throughout and calls it "the seam the person must not hear". If `prism_crossfade_scene` restarts identical stems, the seam becomes the most obvious thing in the demo. Unanswered
- **Nobody knows how long `prism_crossfade_scene` blocks.** The pre-schedule lead time is currently a guess
- **Quest 3S battery is about 2.5 hours.** A 50-person day is over 4 hours of headset-on time. Not addressed anywhere
- **Exhibition halls destroy 2.4 GHz WiFi.** The plan says "a local router" without specifying that it must be yours and must be 5 GHz

## Things that had no owner and now do

- **The four audio stems.** Plan task 3.6 assigned "distinct audio material" to Dev A in Week 3, after the Week 2 gate that requires audio to work. Now split: **placeholder stems this week**, real stems by 30 September, made by whoever actually makes sound
- **Sound Brief v1** exists so the composer does not have to read seventeen pages of engine internals

## The spectator display

- **v3 is a design mock with no live feed.** No `fetch`, no WebSocket. Heart rate comes from a hardcoded sine curve. Plan task **2.10 is done, task 3.7 is not started**
- Its authority arrays **contradict §2**. It shows arousal authority at 0.80 during LOAD where the script says 0.20, which tells the crowd the engine is driving the person when the script's whole point is that the task is
- Its RESOLVE readiness authority **rises** across a segment defined by everything tapering to zero
- **Valence is not a hard zero in it.** Confidence reads 0.03 and authority 0.05, against §6.4's exactly 0.0
- Its valence chip says **"NO SIGNAL"**, which implies a broken armband rather than an architectural limit
- **"HER VIEW — LIVE FROM THE HEADSET" is false.** The laptop never receives the headset's frames, only the parameter feed. It is a reconstruction, and the label is falsifiable in public
- **"GUIDING HER DOWN"** is an outcome claim and fails the marketing claims tier
- It is **written for a woman throughout**, eleven instances. Half the fifty people a day are not, and the reveal panel is the screen they photograph
- Its timeline is **hardcoded to 240 seconds**, but regulate is adaptive up to 270
- It carries a **simulation harness** with speed and profile controls, which must be compiled out of any build that goes to an event

## New risks, not in Plan §14

- §6 added roughly a week of unplanned engine work to the heaviest workload in the project
- The audio stems had no owner and no realistic date
- Clock drift between laptop and client breaks the per-beat sync, which is the whole honesty argument
- A simulation-capable build running at a booth is undetectable from the room
- The 36 to 62 Hz reservation **fails silently**. Everything sounds fine and the demo quietly stops proving anything

## Still unowned, and still needed

- **Exclusion script** at the greet: pregnancy, heart conditions, epilepsy or photosensitivity, severe motion sickness. You deliberately raise strangers' heart rates
- **Consent line** for heart rate capture, plus a retention decision for the session logs. Physiological data under UAE PDPL is a sensitive category
- **Age policy.** Meta guidance is 13+
- **20 outside test subjects** need about two weeks of lead time. Start asking in Week C, not Week E
- **First event decision.** Booth applications often close six to eight weeks ahead, so the Week 4 deadline in the plan may already be too late
- **IP assignment** for any contributor who is not employed by either entity
- **Version control setup:** Unity `.gitignore`, Git LFS from day one, Force Text asset serialization, or scene files become unmergeable
- **Purchasing entity**, Plan §9, still open since Week 0 and now blocking all hardware

## Documents that now exist

| Document | Answers |
|---|---|
| Project Plan v1.1 | What we are building and why |
| Experience Script v1.1 | What happens in the four minutes, with numbers |
| **Message Contract v1** | What the two halves say to each other |
| **Sound Brief v1** | What the four audio files must be |
| **Solo Build Plan** | What you do, in what order, for five weeks |
| **This document** | What changed and why |
