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

### Real MQTT browser test, including one display

Real armband input does not require VR booth mode. In **Administrator PowerShell**, on a
trusted private network, explicitly select MQTT and the laptop/phone addresses:

```powershell
.venv\Scripts\python.exe -m tools.launch --packet-source mqtt --mqtt-bind 192.168.0.39 --mqtt-phone-ip 192.168.0.251 --single-display-test --screen-width-cm 33.2 --distance-cm 80
```

These are the 26 September test addresses, not defaults: verify them whenever Wi-Fi changes.
Set the app's broker to the laptop's `--mqtt-bind` address, port 1883; the remaining app settings
below are unchanged. Only MQTT ingress listens on that LAN address, restricted to the phone.
The WebSocket stays on `127.0.0.1`, `task-screen` remains the task-event producer, and both live
browser pages open. The source stays MQTT after a bridge restart; it never falls back to synthetic.
The launcher owns the same narrow temporary firewall rule and cleanup described below.
Do not use a public/untrusted or venue network merely because its IP is private.

`--single-display-test` is an explicit **test-only** layout: task in the left 65%, attendant
console upper right, spectator preview lower right. Both browser windows are windowed and their
full-screen buttons are hidden so they do not cover the controls. The preview is not a test of
across-the-hall readability; the booth still needs its separate spectator display. All roles
use `--console-display` (default 0). Conflicting display indices, `--booth` and `--headless`
are refused with this flag. Omit it when using the normal two-display browser-test layout.

The example geometry assumes a 15-inch 16:9 screen (about 33.2 cm wide) at 80 cm, **not a measured
screen**. Replace it with measured width/distance before judging difficulty. The task uses the
actual tiled viewport width and shows its geometry in the debug corner. `body` is the default
PSV source; confirm `PSV: body` on the console (B selects it). No start is automatic: wait for
flowing PPI, check fit and low-volume wired output, then use the console's Space/Enter button.
For a full run, move focus to the task after starting, rest both hands during baseline, use only
the right-hand mouse during load, and rest again during regulate/resolve. Q in the console
shuts down cleanly and removes the owned firewall rule. Logs remain in gitignored `logs/`.

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

## Headset test mode

For developing the Unity app against the real laptop side without an armband or a booth:

```powershell
.venv\Scripts\python.exe -m tools.launch --headset-test
.venv\Scripts\python.exe -m tools.launch --headset-test --lan-ip 192.168.0.125 --quest-ip 192.168.0.150
```

Both use the synthetic heart, labelled `SYNTHETIC INPUT - NO ARMBAND`, select `quest` as the
one task-event producer and open **no browser task**. On one screen the spectator preview
takes the left 65% and the attendant console the right 35%, both windowed; pass
`--spectator-display 1` to give the spectator its own screen instead. The sound check and the
attendant button work as in every other mode.

- **Without addresses** the WebSocket listens on `127.0.0.1` only, for Unity Play mode on the
  same laptop. No LAN listener, no firewall rule, no elevation.
- **With `--lan-ip` and `--quest-ip`** (both or neither) it listens on that one local RFC1918
  address, for the real Quest. Run it from **Administrator PowerShell**: the launcher creates a
  uniquely named `PrismQuestLink-<id>` inbound rule with the same scope as the phone's rule
  below, TCP only, the exact headset source address, the exact laptop address and WebSocket
  port, the selected interface, its current profile and this Python image, and removes only
  that rule on Q, Ctrl+C or a startup failure. The addresses above are examples. Unity Play
  mode on the laptop must then use the LAN address too, because nothing listens on localhost.

The rule is the only restriction on who can reach the WebSocket: unlike the MQTT relay, the
server does not itself refuse other source addresses, and an existing broader exception for
Python is neither audited nor changed. Own router only, never venue WiFi. `--headset-test`
refuses `--booth`, `--single-display-test` and `--task-display`. The launcher does not start,
supervise or prove a connection from the headset.

### Headset test with the real armband (10 October)

The same layout and Quest link, with the phone's measured PPI in place of the synthetic heart.
From **Administrator PowerShell**, on your own router:

