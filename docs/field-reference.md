# Shared ambient field — prompt 3.4

The visual source is [`design/field-frames.html`](design/field-frames.html). Its last script block
is a JSON-escaped HTML document containing a DC component. The acceptance tool reads that
component, evaluates only its isolated pure panel/value methods against an inert props holder,
and reconstructs the original CSS layers. **The bundle, its framework and its other scripts are
never run.** Fonts are local. Both live screens remain build-free plain JS.

## The working Unity reference

| File in `web/shared/` | Responsibility |
|---|---|
| `field_mapping.js` | Host state → seven token values; segment/authority history and palette layers |
| `field_pulse.js` | Scheduled beat → bounded visual envelope, never an arrival flash |
| `field_renderer.js` | Seven values → pixels; fixed geometry, drift and linear-luminance safety |
| `field_view.js` | Shared clock-independent view adapter used by task and spectator |

Tokens are `{fog, light, hue, sat, horizon, pulse, motion}`. Drawing accepts weighted complete
palette layers plus a 0–1 heartbeat envelope and drift phase. It knows nothing about WebSockets,
PSV, segment decisions or task performance. The two apps convert `t_play` using the shared clock
synchronizer, scoped to the current connection. There is no field simulation on the spectator;
the task's explicitly labelled standalone test shows a fixed load palette without invented beats.

Horizon coordinates are measured **from the top**; 0.50 is eye level. Increasing the coordinate
lowers the horizon and opens the sky. The diffuse light centre remains **(0.50, 0.40)** in every
palette. Current mapped horizons are at least 0.44. A future horizon below 0.40 numerically
would put the light below ground; do not silently move it or clamp away the retuning mistake.

## Exact state mapping

Let `lerp(lo, hi, x)` clamp x to [0,1], then linearly interpolate. Let
`A = 0.5 + (psv.arousal - 0.5) × authority.arousal` and
`C = 0.5 + (psv.cognitive_load - 0.5) × authority.cognitive_load`.
Authority is always the value sent by the host. **Do not recompute it from confidence or apply
the load ceiling a second time.** This means low-authority live readings generally do not reach
the extreme reference frames. The ten frames are design endpoints, not synthetic state messages.

| Segment | Mapping |
|---|---|
| Baseline | `B = clamp(mean(confidence.arousal, confidence.cognitive_load, confidence.readiness) / 0.393)`. Fog lerp(.38,.26,B), light lerp(.45,.62,B), horizon lerp(.44,.50,B); hue 208°, saturation .12, motion .008. No elapsed-time or baseline-quality shortcut. |
| Load | Hue lerp(214,202,A), saturation lerp(.16,.30,A), light lerp(.70,1.05,A); fog lerp(.20,.30,C), motion lerp(.010,.004,C); horizon .50. |
| Regulate | Hue lerp(24,46,A), light lerp(.38,.95,A), fog lerp(.58,.34,A), motion lerp(.008,.030,A); saturation lerp(.14,.30,C), horizon lerp(.44,.56,C). High arousal is the bright end: falling arousal darkens/thickens/slows the field. No elapsed-time approximation of the adaptive segment. |
| Resolve | Capture the last observed regulate tokens and entry authority. Hue/light/fog/motion ease from those values to (30°,.50,.44,.006), scaling each entry offset by current arousal authority / entry arousal authority. Saturation eases to .14 using the cognitive-load authority ratio. Zero entry authority has zero offset. A mid-resolve join has no history and uses fixed resolve targets. Horizon independently lerps(.44,.58,(20000−remaining)/8000). `remaining = segment_nominal_ms − segment_elapsed_ms`. |

Pulse amplitude uses `R = clamp((hr_bpm − 62) / 33)`, matching the 62/95-bpm authored endpoints.
Baseline lerps .06→.10, load .10→.16, regulate .08→.16; resolve's authored amplitude is .08.
**Visual-only correction, 22 September:** multiply these clamped authored amplitudes in every
segment by **`visualRateGain(hr_bpm) = clamp((120 − hr_bpm) / 25, 0, 1)`**. It is unchanged
through 95 bpm, half at 107.5 and exactly zero at/above 120 bpm. Unknown HR gives zero.
Resolve additionally multiplies by `cos(π/2 × clamp((3000−remaining)/3000))` in the final
three host-reported seconds and is exactly zero at/past the end. No beat message means no
pulse, irrespective of amplitude. Idle/reset clear the live field; the spectator's held trace
continues to obey prompt 3.5. The audio heartbeat is the primary evidence and is unchanged:
every eligible scheduled audio beat continues at every supported rate, with its original
level and ending policy. Only visuals yield before their cadence can approach 3 Hz.

## Transitions, motion and disconnection

Between segments, retain the preceding palette and render it alongside the current palette.
The new palette's target weight is `clamp(segment_elapsed_ms / 10000)`. The renderer blends
the two complete fields in linear light; **it never interpolates a blue hue through green to
amber**. Within a segment, small hue ranges interpolate normally. For smooth presentation of
the 2-second state cadence, the view eases weights to each newly reported target over 250 ms,
never extrapolating past it. The host still owns the ten-second schedule and every boundary.
This bounded presentation lag is part of the Unity reference, not a second segment clock.

Drift advances from the current weighted motion rate while the feed is live, in **view heights
per second** (one normalized unit is one view height, not one stripe period); it moves soft fog
striation, not the light source, horizon, camera or task targets. Frame stalls do not advance a
large catch-up motion. Disconnection or a stale host freezes the last pixels, including drift
and any current pulse, and clears future beats. No resumption of an old pulse queue. The apps
show their unmistakable lost-feed marker. WebGL failure is explicitly labelled, not replaced
with a scene that might appear live.

