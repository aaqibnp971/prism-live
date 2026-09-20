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
