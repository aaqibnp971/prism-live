# Throwaway Polar Sensor Logger MQTT probe

This is a hardware/transport experiment, not bridge code. The app runs on Android
and uses Polar's SDK; its MQTT messages are recorded by a local laptop broker.
Do not interpret optical PPI as standard BLE ECG RR or add a production adapter
until the actual fields, flags and latency have been established.

## This run's addresses and Android settings

Checked 23 September 2026: laptop Wi-Fi `192.168.1.201`; user-reported phone Wi-Fi
`192.168.1.135`. These can change after DHCP renewal. Recheck before another run.

In Polar Sensor Logger, enable the **MQTT** setting, then enter:

| Field | Value |
| --- | --- |
| MQTT-broker address | `192.168.1.201` (no `http://`) |
| Port | `1883` |
| Topic | `prism-probe` |
| Client ID | `verity-phone` |
| Credentials, if offered | blank, for this temporary IP-restricted local probe |
| TLS, if offered | off; use only a trusted local Wi-Fi network |

Keep **SDK mode off**: Verity Sense does not provide HR/PPI in that mode. Select
PPI under SDK data selection when asked to start its measurement; HR may be
published independently. Leave ECG/PPG/accelerometer/gyro/magnetometer unselected
for the first test. The collector subscribes to all ordinary topics (`#`) and
`$SYS/#`, not a guessed `/ppi` schema. Keep the phone app foregrounded initially.
Do not start PPI before the collector is ready and the start marker is recorded.

The settings labels are documented in a
[firsthand integration example](https://github.com/brugr9/Heartbeat51#26-polar-sensor-logger).
The [app author's listing](https://play.google.com/store/apps/details?id=com.j_ware.polarsensorlogger)
confirms MQTT, HR and PPI support, but does not publish a complete MQTT PPI schema.
The app is unofficial even though it uses the official SDK.

## Firewall: one phone, one address, one port

This laptop currently classifies Wi-Fi as **Public**. Do not disable the firewall,
allow Python broadly, change the whole profile, or open a router port. On a trusted
local network, run this scoped temporary rule in **PowerShell as Administrator**:

```powershell
New-NetFirewallRule -Name "PrismPolarMqttProbe" -DisplayName "Prism Polar MQTT probe" -Direction Inbound -Action Allow -Protocol TCP -LocalAddress 192.168.1.201 -LocalPort 1883 -RemoteAddress 192.168.1.135 -InterfaceAlias "Wi-Fi" -Profile Public
```

It allows inbound TCP only from the phone's current IP to the laptop's current
Wi-Fi IP on 1883, for the currently observed profile. No UDP rule is needed. The
probe also rejects other source IPs. Its actual MQTT broker listens on loopback
only; a phone-IP-restricted TCP relay exposes the single LAN listener. There is
no cloud broker, external MQTT bridge, persistent service or TLS on this path.
IP restriction is not encryption; do not use this setup on venue/public Wi-Fi.

After the run, remove exactly this temporary rule:

```powershell
Remove-NetFirewallRule -Name "PrismPolarMqttProbe"
```

No firewall changes are made by the Python probe. For the 23 September capture,
the user installed and later removed the rule; both the exact rule scope during
capture and its absence afterward were verified with read-only Windows checks.
[Microsoft rule documentation](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule)

## Observation protocol

1. Confirm subscriber readiness and receipt of real phone messages. Capture HR
   for 60 seconds if available with PPI stopped.
2. Mark `ppi_start_requested`, ask the user to start PPI, and mark
   `ppi_start_confirmed` when their reply arrives. If the app publishes a start
   event, retain it too. Without an SDK acknowledgement or directly timestamped
   button event, startup latency is **bracketed/approximate**, not an exact
   command-to-first-beat measurement. Already-running PPI cannot answer it.
3. Observe on-arm at rest for 90 seconds after PPI first arrives. Keep every
   field and raw payload, including blocked or suspect samples.
4. Ask for removal; mark the user's confirmation as `off`. Observe at least
   30 seconds, then ask for replacement and mark `on`. Chat-confirmation times
   are not exact physical-action times. Do not infer wear state from flags.
5. Observe another 90 seconds on-arm, then stop the collector and broker. Report
   all streams, sample fields/units, message and batch cadence, and gaps.

The collector saves exact payload bytes as base64 plus JSON when parseable, topic,
QoS/retained/duplicate metadata, host wall and monotonic receipt timestamps, and
explicit action markers. JSON integers are preserved without floating-point
conversion. All real captures go under gitignored `private-data/physiology/captures/`;
derived reports and audio must also remain under `private-data/physiology/`. Synthetic self-tests are
kept separate and never count as physiological evidence.

## How to interpret the timing

The app's FAQ says nanoseconds since 1 January 2000. That must be checked per
field: an app wall timestamp need not be a sensor timestamp. Polar also documents
missing/zero timestamps for HR/PPI, and a device clock can be unset or use local
time. Do not subtract a phone/device timestamp from laptop UTC and call that
one-way latency without establishing the clock relationship.
[Polar time-system documentation](https://github.com/polarofficial/polar-ble-sdk/blob/master/documentation/TimeSystemExplained.md)

Report host-monotonic inter-arrival median/p95/min/max, sample count and interval
span per message/burst, and source timestamp deltas where present. Preserve
retained/duplicate messages and flag them; a retained message is not startup
evidence. Explicitly say if MQTT omits blocker/contact/error flags that the SDK
provides. Missing fields are not zero or valid contact.

The existing scheduler assumes a 1,000 ms playback buffer, 20–1,200 ms last-beat
reporting lag, a 1,500 ms link-gap threshold, interpolation after 2,500 ms without
accepted intervals, and stopping after 5,000 ms. It guarantees at least 300 ms
lead for eligible published lattice beats by skipping late slots; that is **not
a guarantee to replay every measured physical beat**. Compare observed PPI
batching to those assumptions, without changing the scheduler. A bigger replay
buffer can create future lead but also creates more body-to-sound delay; it is
not evidence that the current live mapping works.

Polar documents about 25 seconds before the first PPI batch, HR updates at about
5 seconds when PPI is enabled, and unreliable skin contact with possible nonzero
off-arm HR. These are expectations to test, not results from this run.
[Polar Verity Sense documentation](https://github.com/polarofficial/polar-ble-sdk/blob/master/documentation/products/PolarVeritySense.md)

## Environment

`logs/polar-mqtt-env/` contains aMQTT 0.11.3 and Paho MQTT 2.1.0, isolated from
the project environment. No project dependency manifest was changed. No Windows
service was installed. Broker/subscriber run only for the probe and stop together.

```powershell
.\logs\polar-mqtt-env\Scripts\python.exe -m tools.probe_polar_mqtt --self-test
.\logs\polar-mqtt-env\Scripts\python.exe -u -m tools.probe_polar_mqtt --bind 192.168.1.201 --phone-ip 192.168.1.135 --duration 1800
```

Use the printed capture directory with `--capture DIR --mark NAME --note TEXT`
in a second invocation to record `ppi_start_requested`, `ppi_start_confirmed`,
`off`, `on` or `stop`. `stop` shuts down the complete probe. The 1,800-second
limit is a failsafe, not the required observation duration. No actual phone data
has been demonstrated merely by passing the localhost self-test.
