# Bridge stall investigation — 22 September 2026

Scope: deliberate 2, 5 and 10 second freezes of the Python live loop in baseline, load,
regulate and resolve. Native audio continues to render during the freeze. This is different
from suspending the entire process, starving the audio thread, losing the output device, or
losing armband contact. No engine source, native source, ABI or DLL was changed.

## Failure and repair

The played-beat scheduler has a regular sequence of future beat positions. After a long gap,
`tick()` skips positions that can no longer meet the 300 ms lead. Previously it left its interval
predecessor at the last *published* beat. The first resumed `ok` event therefore described the
whole gap as one RR, producing both a false low displayed rate and potentially an invalid audio
argument. This did not change the physiological intervals fed to PSV/HRV; those follow the
separate reconstructed packet timeline.

A compact pre-fix example at about 75 bpm produced RR values of 2399.4, 5598.6 and 10397.5 ms
after 2, 5 and 10 second gaps. The first was accepted by audio but wrongly described about
25 bpm; the latter two exceeded the shim's 250–2500 ms input range. The exception escaped
`LiveLoop`, ending its tasks. Normal server cleanup then faded and closed audio; the launcher
would start a new idle process, not resume the visitor.

Recovery order matters in the old code: a packet arriving before the first resumed tick refreshed
the scheduler's grace window and exposed this fault. If the tick instead ran first after the
five-second grace, it could stop/restart the lattice and avoid this particular exception.
The deterministic before/after matrix deliberately exercises packet-first recovery; unit tests
cover both orders. Thus absence of a crash in one old-code run did not prove recovery was safe.

Three changes:

1. **Scheduler:** each skipped position becomes the predecessor for interval calculation. The
   next RR is one lattice interval; it is not clamped or divided to disguise the missing beats.
   Actual last-published timing remains separate for restart spacing. Existing phase correction,
   acceptance, interpolation grace, lead and minimum-spacing rules are unchanged.
2. **Beat submission:** only `pls_push_beat` invalid-argument/full-queue refusals are caught.
   Both guarantee nothing was queued. The event is counted in `audio_beats_dropped` and logged as
   `heartbeat_beat_dropped` with sequence, scheduled time, RR and reason. There is no late retry.
   Lifecycle, device and unrelated failures still propagate. A physiological `rejected` event
   remains publish/log-only; native late/full-slot drops keep their own existing counters.
3. **Resolve ending:** recovery after the resolve deadline preserves that deadline before
   entering reset. It sends silence instead of starting a fresh three-second heartbeat fade.
   Previously state-first versus control-first recovery could produce different audible endings.
   Early manual stops still use their ordinary three-second fade.

Network publication still precedes native submission. Consequently a rare native-only refusal
can leave a visual beat with no sound; it is explicitly logged. There is no cancellation field
on the frozen contract, and changing publication order would introduce different races.

## What the visitor would hear

- **While Python is frozen:** native rendering continues. Engine loops keep their phase, using
  the last delivered controls, and native gain ramps already issued keep running. Queued
  heartbeats play and their finite tails finish; then there is no heartbeat. The shim does not
  invent replacements while Python is unavailable.
- **After recovery:** missed beats are skipped. Newly scheduled beats return at future `t_play`
  values, never a burst of overdue beats. Their level follows the current session/controller
  state. Session boundaries catch up to host deadlines rather than extending fixed segments.
- **If a boundary was missed:** an unissued musical gate or fade cannot happen retrospectively.
  A gate may wait for its next loop boundary; a fade can start late with less time left. A stall
  spanning resolve's end must not resurrect heartbeats during completed reset.
- **If the supervisor kills the bridge:** the output process ends abruptly, without the normal
  three-second shutdown fade. Restart comes up idle and silent; the visitor must start again.

These are native rendered-signal observations and code-path consequences, not a headphone
listening report. We have not verified perceptual clicks, the actual DAC anchor, physical device
recovery, real BLE buffering, or behavior when the audio thread itself is starved.

## Real-clock matrix

`tools/check_stalls_realtime.py` ran three independent production-length sessions concurrently,
one per stall duration. Each blocked the actual asyncio owner with `time.sleep` once in each
running segment. A separate paced thread continued pulling the committed engine through the
committed native shim at 48 kHz. The real server publication/lead-validation path ran without
opening a network listener. No audio device, browser, headset or supervisor was started.

Baseline was interrupted at 46 s during hold; other segments at 20 s after entry. The 45 s capture
had ended but its reporting/classification tail could still be pending. This is **not** a claim
that baseline was already final. The 10 s hold stall degraded baseline, preserving null `hr_base`;
the 2 s and 5 s cases retained a ready baseline. All three sessions reached reset and then idle.

Time from unfreeze to the first subsequent native voice start, in milliseconds:

| Injected stall | Baseline hold | Load | Regulate | Resolve |
|---|---:|---:|---:|---:|
| 2 s | 784 | 306 | 420 | 337 |
| 5 s | 331 | 378 | 622 | 569 |
| 10 s | 495 | 498 | 492 | 499 |

Voice timing is observed from native counters at 10 ms resolution with limiter latency accounted
for, not from a DAC. Counters include voices under zero gain and alone cannot prove audibility.
The main injections resume while heartbeat gain is still audible; the separate resolve-ending
case below deliberately verifies that new voice counts can coexist with digital silence.

