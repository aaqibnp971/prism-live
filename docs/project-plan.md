# **Prism Live: Project Plan v1.2**

**Owner:** Ridhwan **Team:** Dev A (laptop side), Dev B (Unity side) **Target:** internal showing, Thursday 15 October 2026 **Status:** planning complete, build starts Week 1 **Version date:** 13 September 2026

---

# **PART ONE: THE PLAIN VERSION**

For marketing, ops, and anyone who needs to understand what we are building and why.

## **1\. What this is**

A four-minute seated experience. A person puts on an armband and a VR headset, sits down, and the Prism Engine reads their body and composes sound and visuals in response to it. By the end, they have felt their own state change, and they know it happened because of what they heard.

It is a demo, not a product. It exists to make the Prism Engine real to people who would otherwise only hear us describe it.

## **2\. Where it gets used**

* Exhibitions, in front of investors and anyone worth networking with  
* University stalls, to pull students to the R13 stand  
* Anywhere we need the sound engine to stop being an abstraction

After someone experiences it, the close is: the same technology runs in our app, and it runs in Prism Venues, in different forms.

## **3\. What has to be true for this to have worked**

**Primary:** the person notices a change in themselves and can say what it was, without us prompting them.

We measure this literally. After every test run in Week 5, we ask one open question: "Did you notice anything change in you, and what?" We write down exactly what they say. If most people cannot name something, the demo has failed and we fix it before we show anyone.

**Secondary:** people stop at the stall, watch, talk to us, and scan the card.

## **4\. What the person actually experiences**

| Time | What happens | What they feel |
| ----- | ----- | ----- |
| 0:00 to 0:45 | Neutral field. They sit and look around. The system learns their normal. | Settling. Nothing dramatic. |
| 0:45 to 2:00 | A light attention task. Something drifts, splits, they follow it. It gets harder. | Focused, slightly pushed, heart rate up |
| 2:00 to 3:15 | The system takes over and works on them. Sound and visuals shift together. | The pull down. This is the moment. |
| 3:15 to 4:00 | It resolves. Their own trace appears. | "That was me." |

Then 20 seconds where their trace stays on the big screen and they photograph it themselves, then a 45-second close from whoever is running the stall.

## **5\. The one thing that makes this honest**

Someone walks off a loud exhibition floor into a quiet dark chair with good headphones. They will feel calmer. That proves nothing, and the first sharp person to point it out damages us.

So we do two things.

**We make them go up before we bring them down.** The task raises their heart rate on purpose. What they feel afterwards is a swing, not just a quiet room.

**We let them hear their own heartbeat.** Their pulse drives a low tone in the sound and a matching pulse in the visuals, in real time. They hear it fast at the start. They hear it slow by the end. Nothing to take on faith, no claim to make, no instrument needed. The medium is the evidence.

That second layer is the difference between "nice ambient music" and "that thing was reading me."

## **6\. What we are NOT building**

Every item here is a launch-date killer if it creeps back in.

1. Not a product. No app store release, no standalone Quest build.  
2. No health, wellness, or efficacy claims of any kind. Marketing tier only, per the claims policy. We describe what the system does. We never say what it achieves.  
3. Single user. No multiplayer, no shared sessions.  
4. No haptics in v1. Seat transducer is a v1.1 add and costs one day when we want it.  
5. No engine port to the headset. The engine runs on a laptop.  
6. No controllers. Reticle only.  
7. No personalised email or printed trace. They photograph the screen.  
8. No elaborate particle work. See section 12\.

## **7\. Budget**

| Item | AED | Note |
| ----- | ----- | ----- |
| Meta Quest 3S 128GB | 1,199 | Noon, currently discounted from 1,399 |
| Polar Verity Sense armband | \~380 | The closed loop depends on this |
| 7-in-1 accessory bundle for Quest 3S | 85 | Silicone face interface, covers, hygiene |
| Hard carrying case | 74 | Transport to events |
| Sony WH-1000XM5 | 0 | Already owned |
| Laptop | 0 | Existing |
| Second Polar Verity Sense | \~380 | Single point of failure for the closed loop. One is worn while the other charges |
| Travel router, 5 GHz | \~200 | Exhibition halls destroy 2.4 GHz WiFi. Never use venue WiFi |
| Long USB-C cable and power bank | \~200 | Quest 3S runs about 2.5 h. A 50-person day is over 4 h of headset-on time |
| Spare XM5 ear pads | \~150 | Leather absorbs sweat across dozens of users. Flagged in §12, never budgeted |
| Gaffer tape, cable management | \~30 | Someone who cannot see the floor, a headphone cable and a power cable |
| **Committed total** | **\~2,698** | Was \~1,738. Roughly USD 735 |

Reserve, not yet committed:

