# Prism Live: Sound Brief v1

**For:** the person producing the four audio stems
**From:** Ridhwan, R13 Labs
**Date:** 10 September 2026
**Source of truth:** Prism Live Experience Script v1 §0, §2, §6. Where this brief and the script disagree, the script wins.

---

## 1. What you are making, and what you are not

You are making **four seamless loops**. You are not making a four-minute piece.

The engine plays all four at once, at gains it decides moment to moment from the person's heart rate, and sweeps a low-pass filter across the sum. The composition happens live, in software. Your job is to build the material that composition acts on.

There is no arrangement, no arc, no development. A listener hearing your four files on their own should find them slightly boring. That is correct.

**You are also not making the heartbeat.** The heartbeat sound is generated live by the engine from the person's actual pulse. Read section 5 carefully, because the one way to break this demo is to put your material where the heartbeat lives.

---

## 2. The four stems

| Stem | Character | Loop | Root | Notes |
|---|---|---|---|---|
| `bed` | Sustained pad. The room | **19.000 s** | D3, 146.8 Hz | No rhythm. No transients. Present in all four segments |
| `sub` | Deep drone. The floor | **17.000 s** | D2, 73.4 Hz | **High-passed at 62 Hz, 24 dB/oct, applied in the file.** See section 5 |
| `air` | High texture. Movement and unease | **13.000 s** | non-pitched or D-compatible | Opens partway through the load segment, closes during regulate |
| `pulse` | Non-melodic rhythmic layer | **11.000 s** | non-pitched | 96 BPM implied, **no downbeat**. See section 6 |

All in **D minor**. All four play simultaneously and must sit together in that key without beating or clashing.

There is a fifth stem in the spec called `lead`. **It is not used in v1.** Do not make it.

### Loop lengths are coprime on purpose

19, 17, 13 and 11 seconds share no common factor, so the combination does not audibly repeat for about 90 hours. This only works if the lengths are **exact**. A loop that is 19.02 seconds breaks the property and the person hears a repeat inside four minutes.

---

## 3. Delivery format, non-negotiable

| Property | Value |
|---|---|
| Channels | **Mono** |
| Sample rate | **48,000 Hz** |
| Bit format | **32-bit float** WAV |
| Sample counts | `bed` 912,000 · `sub` 816,000 · `air` 624,000 · `pulse` 528,000 |
| True peak | never above **−1.0 dBTP** in any file |
| Loop join | seamless, verified by playing the file looped for two minutes with no audible click or swell at the wrap |

The engine has **no resampler**. A file at 44.1 kHz is rejected outright and the segment fails to load. There is no stereo path anywhere in the engine, so a stereo file will be wrong in a way that is hard to hear until it is on a stall.

Sample counts must be exact to the sample. Check them, do not trust the DAW's timeline display.

---

## 4. The filter is the main expressive tool, so give it something to work on

This is the part that is easy to miss and expensive to discover late.

The engine does not fade your stems in and out to create the arc. It sweeps a **master low-pass filter** across the whole mix:

| Segment | Cutoff moves |
|---|---|
| Baseline | fixed at **1,400 Hz** |
| Load | **1,400 → 3,600 Hz** as arousal rises |
| Regulate | **3,600 → 620 Hz** as the person comes down |
| Resolve | holds 620 Hz, then opens slightly to **900 Hz** as the trace appears |

So the entire emotional payoff of this piece is content being **revealed and then withdrawn** between roughly 600 Hz and 4 kHz.

What this means for you:

1. **The `bed` must have real harmonic content up to at least 6 kHz.** If it is a soft pad that rolls off at 1.5 kHz, opening the filter to 3,600 Hz does nothing and the load segment is silent movement.
2. **Put the interest between 620 Hz and 3,600 Hz.** That is the band the person will experience opening and closing. Upper harmonics, movement, texture, the thing that makes it feel alive. That band is your instrument.
3. **Below 620 Hz should be able to stand alone.** In the last stretch of regulate, that is all the person hears. If the bed and sub are uninteresting under 620 Hz, the payoff is muffled rather than intimate.
4. **The `air` stem lives above 3 kHz.** It is what opens at the top of the load segment. It is also almost entirely filtered out during regulate, which is a large part of why regulate feels like relief.

Author with the assumption that a filter is going to move across your work constantly. Check your stems through a low-pass at 620 Hz, at 1,400 Hz and at 3,600 Hz and make sure each of those three states is something you would be happy for someone to sit in for a minute.

---

## 5. The spectral reservation, and why it is the one rule that cannot bend