- Actual blocking durations were 2.0004–2.0011, 5.0003–5.0007 and 10.0003–10.0011 s.
- Audio frames advanced throughout every freeze: 95,520–96,480, 239,520–240,960 and
  480,000–480,480 respectively (snapshots have block granularity).
- No loop exception, native beat rejection, render error or native late/full-slot drop occurred.
- Minimum observed voice-start spacing was 540 ms: no recovery burst or overlapping voices.
- Music remained in the summed output during the stalls. All three final seconds were exactly
  digital silence. Full-run sample peaks were below −7.5 dBFS; this is not a new true-peak claim.
- The render driver's lateness was about 7.1 ms median and 52–58 ms maximum. This is a Python
  test driver, not WASAPI underrun evidence or a device-clock measurement.
- The 10 s baseline-hold stall crossed load entry and missed its planned pulse-stem opening;
  opening moved to the next 11 s loop boundary. Recovery did not restore that lost musical time.

Evidence and summed-output WAVs are under gitignored
`logs/stalls-realtime-20260922-fixed/{2s,5s,10s}/`.

## Deterministic before/after and ending checks

`tools/check_stalls.py` uses production durations, the real asyncio loop tasks, scheduler/model/
session/controllers, generated HRM notifications and the committed native engine/shim. Its clock
advances by rendered audio frames rather than wall-clock waiting. During a freeze, every native
audio frame is still rendered but no bridge callback runs. Buffered synthetic notifications then
arrive once, with late arrival timestamps. Each case starts a separate session and injects one
stall 20 s into its selected segment, including baseline capture. This complements, rather than
substitutes for, the real-clock run.

Historical comparison reads scheduler/live code from `99b05ef` via `git show` into the diagnostic
process. It does not check out or modify the repository. The native binaries and other components
are unchanged by the scheduler comparison. Reports retain the tested revision and resume order.

The completed comparison (12 cases on each version) was:

| Segment | Old code, 2 s | Old code, 5 s | Old code, 10 s | Fixed code, all three |
|---|---|---|---|---|
| Baseline capture | Crashed | Crashed | Crashed | Recovered; session completed |
| Load | Crashed | Crashed | Crashed | Recovered; session completed |
| Regulate | False long RR | Crashed | Crashed | Recovered; session completed |
| Resolve | False long RR | Crashed | Crashed | Recovered; session completed |

The two non-crashing old cases emitted 1921.4 ms /31.2 bpm in regulate and 2467.1 ms /24.3 bpm in
resolve. All fixed cases reached fresh idle with zero native render, late-beat or full-slot
errors. First resumed scheduled beats were 314–1113 ms after unfreeze; the minimum publish lead
across all twelve cases was 314 ms. The slower end of this recovery range is ordinary waiting
for the next eligible beat position, not a late beat being played. This deterministic run uses
the designed-pose PSV source; the real-clock run above uses the default body source.

The additional resolve +40 s /10 s case spans the final fade and reset boundary. Its rendered
output remains exact digital silence after the resolve deadline (allowing 100 ms observation
margin). Native unit integration tests force both state-first and control-first recovery and
assert resumed voices produce only zeros. No second adversarial review round was run.

Evidence: gitignored `logs/stalls-before-20260922/final-matrix/`,
`logs/stalls-after-20260922/final-matrix/` and
`logs/stalls-after-20260922/resolve-ending-clock-checked/`.

## The launcher is a separate recovery boundary

The existing supervisor accepts cached bridge status/audio-counter snapshots while they are less
than five seconds old, refreshing `last_healthy`. Once stale, it waits more than five seconds from
that last healthy observation before restarting. Therefore a roughly ten-second freeze races
with the watchdog; two and five second recovery is not the same case. A 250 ms modeled polling
test reaches the restart predicate at 10.0 s. Actual process polling can differ.

The diagnostic deliberately bypasses this supervisor to observe in-process recovery. It must
not be read as a promise that a launched booth will retain its session through a ten-second
freeze. No watchdog policy change was made: waiting longer versus restarting sooner is a separate
operational choice. A restart force-closes the owned Windows Job; it cannot ask a frozen loop to
perform its graceful audio fade.

## Reproduce

```powershell
.venv\Scripts\python.exe -m tools.check_stalls --baseline-revision 99b05ef
.venv\Scripts\python.exe -m tools.check_stalls
.venv\Scripts\python.exe -m tools.check_stalls --segment resolve --seconds 10 --offset-ms 40000
.venv\Scripts\python.exe -m tools.check_stalls_realtime --wav
.venv\Scripts\python.exe -m pytest
```

The historical diagnostic is expected to return failure when it reproduces the old exception;
this is not an expected-failure test in the suite. Normal regression tests must all pass.

## Verification

- Full Python/browser regression suite: **710 passed**, with no expected failures.
- Existing native DSP executable: **91 checks, zero failures**.
- Existing native real-time allocation tripwire: **zero allocations and zero frees** while armed.
- Ruff and `git diff --check`: passed.
- One heavy-review round, three agents. Review covered scheduler recovery, event-local native
  refusal handling, and end-to-end audio/supervisor boundaries. The resolve-ending finding was
  fixed and regression-tested; the supervisor boundary is documented above. No second review
  round was run.

The existing committed native binaries were exercised, not rebuilt. The engine source, native
source, DLLs and frozen wire contract are unchanged.
