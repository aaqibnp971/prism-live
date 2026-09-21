# Booth launcher and attendant console

From `D:\ANP\prism-live`, run the default **local browser test mode**:

```powershell
.venv\Scripts\python.exe -m tools.launch
```

This mode binds the bridge to `127.0.0.1` only, selects `task-screen` as its task-event producer,
and opens both browser screens. Its normal layout uses two extended displays, not mirrored displays:

- Display 0 (the laptop): task on the left 65%, attendant terminal on the right 35%.
- Display 1 (the external screen): spectator full-screen.

The browser task is deliberately windowed in test mode so it cannot cover the attendant's
signal-loss warning. Its full-screen button is hidden. The console fills its terminal's client area.
The launcher starts an owned Windows Console Host window, even when launched from Windows
Terminal; it does not move another terminal or any existing browser window.
Direct `python -m bridge.server` also defaults to localhost; LAN access is never implicit.

## VR booth mode

The participant does the task in the headset and never looks at the laptop. On the project's
**own router, never venue Wi-Fi**, use:

```powershell
.venv\Scripts\python.exe -m tools.launch --booth
```

`--booth` switches the whole configuration together:

| Setting | Default test mode | `--booth` |
|---|---|---|
| Bridge listener | Localhost (`127.0.0.1`) | Selected local RFC1918 LAN IPv4 address |
| Task-event producer | `task-screen` | `quest` |
| Browser task | Open | Not opened |
| Laptop | Tiled task and console | Full-screen attendant console |
| External display | Full-screen spectator | Full-screen spectator |

Only one task-event producer can bind under prompt 3.2. The launcher therefore never opens a
browser task in booth mode: the headset owns that input. The attendant's start/cancel/stop button
still runs in-process; enabling LAN data does not add network controls.

The launcher chooses the LAN address automatically only when exactly one suitable local address
exists. With multiple adapters, select the IPv4 address belonging to the project's router:

```powershell
.venv\Scripts\python.exe -m tools.launch --booth --lan-ip 192.168.50.20
```

Replace that example address with the laptop's actual address. `--lan-ip` must name a local
RFC1918 address and is rejected without `--booth`; the launcher does not bind every interface.
A private address is not evidence of a trusted router: verify the network yourself, and never
select venue Wi-Fi. The launcher prints the headset URL, for example
`ws://192.168.50.20:8787/live`, and the expected client identity, `quest`. Use the printed address,
not `localhost`, on the headset. LAN mode does not configure the router or Windows firewall.

## Display mapping and browser-test geometry

Inspect/change the mapping without editing code:

```powershell
.venv\Scripts\python.exe -m tools.launch --list-displays
.venv\Scripts\python.exe -m tools.launch --console-display 0 --task-display 0 --spectator-display 1
.venv\Scripts\python.exe -m tools.launch --booth --console-display 0 --spectator-display 1
```

In test mode, a separate third task monitor can use `--task-display 2`; in that layout both browser
screens are full-screen. `--task-display` is rejected with `--booth`, where no browser task exists.
The spectator cannot share the console or task display. Missing displays are an error before
processes start, not an excuse to silently cover the console.

For browser-task testing, pass the **whole task monitor's measured physical width**, not the
tiled window's width:

```powershell
.venv\Scripts\python.exe -m tools.launch --screen-width-cm 34.4 --distance-cm 60
```

The defaults remain the earlier task's 27-inch 16:9 monitor (59.77 cm wide), viewed at 60 cm.
That is not a measurement of this laptop. The task apportions the supplied width by
`viewport CSS width × devicePixelRatio / monitor physical-pixel width`, including when resized;
the debug corner shows the resulting physical viewport width. Measure the test monitor before
judging browser-task difficulty. Keep the task on its assigned monitor. These laptop measurements
do not describe viewing geometry inside the headset.

## The attendant button

With the attendant terminal focused, Space or Enter is the same single button:

- Idle: arm and show the pulse-aligned countdown, up to 11 s.
- Countdown: cancel and disarm. The session has not started.
- Running: stop through the host's 3 s reset.
- Reset: wait; another press cannot shorten it or queue a new visitor.

The console always says what the next press will do. B and P switch the existing body/pose PSV
source; Q exits the launcher and its processes. Signal loss makes the whole console red but never auto-stops.
Read the spoken close's difference from the spectator's trace, not from the console.

