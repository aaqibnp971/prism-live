# Spectator screen

Open `index.html` full-screen on the spectator display while the bridge is running. It connects to
`ws://localhost:8787/live` by default. To point it at another live bridge, use only the transport
override, for example:

```text
index.html?ws=ws://192.168.1.20:8787/live
```

There is deliberately no simulation or standalone mode. For development, run
`python -m tools.fake_sender --loop`; the page still receives a real, contract-checked WebSocket
feed and shows its connection state.

The page has two idle views:

- Cold idle shows a quiet ready state, with no progress, readings or trace.
- After a completed 20-second reset, the completed beat trace remains through idle and is cleared
  only when the host announces the next baseline. A 3-second stopped/failed reset is not presented
  as a completed session.

The held trace exists only in the browser's memory because the frozen contract has no history
message. Reloading the page in idle therefore returns to cold idle. During a session, a page opened
late begins its trace at the next live beat and never invents earlier samples.

If the socket closes, a message is invalid, or no state arrives for 2.5 seconds (including the
first state), the last verified view freezes under a full-width red `LIVE FEED LOST` banner. With
no verified view yet, the banner says `LIVE FEED UNAVAILABLE`. Reconnection happens once per second;
the banner leaves only after a valid host state arrives.

## Visual design

The layout, amber palette, hierarchy and two-bar reading/authority treatment come from the component
inside `docs/design/spectator-v3.html`. That file is a design archive, **not a runnable application**:
its last script contains a JSON-escaped HTML component. Its bundle, React framework and simulation
logic are not used by this page. IBM Plex Sans and IBM Plex Mono are served from `fonts/`, extracted
from the design's embedded resources, with their SIL Open Font License. No CDN is needed.

The 1920 × 1080 stage scales uniformly to the display. The reading's hatched band narrows with the
host confidence; it is a visual uncertainty cue, not a statistical confidence interval. The lower
MAY MOVE bar reads the host's authority directly. Valence has no reading or authority fill, exact
0.00 confidence/authority, and a stop marker at zero with a hatched dead zone. The baseline learning
fill and each segment's progress use host elapsed/nominal time; strip widths are layout proportions,
not a four-minute deadline. The timer shows host `t_session` without a fixed total.

The field is deliberately a placeholder for 3.4. The 3.5 reveal ports v3's three large-number cards,
photograph heading and expanded trace, with the live rules below. Dimension titles are slightly
smaller than v3 to fit second-person labels and NOT READABLE; compact numeric confidence labels,
signal status and live-link/fullscreen controls are retained from the live screen.

The trace's axis includes every observed BPM, including the lowest value. Both live and held plots
have an explicit SVG clip and an overflow-clipped, bounded grid cell; they cannot cover the footer.
`tests/web_spectator.test.cjs` checks numerical bounds. `tests/test_spectator_browser.py` checks actual
browser geometry, scheduled fixture beats, baseline fill, both idle views and frozen disconnects.
It requires an already-installed Edge/Chromium and otherwise skips; it installs nothing. Run the
same check manually and capture 1920 × 1080 screenshots with:

```text
python -m tools.check_spectator_browser --output working/spectator-check
```

This tool supplies test messages over a localhost WebSocket; it adds no simulation mode or fixture
data to the production page.

## Trace reveal (3.5)

Resolve reveals the panel on the first host state with `segment_nominal_ms − segment_elapsed_ms`
at or below 20,000. The local clock never starts it; the 2 s host cadence bounds its timing precision.

- SAT DOWN AT: first plotted baseline reading, including an elevated arrival rate.
- PEAKED AT: maximum plotted rate **in load only**, never baseline, regulate or resolve.
- LEFT AT: most recent plotted resolve beat, labelled as updating until the completed reset.
- PEAKED AT − LEFT AT: subtract those same one-decimal displayed values. Negative values stay
  negative. There is no threshold colour, outcome, recommended close or `drop_bpm` substitution.

The trace retains all session beats, not a rolling 1,024-point tail. Only scheduled, non-rejected
beats supply its values; 2 s state HR messages do not add samples or replace extrema. Host boundary
times classify beats by `t_play`, including a boundary message arriving after a beat was plotted.
The history rail records the maximum observed host authority on each dimension. Its label describes
history, not current idle authority; valence remains NOT READABLE with exactly zero confidence and
authority. No authority or confidence is inferred by this page.

The resting-rate dashed line comes only from `hr_base`, on both small and expanded traces. A null
removes line and label; baseline/degraded sessions do not substitute an average or first reading.
The axis includes the resting line as well as every beat so neither falls outside the plot.

A completed 20 s reset freezes the whole summary, trace and resting reference. Idle's new session
id does not overwrite them; the next baseline clears all of them. A stopped/failed 3 s reset does
not present a completed result. A screen opened late or disconnected mid-session cannot backfill:
it marks PARTIAL TRACE, uses only observed readings and shows a dash for missing first/load/resolve
readings or N. Opening after baseline cannot supply SAT DOWN AT. Reloading in idle remains cold.