| Item | AED | Trigger |
| ----- | ----- | ----- |
| Paid shader help, 2 to 3 days | 0 to 1,800 | Only if Dev B hits a wall in Week 3 |
| Disposable VR face covers, 100pc | \~60 | Before first public event |
| Printed cards with QR | \~200 | Before first public event |
| Spectator monitor | borrow or rent | At the venue |

Buy the headset, armband, accessories and case this week. Everything else waits for a real event.

**Note on entities.** The kit should be bought through whichever entity is based where the demo will live. India Pvt Ltd and the Masdar City FZ LLC have no nexus, so kit belonging to one should not quietly become the other's asset. If the demo is built in one country and shown in the other, check the customs position before booking travel.

## **8\. Running it at a stall**

Two people per shift, and the second one is the important one.

| Role | Job |
| ----- | ----- |
| **Fitter** | Armband, headphones, headset, hygiene wipe between users, restart if anything breaks |
| **Narrator** | Stands at the big screen, talks the crowd through what they are seeing live, takes the conversations, hands out cards |

Only about 50 people wear the headset in a day. Several hundred walk past the screen. The narrator is doing the commercial work. That is a marketing person, not a developer.

**Per-person cycle: about 7 minutes.**

1. Greet, exclusion questions and consent line, then armband on upper arm, 20 seconds. The armband goes on before the pitch.  
2. 20-second pitch, with the armband already reading  
3. Headphones, then headset, 30 seconds  
4. Four-minute run  
5. 20 seconds of trace on screen, they photograph it  
6. 45-second close, card with QR

**Settling, and why the armband goes on before the pitch.** People arrive off a loud floor with their heart rate still falling. A baseline measured while it is still falling steeply gives the system nothing to trust for the rest of that person's run. The baseline starts 70 seconds after the greet and ends 115 seconds after it, inside the same 7-minute cycle. Fitting the armband before the pitch does not change those times. It means the armband is reading for about 50 seconds before the baseline instead of 30, so a poor fit can show up before the headset goes on.

## **9\. Open items**

| Item | Owner | Needed by |
| ----- | ----- | ----- |
| Which event is first | Ridhwan | Week 4, so booth items can be ordered |
| Purchasing entity | Ridhwan with Abdul Kader | This week |
| Which two developers, and what comes off their plates | Ridhwan | This week |
| QR landing page live | Marketing | Week 5 |
| Both closing scripts written | Ridhwan with Mariah | Week 3 |

---

# **PART TWO: THE TECHNICAL VERSION**

## **10\. Architecture**

The engine does not go on the headset. The headset is a thin visuals client.

| Component | Runs on |
| ----- | ----- |
| Prism Engine C++ core, PSV emission, confidence-weighted actuation | Booth laptop |
| BLE ingest from Polar Verity Sense | Booth laptop |
| Audio generation and output | Booth laptop, XM5 wired into it |
| Per-beat pulse layer | Booth laptop |
| Spectator screen | Booth laptop, second display output |
| Visuals, session flow, reticle task | Quest 3S, Unity, over local WiFi |

This avoids three things that would each cost a week or more: an ARM64 Android build of the core, a Unity native plugin bridging the C ABI on device, and Bluetooth peripheral access on Quest, which is restricted and unreliable.

The cost of this decision: we do not harden the OEM SDK path as a side effect. Accept it. Six weeks does not have room.

The person is tethered to the laptop by a headphone cable. This is a seated demo, so that is fine, and it removes all wireless audio latency from the picture.

## **11\. Message contract, frozen by end of Week 1 day two**

**SUPERSEDED as of 10 September 2026 by docs/message-contract-v1.md. Do not build against this section.**

Two message types over UDP or WebSocket on a local router. Both devs build against a fake sender and a fake receiver and do not block each other until Week 4\.

**Type 1: `beat`** (sent per detected heartbeat)

* timestamp  
* instantaneous interval  
* current heart rate

**Type 2: `state`** (sent every 30 seconds, the PSV cadence)

* arousal, valence, cognitive\_load, readiness  
* confidence per dimension  
* authority per dimension  
* session segment (baseline, load, regulate, resolve)  
* elapsed session time

Nothing else goes over this link. If either dev needs a field added, it goes through Ridhwan and both sides update together.

## **12\. Hardware notes**

**Sony WH-1000XM5.** Keep them, with two conditions.

* **Wired only.** Bluetooth latency would break the per-beat layer against the visuals. Use the 3.5mm cable into the laptop.  
* **Do not tune the sound design on them.** They are consumer-tuned with a bass shelf and DSP in the chain. What the person hears is not exactly what the engine generated. Author and tune on flat reference monitoring, then verify on the XM5.

The upside is real: active noise cancellation in an exhibition hall beats passive isolation by a wide margin, and hall noise is the single biggest threat to an audio demo. Keep them powered on so ANC is active.

