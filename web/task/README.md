# Browser load task

Open `index.html` on the booth laptop. In live mode it connects to
`ws://<page-host>:8787/live`, sends the required `task-screen` hello, and remains paused until a
host `state` message enters `load`. When opening the file directly, the default host is
`localhost`. Override it for another laptop with `?ws=ws://192.0.2.10:8787/live`.

For the bridge-free 75-second judging run, open:

```text
index.html?standalone=1
```

It uses the exact `LoadTask` timing model used by the live path. Task events are printed to the
browser console, and **R** or the on-screen button restarts the run.

The geometry defaults to a 27-inch 16:9 display (59.77 cm physical width) viewed from 60 cm. The
page must be full-screen so its pixel width spans that physical width. Override measured values
with, for example:

```text
index.html?standalone=1&distance_cm=65&screen_width_cm=59.8
```

The debug corner always shows the geometry, horizontal field of view, angular speed and its
instantaneous projected pixel speed.

## Ambient field (3.4)

The same `web/shared/` mapping, scheduled pulse, renderer and animation controller as the spectator
draw the field throughout baseline, load, regulate and resolve. Only load adds the attention task.
There is no task/waiting text over the field during the other three active segments. Idle/reset
clear it rather than retain a previous visitor's physiology.

The field uses the host's state and authority, including baseline confidence. Segment changes
cross-dissolve separate palettes over ten seconds of **host-reported elapsed time**, never sweeping
hue or assuming when an adaptive regulate segment ends. Only fog striation drifts locally; the
light stays fixed at x=0.50, y=0.40. The seven-token mapping is separate from drawing for Unity.
To soften the two-second state cadence, drawing eases palette weights for at most 250 ms toward
each newly reported host weight; it never advances beyond that target. Unity must use the same
ten-second host schedule plus this bounded visual settling, not a predicted local timeline.

Clock ping/pong converts a beat's `t_play` before it is queued. There are no arrival-time flashes,
rejected beats or late catch-up flashes. A lost socket or 2.5 seconds without host state freezes
the field and task behind a large red connection warning, clears queued beats and reconnects.
Fresh state resumes; an old queue cannot replay. No synthetic heartbeat fills a gap.
If WebGL is unavailable or its graphics context is lost, a persistent FIELD UNAVAILABLE warning
replaces the field, while the connection and task continue. Reload after restoring graphics;
there is no unlabelled fallback picture.

Standalone uses an explicitly labelled, fixed LOAD test field with drift but **no heartbeat**.
It does not invent state messages, confidence or authority, and still uses the identical 75-second
task model. This browser task is a testing aid; booth mode reserves the event-producer role for
the headset and does not open it automatically.