## Supervision and ownership

The supervisor runs the real engine bridge and independent Edge processes with dedicated profiles:
task and spectator in default test mode, spectator only in booth mode. It supervises only enabled
processes/pages; it does not launch, supervise or prove a connection from the Quest application.
The console and its start/cancel/stop button are **inside the bridge process**. There
is no HTTP or WebSocket start/stop path. JSON status files are read-only telemetry; editing one
cannot press a button. Browser debug connections only inspect the page's health; they do not
dispatch button/key events or inject session data.

The bridge starts with the synthetic packet source until prompt 2.8 supplies BLE. The console
labels it `SYNTHETIC INPUT - NO ARMBAND`. No canned feed or simulation mode is added to either
browser. Browser pages are loaded from local files, including their local fonts.

Closing/killing a browser restarts that browser. A bridge crash or stalled audio/status loop
starts a **new idle bridge and session ID**; the visitor must start again. Nothing resumes or
replays the interrupted session. The restarted console says so plainly. Surviving browsers
reconnect by their existing live-client code. The supervisor also detects an unresponsive
renderer and a responsive page whose feed never reconnects to a healthy bridge.

Process checks run every 250 ms, browser health checks every second, with a 15 s startup
allowance and a 5 s health timeout after first successful readiness. Restart backoff is 1 s.
These are limits for detection/retry, not a guarantee that missing hardware or a persistently
broken program can recover in 30 s. Measured recovery times and unverified physical-display
checks belong in `known-limits.md`.

**Q in the console is an intentional booth shutdown**, not a crash. It lets the bridge fade
and close normally, then closes the owned browser processes. Ctrl+C in the launcher is an
emergency supervisor shutdown. Killing the supervisor itself stops its children via Windows
Job ownership; it cannot restart itself. There is no installed Windows service or startup task,
and computer/OS failure is outside this process-recovery boundary.

Only these process families are stopped. Existing Edge profiles, windows and unrelated
processes are untouched. Each browser restart uses a new dedicated profile so stale tabs and
crashed profile locks cannot be resumed. Profiles use the Windows temporary directory on the
system drive, separate from the project/audio/log drive; their exact paths are recorded in
status telemetry. They are retained for diagnostics. Logs stay under gitignored `logs/launcher/`;
no engine source or built library is modified.

## Recovery diagnostics

Repeat the measured kill/restart test (real audio, real clock, isolated headless browsers):

```powershell
.venv\Scripts\python.exe -m tools.check_recovery
```

It sends local stdin presses, kills only its own bridge/task/spectator PIDs mid-session,
asserts recovery below 30 s for each, checks the fresh idle session after bridge death, and
saves `results.json` with logs under a new gitignored `logs/recovery-<timestamp>/` directory.
The recorded three-role recovery measurements use this default browser-test configuration, not
booth mode. They do not verify headset connectivity/recovery, physical screen placement or the
VR experience, and do not replace an armband-day run.

On a machine without the booth's second display, use:

```powershell
.venv\Scripts\python.exe -m tools.launch --headless --console-input pipe
```

This still opens the **real audio device and engine**. It is a software recovery diagnostic,
not a visitor-facing mode and not evidence of physical screen placement. Space or Enter on
the inherited input pipe presses the real in-process button; `q` requests clean shutdown.
Do not feed keyboard events through a browser debugger or a network endpoint.

`--port`, `--log-dir` and `--status-dir` isolate a diagnostic run. A status-directory lock
prevents two launchers from owning the same profiles/status. `launcher.json` reports each
owned PID, wrapper PID, generation and readiness, plus the bridge's current status. Recovery
requires a healthy audio stream, the restarted console/bridge and all enabled pages connected:
both task and spectator in test mode, only spectator in booth mode. It does not wait for, or prove,
a Quest connection. Readiness means more than a new process appearing, but is not proof that the
whole VR booth is ready. `supervisor.jsonl` records starts and recovery reasons.

Microsoft documents the browser's full-screen kiosk flags in
[Configure Microsoft Edge kiosk mode](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-configure-kiosk-mode).
The launcher's restart policy supplies supervision separately; kiosk flags alone do not.