## Mandatory heartbeat tests and safety limits

The pulse begins at synchronized `t_play`. It uses a 90 ms raised-cosine rise, a 180 ms
raised-cosine fall and no support beyond 270 ms. Envelopes never add. Already-late messages,
rejected beats, duplicates, wrong-session events and onsets first observed over 45 ms late
cannot flash. No autonomous beat timer exists. Defence against unexpected faster streams
uses the maximum of `beat.hr_bpm`, `60000 / beat.rr_ms` and
`60000 / planned_onset_interval_ms`. The last valid, non-rejected future candidate supplies
the previous onset, including candidates suppressed visually. A rate at least 120 bpm or
interval of 500 ms or less is suppressed, not converted into an every-Nth-beat visual pattern.
The view caps the state's already-tapered mapped amplitude with the active beat's tapered
segment amplitude using `min`; it does not multiply the taper twice. Thus a state message
from up to two seconds earlier cannot allow fresh fast beats to flash at the old brightness.
These are visual-only guards; they cannot suppress audio and never replay a suppressed pulse.

The shader measures **linear-sRGB relative luminance**, not HSV brightness or CSS opacity.
For every pixel it preserves the unpulsed gradient and full-field haze, constructs a local
fog/light peak, and limits its positive luminance difference to:

`min(token.pulse × unpulsed_base_gradient_luminance, 0.09)`, with `token.pulse ≤ 0.22`.

The unpulsed gradient is darker than the composite field, so this is a stricter denominator
than the complete field. The final cap includes quantized 8-bit output and cross-dissolves;
dithering is disabled. Only fog/light masked pixels can change. The continuous envelope is
sampled by display frames; frame pacing and quantization are not a sub-millisecond onset claim.

Tests combine every integer rate from **45 to 180 bpm** (future scheduling, complete 90 ms
rise on visible pulses, no overlap/addition, dropped late/rejected events) with CPU pixel/property checks and
actual browser framebuffer comparisons of rest/rise/peak across all ten palettes, amplitude
.22 variants, and blue/amber cross-dissolves. See the measured acceptance report below.
The taper adds checks for unchanged amplitudes at/below 95, the 107.5 midpoint and exact zero
through 120–180 bpm in all segments, including resolve's fade. Stale-state and disagreement
between HR, RR and onset cadence must not bypass the taper or manufacture divided-rate flashes.

**No photosensitivity safety certification is claimed**, at 95 or 180 bpm. The old assertion
in the script was unsupported. Hardware, headset and whole-experience limitations, plus the
W3C source for the additional absolute-luminance margin, are recorded in `known-limits.md`.

## Ten-frame acceptance

`tools/check_field_browser.py` renders the ten exact token sets through the new renderer and
places them next to the extracted original CSS panels. Frames 9 and 10 include both rest and
peak, so twelve reference panels are checked. Each comparison is **1920×1080**. A ten-frame
contact sheet and per-panel RGB/luminance differences are also produced. All screenshots are
explicitly diagnostic, never a live feed. Local artifacts are in
`logs/field-acceptance-20260922/` (gitignored; reproducible).

Measured 22 September, using installed headless Edge with `--disable-gpu` (shader/framebuffer
checks, **not booth GPU performance or headset output**): resting-panel RGB mean absolute
difference **0.00254–0.00345** on the 0–1 scale (under one 8-bit channel level on average).
The largest peak mean difference is frame 10 at **0.00653**, with a maximum channel difference
of **7/255**. The rendered pulse check covers **380,928 actual pixel comparisons**, 31 palette/
blend cases at four envelope strengths: maximum relative luminance increase **0.218683**,
absolute **0.079194**, below 0.22 and 0.09 respectively. Pixels outside fog/light are unchanged.

Differences that remain:

- Reference CSS performs browser layer compositing; the shader reconstructs the same rounded
  HSV colours and gradients with floating-point arithmetic. Tiny 8-bit rounding differences
  remain. Soft streak blur uses a normalized nine-tap Gaussian approximation of CSS blur(2.5px).
- The reference peak also brightens full-field ambient haze. The implementation leaves that
  layer unchanged and caps actual linear luminance. Those intentional safety corrections make
  some peak panels subtly dimmer; the base field and fixed-light geometry match.
- Reference frames are static. The live renderer adds the authored fog drift and scheduled
  heartbeat envelope. Comparison screenshots freeze drift at zero and use rest/peak endpoints.

Reproduce using the installed Node and Edge/Chromium; nothing is installed:

```powershell
node --test tests/web_field.test.cjs tests/web_field_renderer.test.cjs tests/web_field_integration.test.cjs
.venv\Scripts\python.exe -m tools.check_field_browser --output logs/field-acceptance-20260922
.venv\Scripts\python.exe -m pytest
```

Review scope: light self-check and tests, including the mandatory pulse rail tests. No adversarial
review round, engine edit, native audio edit or contract change is part of prompt 3.4.

Original prompt 3.4 verification on 22 September, before the high-rate taper: **719 tests passed**, including existing scheduler/audio
coverage and the live task/spectator browser checks; **34 Node field tests** are exercised by
the Python wrappers. Ruff and whitespace checks passed. The ten-frame output and a live
spectator capture in `logs/field-live-screen-20260922-fixed/` were visually inspected. Existing
uncommitted stall-fix work was preserved separately; no commit or push was requested here.

The live capture check also caught a headless-browser artifact: `--disable-gpu` produced black
compositor tiles even though every field framebuffer pixel was valid. The live browser checks
now use the browser's normal graphics path and assert the **composited screenshot**, not just
the canvas buffer. The clean capture required no production-renderer change. Static endpoint
comparisons keep their separate, verified software-rendering setup.