**36 Hz to 62 Hz belongs to the engine's heartbeat layer alone. Nothing you make may put energy there.**

The engine generates a sub-bass event on every one of the person's actual heartbeats. 44 Hz fundamental, energy confined to 36 to 62 Hz, 8 ms attack. That sound is the entire argument of the demo. The person is supposed to hear their own pulse speed up and slow down, and understand without being told that the system is reading them.

If your `sub` stem has energy in that band, it masks the heartbeat. Not entirely, and not obviously. The piece will still sound good. The heartbeat will just become mud, nobody will notice in the studio, and the one thing this demo exists to prove will quietly stop being true.

**Therefore:**

- `sub` is high-passed at **62 Hz, 24 dB/oct, printed into the file.** Not as a plugin on the bus, not in the engine, in the file.
- Its fundamental at D2 = 73.4 Hz sits above the cutoff, so the note survives. What gets removed is everything under it.
- `bed`, `air` and `pulse` should have nothing meaningful below 62 Hz either. High-pass them all and check on a spectrum analyser rather than by ear.

**QC step:** run each finished file through a spectrum analyser with a 36 to 62 Hz band highlighted. If any stem shows energy there, it is not finished.

---

## 6. Two things are called "pulse"

This has caused confusion already, so, explicitly:

| Name | What it is | Who makes it |
|---|---|---|
| The `pulse` **stem** | A musical rhythmic layer, 11 s loop, 96 BPM implied, non-melodic, no downbeat | **You** |
| The **per-beat layer** | A sub-bass thump on the person's real heartbeat, 44 Hz, 36 to 62 Hz | The engine, live |

The `pulse` stem must not be low. It must not be in 36 to 62 Hz. It must not sound like a heartbeat. It is a mid-range rhythmic texture whose job is to add pressure during the load segment, and it closes down entirely during regulate.

"No downbeat" means a listener should not be able to tell where bar one is. Avoid anything that implies a musical grid, because the person's own heartbeat is the only rhythm in the room that is allowed to matter.

---

## 7. Loudness

| Target | Value |
|---|---|
| Integrated programme loudness | **−16 LUFS** across the nominal four minutes |
| Short-term, 3 s window | **never above −11 LUFS** anywhere |
| True peak | **−1.0 dBTP**, enforced again by the engine's limiter |

Segment short-term ranges, as the engine will drive them:

| Segment | Range |
|---|---|
| Baseline | −22 → −17 LUFS |
| Load | −17 → −12 |
| Regulate | −20 → −14, descending |
| Resolve | −24 → −16, tapering to silence |

Note that regulate gets **darker, not quieter**. The sub grows as the top end goes. Do not build a regulate that only works if the level drops.

---

## 8. Monitoring, and one warning

**Author and balance on flat reference monitoring only.** Open-back studio headphones or nearfields.

The demo runs on Sony WH-1000XM5, which is what the person actually wears. **Do not tune on them.** They have a consumer bass shelf and DSP in the chain. Anything you balance on the XM5 will be written too quiet in the sub, and it will vanish on reference.

The correct workflow: author on reference, then **verify** on the XM5 with ANC on. Verify means checking nothing is broken. It does not mean adjusting.

---

## 9. Delivery checklist

Tick every line before handing over.

- [ ] Four files: `bed.wav`, `sub.wav`, `air.wav`, `pulse.wav`
- [ ] Mono, 48 kHz, 32-bit float
- [ ] Exact sample counts: 912,000 / 816,000 / 624,000 / 528,000
- [ ] Each loops seamlessly for two minutes with no click or swell at the wrap
- [ ] No energy between 36 and 62 Hz in any file, checked on a spectrum analyser
- [ ] `sub` high-passed at 62 Hz, 24 dB/oct, printed
- [ ] `bed` has usable harmonic content to at least 6 kHz
- [ ] Each file peaks below −1.0 dBTP
- [ ] All four sound coherent together in D minor with no beating
- [ ] Checked through a low-pass at 620 Hz, 1,400 Hz and 3,600 Hz
- [ ] Balanced on flat reference, verified on XM5, not adjusted on XM5

---

## 10. Timeline

| Deliverable | When | Note |
|---|---|---|
| **Placeholder stems** | This week | Plain synth pads are fine. Correct lengths, correct key, correct high-pass. They exist only so the engine has something to load and the Week B gate can run |
| **Final stems** | End of Week C, 30 September | The real thing |
| Revision pass | Week D | After the first runs on real people |

The placeholders matter more than they sound like they do. Without them the whole audio chain cannot be tested, and the audio chain is the demo.