```powershell
.venv\Scripts\python.exe -m tools.launch --headset-test --lan-ip 192.168.1.175 --quest-ip 192.168.1.152 --packet-source mqtt --mqtt-phone-ip 192.168.1.196
```

Those are the 10 October home-router addresses, not defaults. All three addresses are
required; MQTT listens on `--lan-ip` (`--mqtt-bind` is refused here) and the phone's settings
are the table in the runbook below with that address as the broker. The launcher owns **two**
rules and nothing wider: `PrismPolarMqtt-<id>` admits only the phone on the MQTT port and
`PrismQuestLink-<id>` only the Quest on the WebSocket port, each with the exact laptop
address, interface, profile and Python image described below. Both names and their removal
commands are printed before either is created. They are created one after the other, so
allow up to a minute before the console appears. If the second cannot be created the first is
removed; on Q, Ctrl+C or a failure both are removed, and a failure removing one does not stop
the attempt on the other. The console's second line reads `POLAR SENSOR LOGGER / MQTT /
MEASURED PPI, BUFFERED PLAYBACK`, never `SYNTHETIC INPUT - NO ARMBAND`. `--booth` still adds
no Quest rule of its own.

Ctrl+C during rule setup finishes the creation in flight, starts no other, and then removes
what exists; a second Ctrl+C no longer cuts that wait short. If both removals fail, both rule
names are reported.

**Review:** one heavy-review round of two agents on the firewall scoping. One compared old
and new launchers over 32,400 argument combinations with a recording fake in place of the
rule and found no wrong, extra or wider rule and no change to an existing mode. The other
found the two-removal report and the interrupted-setup behaviour above, fixed afterwards
with regression self-checks only. No rule has been created by this mode on the laptop yet.

### The spectator under an elevated launch (10 October)

On 9 and 10 October every LAN headset test left the spectator window missing. Edge 154,
started from an elevated process, relaunches itself unelevated through Explorer. That copy is
outside the launcher's Job and cannot open the profile folder the elevated launcher created,
so no window appeared, the first process exited 0 and the supervisor started another about
every two seconds (778 times in one half-hour run). Owned browsers are now started with
Edge's `--do-not-de-elevate`, so the browser stays in the Job as it did on 26 September.
This was confirmed in the logs and by an unelevated run with the flag; **an elevated launch
with the flag has not yet been run.** If the spectator is still missing, `spectator` lines
repeating `process exited 0` in `logs/launcher/supervisor.jsonl` mean the flag was not
honoured. The earlier runs also left about 2,800 `prism-live-spectator-*` profile folders in
the Windows temporary directory, removable only from an elevated prompt.

**Windows Mobile Hotspot.** The Quest can join the laptop's own hotspot instead of a router;
`--lan-ip` is then the hotspot adapter's address, normally `192.168.137.1`. Windows gives that
adapter no network profile, so the rule there carries no profile condition. It is still bound
to that one adapter, both addresses, the port and this Python image. The launcher refuses an
adapter whose name could select a second interface.

If rule setup fails, the launcher prints what PowerShell itself reported and the step it was
on, and a failure while shutting down no longer replaces the error that caused the shutdown.
Creating the rule can take 10 to 25 s on this laptop.

As of 9 October 2026 the rule script has been run on this laptop without elevation only: it
resolves the hotspot adapter and stops where Windows refuses an unelevated change. A rule
has not yet been created from an elevated launch here.

## Phone and router runbook (replacement for direct laptop BLE / prompt 2.8)

**Fit and posture standard (26 September).** The attendant fits the armband on the **thick part of the left forearm** and physically checks the fit before starting. Both hands rest in the lap; no phone use by the participant. Keep the left arm resting for the whole experience. For the browser task test, only the right hand moves to the mouse during load; both hands rest again in regulate and resolve. Fit checking remains the attendant's responsibility even when packets are flowing. Neither Polar's reported HR nor its skin-contact flag verifies a good fit or wear.

**HR for decisions comes from accepted PPI intervals, never Polar's separate HR number.** The latter is diagnostic only, including when carried inside a PPI sample. Do not use it to decide baseline quality, activation, regulate outcome, confidence, wear or when to start/stop. A low device HR is not a reason to override interval-derived session decisions; see the paired-rest finding in `docs/known-limits.md`.

The travel router **must reserve fixed DHCP addresses for both laptop and Android phone**.
Use the phone's per-network stable MAC when configuring its reservation; verify the addresses
after reconnecting. If either address changes, stop the launcher and correct the reservations
and command. Do not widen the allowed source to a subnet. Do not port-forward either MQTT port.

Prepare the optional production MQTT dependencies using the project's `mqtt` extra in the
project environment (`python -m pip install -e ".[mqtt]"`); this is an operator setup step, not
an automatic installation by the launcher. Run either MQTT mode from **Administrator PowerShell**
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
An adapter with no Windows network profile, such as the Mobile Hotspot adapter, gets the same
rule without a profile condition. The rule can be scoped to a trusted router labelled Public by Windows;
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
Except for explicit `--single-display-test`, the spectator cannot share the console or task display.
Missing displays are an error before
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

## Mandatory audio sound check

Plug in the visitor's **wired headphones**, select them as the Windows output, and set a low safe
volume **before launching Prism**. The shim pins the endpoint it opens and deliberately does not
follow a later default-device change.

Every new bridge process automatically plays a short piece of the loaded Prism scene through the
real path: engine render, high-pass, session gain, limiter and the opened output device. The
attendant console shows `AUDIO DEVICE: <name>` using the endpoint name reported by that opened
stream. Read the name, listen through the visitor headphones, and wait for the whole check to end.
Then press Space or Enter once at `NEXT PRESS: CONFIRM SOUND HEARD`. That press only confirms the
check; it does not arm a session. The following press can arm the first visitor.

The first session is locked until a person confirms that they actually heard the check. An endpoint
name or moving audio counter alone is not proof of audible sound. Press R to replay an unconfirmed
check. If the named device is wrong or the sound is missing, press Q, correct the Windows output,
cable, 48 kHz setting and volume, then relaunch. Never confirm a check that was not heard.

Confirmation lasts for later visitors while that bridge process and its opened stream remain alive.
A bridge restart opens the device again, plays the check again, and requires a new confirmation;
restarting only a browser does not. The non-interactive `bridge.server --one-session` timing
diagnostic bypasses this human interlock and is not a booth launch path.

## The attendant button

### Start rule: a settling minute, then a heart rate that has stopped falling (9 October)

**Do not arm a start until the visitor has sat still for a full minute and the console's heart
rate has stopped falling.** The console shows `HEART RATE: 84 bpm | 30 s ago: 96 bpm` from the
host's own state (accepted intervals, never Polar's separate HR number) and repeats the rule
while idle. Wait until the two numbers are within a few beats of each other.

Why it matters: the baseline's quality score falls to 0 when heart rate drifts by about 12 bpm
per minute or more across the 45-second capture, roughly 9 bpm over the window. That score
multiplies arousal and readiness confidence for **the rest of the session**, so a visitor who is
still coming down from the hall when baseline starts gets a session in which the heart has no
authority at all. Cognitive load cannot carry it: its evidence ends with the task, a few
seconds into regulate. This was seen in headset test session S-20261008-0008 with the synthetic
heart, which loops a four-minute profile; a start pressed on its down-ramp does the same.

The console displays and reminds; it does not judge, block or delay a start, and the session
decides nothing from this line. It is the attendant's call, alongside the fit check.

With the attendant terminal focused, Space or Enter is the same single button:

- Before the first session: confirm the completed sound check; a separate next press arms.
- Idle: arm and show the pulse-aligned countdown, up to 11 s.
- Countdown: cancel and disarm. The session has not started.
- Running: stop through the host's 3 s reset.
- Reset: wait; another press cannot shorten it or queue a new visitor.

The console always says what the next press will do. R replays an unconfirmed sound check. B and P
switch the existing body/pose PSV source; Q exits the launcher and its processes. Signal loss makes
the whole console red but never auto-stops.
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

**Edge startup handoff (26 September).** From Administrator PowerShell, Edge's initial process
can exit successfully while its real browser continues inside the same launcher-owned Windows
Job. That exit alone is not a crash: the supervisor retains the Job and checks the actual page
and connection on the usual deadlines. The browser PID in status comes from browser telemetry
and must belong to that Job; `wrapper_pid` records the original process. An empty Job, a nonzero
initial exit or failed page health still triggers recovery. Q still closes the entire owned
process family. No browser security flag, UAC setting or external browser process is changed.
Visible-window lookup also uses the owned Job, not the exited wrapper PID, and matches the
Prism page title so Edge's own dialogs are not mistaken for the application window. Status
includes the observed window bounds when placement fails. Owned pages open in InPrivate mode
to avoid the account sign-in/sync setup dialog that appeared in fresh app-mode profiles on this
laptop. This changes only the launcher-owned browser invocation, not the user's Edge settings;
InPrivate is not a claim that the browser makes no background network requests.

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

## What the session log records about activation and the phone link (9 October)

Everything here is written to the gitignored per-session JSONL under `logs/`. None of it is
sent to a screen or the headset, and none of it changes a decision.

**Activation.** One answer per session, by the same heart-rate test that lets a spoken N exist:
HR_load at least 6 bpm above HR_base. RMSSD never counts toward it.

- `activation_result`, when regulate ends: `activated`, `judged`, the two heart rates, the rise,
  and `not_judged` with the reason when it could not be judged (degraded baseline, too little of
  load's last 30 s, or a stop before load's last intervals had settled).
- `session_end`: `activated` and `activation_judged`. A session that never reached the end of
  load is `activated: false, activation_judged: false`.
- `regulate_result.outcome` and the `segment` reason are `regulated` only for an activated
  session. A threshold met without activation is `met_not_activated`. Regulate still ends at
  exactly the same moment; only the label differs. `regulate_met` and `first_met_ms` still record
  that the threshold was met.
- `load_activation.activated` now follows the same rule. The experience script's wider test,
  heart rate or RMSSD, is kept as `by_hr_or_rmssd`, with `by_hr` and `by_rmssd` beside it.
- `activated` does not mean an N was shown: N also needs all three cards and a fall of at least
  3 bpm.

**The phone link.**

- `mqtt_phone_connected`, `mqtt_phone_disconnected`: the phone's TCP connection through the LAN
  relay. `mqtt_connection_refused` with the reason for anything turned away.
- `mqtt_subscriber_connected`, `mqtt_subscriber_disconnected`: the bridge's own subscription to
  the laptop's loopback broker, stamped with the time Paho reported it and its reason.
- `ppi_burst`, one per burst handed to the scheduler: `arrived_ms` is the arrival of the burst's
  last message, `t_engine` when the bridge handled it, with `intervals_accepted`,
  `intervals_refused`, and `ignored` when a whole burst was skipped.
- `interval_rejected`, one per refused interval, with its reason: `blockerBit set`,
  `errorEstimate ... over 10% of the interval`, `outside 300 to 2000 ms`,
  `more than 20% from the recent median ...`, or `overlaps the measurement timeline already
  consumed`. The synthetic one-second path logs its refusals the same way.
- `ppi_message_not_used`: a PPI message that arrived but never reached the scheduler, with its
  arrival and why (`probation`, `duplicate`, `malformed`, `old_message`, `phone_backlog`,
  `stale_callback`, `stale_burst`, `burst_overflow`, `retained`, `wrong_identity`).

What it does not record: an accepted interval later dropped as too late to schedule shows only
in the running `not_scheduled_total`; the HRV cleaner's exclusions and the legacy path's
contact rule are not itemised; the subscriber closing at shutdown is not logged. Because the
phone streams between visitors, these lines are also written while idle, into the next
session's file: roughly 1 MB an hour with the armband off the arm.

**Review:** one heavy-review round of two agents, then regression self-checks only. One
compared the old and new schedulers on identical inputs (about 210,000 PPI bursts and 331,000
one-second packets) and found results, counters and every published message identical; the
other found that a stop in regulate's first seconds could record activation from heart-rate
data that had not finished arriving, which is why such a session is now `judged: false`.

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
