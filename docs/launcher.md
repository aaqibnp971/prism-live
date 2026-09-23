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
.venv\Scripts\python.exe -m tools.launch --booth --lan-ip 192.168.1.201 --mqtt-phone-ip 192.168.1.135
```

`--booth` switches the whole configuration together:

| Setting | Default test mode | `--booth` |
|---|---|---|
| Bridge listener | Localhost (`127.0.0.1`) | Selected local RFC1918 LAN IPv4 address |
| Heartbeat packet source | Synthetic, explicitly labelled | Android Polar Sensor Logger MQTT PPI |
| Task-event producer | `task-screen` | `quest` |
| Browser task | Open | Not opened |
| Laptop | Tiled task and console | Full-screen attendant console |
| External display | Full-screen spectator | Full-screen spectator |

Only one task-event producer can bind under prompt 3.2. The launcher therefore never opens a
browser task in booth mode: the headset owns that input. The attendant's start/cancel/stop button
still runs in-process; enabling LAN data does not add network controls.

MQTT requires both addresses explicitly; reserve them on the travel router before the booth.
The addresses above are the 23 September probe's laptop and phone, not universal defaults.
For a layout/recovery check without a phone, select synthetic input explicitly:

```powershell
.venv\Scripts\python.exe -m tools.launch --booth --packet-source synthetic --lan-ip 192.168.50.20
```

Replace that example address with the laptop's actual address. `--lan-ip` must name a local
RFC1918 address and is rejected without `--booth`; the launcher does not bind every interface.
A private address is not evidence of a trusted router: verify the network yourself, and never
select venue Wi-Fi. The launcher prints the headset URL, for example
`ws://192.168.50.20:8787/live`, and the expected client identity, `quest`. Use the printed address,
not `localhost`, on the headset. The launcher never configures the router. MQTT mode owns the
narrow temporary firewall exception below; it does not add a WebSocket/Quest firewall exception.
If a Quest connection needs an allowance, scope that separately to the own-router adapter and
Quest address, rather than accepting Windows' broad "allow Python" prompt.

## Phone and router runbook (replacement for direct laptop BLE / prompt 2.8)

The travel router **must reserve fixed DHCP addresses for both laptop and Android phone**.
Use the phone's per-network stable MAC when configuring its reservation; verify the addresses
after reconnecting. If either address changes, stop the launcher and correct the reservations
and command. Do not widen the allowed source to a subnet. Do not port-forward either MQTT port.

Prepare the optional production MQTT dependencies using the project's `mqtt` extra in the
project environment (`python -m pip install -e ".[mqtt]"`); this is an operator setup step, not
an automatic installation by the launcher. Run MQTT booth mode from **Administrator PowerShell**
so it can create and remove its temporary firewall rule. Ordinary localhost synthetic tests do
not need elevation or MQTT dependencies.

The MQTT extra was installed into this project's `.venv` during implementation on 23 September.
It pins aMQTT 0.11.3 and Paho 2.1.0; aMQTT resolves the shared `websockets` dependency to
15.0.1. Run `python -m pip check` after preparing another machine. This installation does not
create a service or permanent listener/firewall exception.

On Android, keep Polar Sensor Logger connected to the Verity Sense. Use these settings for the
addresses and defaults in the command above:

| App setting | Value |
|---|---|
| MQTT broker address | `192.168.1.201` (no URL scheme) |
| MQTT port | `1883` |
| Topic | `prism-probe` (the app publishes below `psl/prism-probe`) |
| Client ID | `verity-phone` |
| Credentials / TLS | Blank / off; own trusted router only |
| Selected streams | HR and PPI |
| SDK mode | **Off**; Verity Sense HR/PPI are unavailable in SDK mode |

The default expected sensor ID is `1967873D`, our captured unit. `--mqtt-topic-prefix`,
`--mqtt-client-id` and `--mqtt-device-id` let the operator select a different documented setup;
they must match the app and payload identity. The PPI MQTT topic ends in **`/ecg`**, despite
carrying optical PPI, not ECG. No direct Windows BLE connection is attempted.