Watch the ear pads. Leather absorbs sweat across dozens of users. Wipe between users, and buy replacement pads before the first multi-day event.

**Meta Quest 3S.** Fine for this, with one art-direction consequence.

The 3S uses Fresnel lenses rather than the pancake optics in the Quest 3\. Lower clarity, smaller sweet spot, and noticeably more glare on small bright elements against near-black backgrounds. That is exactly the content a dark ambient scene would otherwise use.

So: no pinpoint bright sources on near-black. Favour mid-luminance fields, soft gradients, and large diffuse light rather than small bright ones. Design to the lens.

Same Snapdragon XR2 Gen 2 as the Quest 3, so performance headroom is unchanged. 128GB is far more than this app needs. The lack of a headphone jack on the headset is irrelevant here since audio comes from the laptop.

**Visual direction.** One colour field, volumetric fog, a single diffuse light source, a horizon, slow gradient shifts. Everything driven by the actuation parameters. Hold 90fps.

Transparent overdraw is the number one Quest performance killer, so heavy particle work is both the thing a small team does badly and the thing that tanks frame rate. Restraint reads as premium. Spectacle done badly reads as a student project. This is not a compromise, it is the correct choice.

## **13\. Full task list**

**SUPERSEDED as of 10 September 2026 by docs/solo-build-plan.md. This task list assumed two developers over six weeks. Do not follow it.**

### **Week 0: this week, 2 to 6 September**

| \# | Task | Owner |
| ----- | ----- | ----- |
| 0.1 | Order Quest 3S, Verity Sense, accessory bundle, carrying case | Ridhwan |
| 0.2 | Confirm purchasing entity with Abdul Kader | Ridhwan |
| 0.3 | Confirm both devs and what comes off their current workload | Ridhwan |
| 0.4 | Draft the message contract, circulate to both devs | Ridhwan |
| 0.5 | Build and sideload a hello-world APK to a Quest. One day, hard gate. | Dev B |
| 0.6 | Confirm Verity Sense BLE reads on the laptop, can start before headset arrives | Dev A |

**0.5 is the most important task in this document.** Dev B has an unknown level of Quest experience and we can only find that out by trying. If that day goes badly, we know in Week 0 instead of Week 3\.

### **Week 1: 7 to 13 September. Signal.**

| \# | Task | Owner |
| ----- | ----- | ----- |
| 1.1 | Freeze the message contract, day two, no changes after | Ridhwan |
| 1.2 | Stable BLE stream: heart rate plus RR intervals | Dev A |
| 1.3 | Reconnect handling. Armband dropouts are common and must degrade gracefully | Dev A |
| 1.4 | RR intervals to HRV features, rolling 60-second window | Dev A |
| 1.5 | 45-second per-person baseline capture | Dev A |
| 1.6 | Arousal and readiness mapping from HR delta and HRV against baseline | Dev A |
| 1.7 | Confidence model. Rises with signal quality and window fill. Valence confidence sits near floor by design. | Dev A |
| 1.8 | Per-session logging to disk | Dev A |
| 1.9 | Unity project set up, Meta XR SDK, build pipeline working | Dev B |
| 1.10 | Network receiver parsing both message types from a fake sender | Dev B |
| 1.11 | Base scene: seated camera rig, fog volume, one light, driven by fake params | Dev B |
| 1.12 | Write the four-minute arc segment by segment in words | Ridhwan |
| 1.13 | Art direction in Claude Design: colour language per segment, 6 to 8 storyboard frames, push to Figma | Ridhwan |

### **Week 2: 14 to 20 September. Closed loop, audio only. GO/NO-GO.**

| \# | Task | Owner |
| ----- | ----- | ----- |
| 2.1 | Wire PSV output into the engine actuation input via the C ABI | Dev A |
| 2.2 | Apply confidence-weighted authority per dimension | Dev A |
| 2.3 | Per-beat pulse layer: heartbeat triggers a sub-bass event, on a separate path from PSV | Dev A |
| 2.4 | Audio output path to the laptop jack | Dev A |
| 2.5 | Session state machine: idle, baseline, load, regulate, resolve, reset | Dev A |
| 2.6 | Publish the spectator feed at 10Hz: PSV, confidence, authority, heart rate | Dev A |
| 2.7 | Switch from fake sender to Dev A's real link, both message types | Dev B |
| 2.8 | Visuals respond at both rates: slow field changes, per-beat pulse | Dev B |
| 2.9 | Session segment received and reflected in the scene | Dev B |
| 2.10 | Spectator screen design in Claude Design, push to Figma | Ridhwan |
| 2.11 | **Go/no-go call, end of week** | Ridhwan |