Start PPI when the phone connects and **leave it streaming between visitors**. The laptop does
not send a phone start/stop command at session boundaries. Allow approximately 25 seconds for
the initial PPI warm-up plus a further advancing burst (about five seconds) for freshness probation;
the console says `WAITING FOR PPI` until that check completes and makes a
stalled/disconnected feed visible. Flowing packets are not proof the sensor is worn: neither HR
nor skin-contact flags are trusted for that decision. The host still refuses session start
without accepted data. The heartbeat is measured but played through a several-second buffer,
not an instantaneous pulse monitor.

### Narrow firewall ownership

Before starting bridge/browser processes, the launcher creates a uniquely named
`PrismPolarMqtt-<id>` inbound rule for **TCP only, the exact phone source IP, the exact laptop
destination IP and MQTT port, the selected interface, its current Windows profile, and this
Python process image**. A Windows virtual environment's executable redirects to its base Python;
the launcher resolves the actual image first so the program-scoped rule matches the listener.
The rule can be scoped to a trusted router labelled Public by Windows;
this does not make venue Wi-Fi acceptable. It never changes the network category or disables
the firewall. An independent relay also rejects every source IP except the phone's.

The memory-only MQTT broker binds `127.0.0.1:1884`; only its relay binds the selected LAN IP on
1883. The source subscribes before LAN ingress opens. There is no persistent broker service,
external broker or cloud forwarding. These ports are configurable with `--mqtt-port` and
`--mqtt-broker-port`; the LAN, loopback and WebSocket ports must differ.

The launcher removes **only its own rule** on Q, Ctrl+C and ordinary startup/shutdown failures.
It never adopts, overwrites or removes a pre-existing rule. Rule setup failure stops startup;
do not work around it with a broad exception. Existing broader firewall exceptions are not
audited or modified. IP allowlisting is not encryption or authentication.

The exact owned rule name and emergency removal command are printed at startup and recorded
in `logs/launcher/launcher.json`. A forced kill of the supervisor or power loss cannot execute
its cleanup; children and listeners stop, but the rule can remain. After such a kill, use the
printed exact name in Administrator PowerShell:

```powershell
Remove-NetFirewallRule -Name "PrismPolarMqtt-<the-exact-printed-id>"
```

Do not use a wildcard or remove the old probe's rule by guessing. A bridge-only restart keeps
the launcher's rule, recreates the memory broker/source, and returns to a fresh idle session;
the phone reconnects while its PPI stream remains running. No old broker queue survives a crash.

## Display mapping and browser-test geometry

Inspect/change the mapping without editing code:

```powershell
.venv\Scripts\python.exe -m tools.launch --list-displays
.venv\Scripts\python.exe -m tools.launch --console-display 0 --task-display 0 --spectator-display 1
.venv\Scripts\python.exe -m tools.launch --booth --lan-ip 192.168.1.201 --mqtt-phone-ip 192.168.1.135 --console-display 0 --spectator-display 1
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

Default browser-test mode uses synthetic packets and labels them `SYNTHETIC INPUT - NO ARMBAND`.
Booth mode defaults to the phone's MQTT PPI source, with source/warm-up status on the console.
No canned feed or simulation mode is added to either browser. Browser pages are loaded from
local files, including their local fonts.

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

## Private physiological recordings

Booth session JSONL logs, raw HR/PPI captures, filter/baseline analyses, listening WAVs and
screenshots containing a real visitor's readings must stay local. `logs/` and
`private-data/physiology/` are gitignored; use `private-data/physiology/captures/` for raw
recordings and `private-data/physiology/reports/` for their analyses. Never force-add these
files, copy their readings into tracked documentation, or move them into `tools/fixtures/`.
Only generated synthetic fixtures belong in Git and canonical tests. Gitignore is a guard
against accidental staging, not access control or a guarantee against an explicit force-add.

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