**Gate:** the loop must be closed on the laptop with a real armband, audio working, spectator data flowing. If it is not, we cut VR and ship the seated audio version. That decision is made on the date, not on optimism.

### **Week 3: 21 to 27 September. Task and look.**

| \# | Task | Owner |
| ----- | ----- | ----- |
| 3.1 | Gaze-dwell interaction, no controllers, no hand tracking | Dev B |
| 3.2 | Load task: drifting object splits, select by dwell, 75 seconds, difficulty ramps | Dev B |
| 3.3 | Implement art direction from the Figma frames | Dev B |
| 3.4 | Performance pass: hold 90fps, audit transparent overdraw, no bright points on near-black | Dev B |
| 3.5 | Task events feed back into cognitive\_load | Dev A |
| 3.6 | Distinct audio material for each of the four segments | Dev A |
| 3.7 | Spectator screen built as a web page reading the feed, Proof Desk reskin | Dev A |
| 3.8 | Both closing scripts, 45 seconds each, investor and student | Ridhwan with Mariah |
| 3.9 | QR landing page copy, two buttons: app, Venues | Ridhwan with Mariah |
| 3.10 | Claims review: every line of stall copy inside marketing tier | Ridhwan |

### **Week 4: 28 September to 4 October. Assemble.**

| \# | Task | Owner |
| ----- | ----- | ----- |
| 4.1 | Full four-minute run, end to end, no manual intervention | Dev A and Dev B |
| 4.2 | Single-button start, stop, reset for the attendant | Dev A |
| 4.3 | Trace reveal screen: heart rate curve, start against end, which dimensions held authority | Dev A |
| 4.4 | Crash recovery. Any component dies, attendant is back up in under 30 seconds. | Dev A and Dev B |
| 4.5 | First five internal runs on team members | All |
| 4.6 | Decide the first event and order booth-specific items | Ridhwan |

### **Week 5: 5 to 11 October. Tune on real people.**

| \# | Task | Owner |
| ----- | ----- | ----- |
| 5.1 | 20 runs on people who do not work here | All |
| 5.2 | After each run, ask the open question and write down the verbatim answer | Ridhwan |
| 5.3 | Tune the load segment until heart rate reliably rises | Dev A |
| 5.4 | Tune the regulate segment until heart rate reliably falls | Dev A |
| 5.5 | Fix whatever breaks across the 20 runs | Dev A and Dev B |
| 5.6 | QR landing page live | Marketing |

**Gate:** 20 consecutive runs with no crash, and a majority naming a change in themselves unprompted. If the second half fails, the fix is the arc, not the code.

### **Week 6: 12 to 18 October. Harden and show.**

| \# | Task | Owner |
| ----- | ----- | ----- |
| 6.1 | Noise test: 80dB crowd noise beside the chair, full run, repeat | All |
| 6.2 | Hygiene kit assembled: disposable face covers, wipes, spare ear pads | Ops |
| 6.3 | Packing list and case loadout | Ops |
| 6.4 | One-page attendant runbook | Ridhwan |
| 6.5 | Cards printed with QR | Marketing |
| 6.6 | Full dry run with the complete booth setup | All |
| 6.7 | **Internal showing, 15 October, 20 outside guests** | All |

## **14\. Risks**

| Risk | Severity | What we do |
| ----- | ----- | ----- |
| Dev B has not shipped Unity on Quest | High | Hello-world APK in Week 0, before anything depends on it |
| Integration blowup between the two halves | High | Contract frozen Week 1 day two, fake sender and receiver both sides |
| Person does not get activated in the load segment | High | This kills the whole proof. Tune in Week 5, have a harder task variant ready. |
| Armband drops mid-session | Medium | Reconnect logic, and authority falls with confidence, which is honest behaviour rather than a bug |
| Hall noise defeats the audio | Medium | ANC on, wired, and the 80dB test in Week 6 |
| XM5 colours the low end and the mix is wrong elsewhere | Medium | Author on reference, verify on XM5 |
| Fresnel glare on the 3S | Medium | No pinpoint bright sources on near-black, designed in from Week 1 |
| Scope creep | Medium | Section 6 is checked at every weekly review |
| Ridhwan's time gets eaten by other workstreams | Medium | His tasks are design, scripts and the go/no-go call. If he cannot make Week 2's call on time, the date moves. |

## **15\. Version history**

* v1, 2 September 2026, initial plan
* v1.1, 11 September 2026: §4 and §6 replace eye control with reticle control, since the Quest 3S has no eye tracking. §7 adds the change log budget items, committed total \~1,738 to \~2,698 AED. §10 renames the gaze task to the reticle task. §11 and §13 are marked superseded by the message contract and the solo build plan.
* v1.2, 13 September 2026: §8 fits the armband before the pitch, right after the exclusion questions and the consent line, and states the settling time before and through the baseline.

&nbsp;