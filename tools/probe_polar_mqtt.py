"""Throwaway Polar Sensor Logger MQTT capture; never imports or modifies the bridge.

Install amqtt==0.11.3 and paho-mqtt==2.1.0 in an isolated disposable environment.
This is plaintext MQTT for a TRUSTED PRIVATE LAN ONLY, with a phone-IP allowlist.
It changes neither Windows Firewall nor router settings. Do not port-forward it.

    python -m tools.probe_polar_mqtt --self-test
    python -m tools.probe_polar_mqtt --bind 192.168.1.10 --phone-ip 192.168.1.20
    python -m tools.probe_polar_mqtt --capture DIR --mark ppi_start_requested
    python -m tools.probe_polar_mqtt --capture DIR --mark ppi_start_confirmed --note "Tapped"
    python -m tools.probe_polar_mqtt --capture DIR --mark off --note "Confirmed off arm"
    python -m tools.probe_polar_mqtt --capture DIR --mark on --note "Confirmed on arm"
    python -m tools.probe_polar_mqtt --capture DIR --mark stop

The broker is memory-only, localhost-only, and loads only anonymous authentication.
The LAN TCP relay accepts ONLY the supplied phone IP. Subscribe before opening it.
No schema/epoch/units are presumed: exact MQTT application payloads are base64 saved
alongside lossless integer JSON decoding. QoS/retain describe subscriber delivery,
NOT necessarily the publisher's original flags. Capture time is the local Paho
callback time, not radio receipt, broker TCP receipt, acquisition time or onset.
Manual markers record confirmation time; they are not precise phone-side events.
Physical wear state is never inferred from a contact flag. This is not bridge code.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import importlib.metadata
import ipaddress
import json
import logging
import math
import platform
import re
import socket
import statistics
import time
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def stamp():
    wall_ns = time.time_ns()
    return {
        "utc": datetime.fromtimestamp(wall_ns / 1e9, UTC).isoformat(),
        "time_ns": wall_ns,
        "perf_counter_ns": time.perf_counter_ns(),
    }


def private_ipv4(value):
    address = ipaddress.ip_address(value)
    if address.version != 4 or not any(address in network for network in PRIVATE_NETWORKS):
        raise ValueError("Use one explicit RFC1918 LAN IPv4 address, never 0.0.0.0 or a public IP")
    return str(address)


def allowed_peer(peer, allowed):
    return bool(peer) and peer[0] == allowed


def reject_constant(value):
    raise ValueError(f"Not a JSON number: {value}")


def payload_record(topic, payload, qos, retain, dup=False, received=None):
    record = {
        "kind": "mqtt_message",
        **(received or stamp()),
        "topic": topic,
        "qos": qos,
        "retain": bool(retain),
        "dup": bool(dup),
        "payload_length": len(payload),
        "payload_base64": base64.b64encode(payload).decode("ascii"),
    }
    try:
        record["payload_utf8"] = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        record["decode_error"] = str(error)
    else:
        try:
            record["json"] = json.loads(record["payload_utf8"], parse_constant=reject_constant)
        except ValueError as error:
            record["json_error"] = str(error)
    return record


def distribution(values):
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": statistics.median(ordered),
        "p95": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)],
        "max": ordered[-1],
    }


def inventory(value, fields, arrays, path="$", depth=0):
    fields[path][type(value).__name__] += 1
    if depth >= 32:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            inventory(child, fields, arrays, f"{path}.{key}", depth + 1)
    elif isinstance(value, list):
        arrays[path].append(len(value))
        for child in value:
            inventory(child, fields, arrays, f"{path}[]", depth + 1)


class Capture:
    def __init__(self, directory, *, synthetic=False):
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "markers").mkdir()
        self.directory = directory
        self.file = (directory / "raw.jsonl").open("x", encoding="utf-8", buffering=1)
        self.synthetic = synthetic
        self.phase = "unconfirmed_wear_state"
        self.times = defaultdict(list)
        self.phase_counts = Counter()
        self.retained_counts = Counter()
        self.fields = defaultdict(lambda: defaultdict(Counter))
        self.arrays = defaultdict(lambda: defaultdict(list))
        self.queue_delays = []
        self.marker_files = set()
        self.markers = []
        self.started = stamp()
        self.closed = False

    def write(self, kind=None, **record):
        if kind:
            record = {"kind": kind, **stamp(), **record}
        record.setdefault("phase", self.phase)
        record["synthetic"] = self.synthetic
        self.file.write(json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n")

    def message(self, record):
        queue_delay_ms = (time.perf_counter_ns() - record["perf_counter_ns"]) / 1e6
        record["callback_to_log_ms"] = queue_delay_ms
        self.queue_delays.append(queue_delay_ms)
        topic = record["topic"]
        self.times[topic].append(record["perf_counter_ns"])
        self.phase_counts[(self.phase, topic)] += 1
        self.retained_counts[topic] += int(record["retain"])
        if "json" in record:
            inventory(record["json"], self.fields[topic], self.arrays[topic])
        self.write(**record)
        if len(self.times[topic]) == 1:
            print(
                f"FIRST TOPIC {topic!r}: {record.get('payload_utf8', '<binary>')[:2000]}",
                flush=True,
            )

    def poll_markers(self):
        stop = False
        paths = sorted((self.directory / "markers").glob("*.json"))
        for path in paths:
            if path.name in self.marker_files:
                continue
            try:
                marker = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # A separate process may still be writing the new marker file.
                continue
            self.marker_files.add(path.name)
            self.markers.append(marker)
            name = marker.get("name")
            if name == "off":
                self.phase = "user_confirmed_off_arm"
            elif name == "on":
                self.phase = "user_confirmed_on_arm"
            self.write("marker", marker=marker, observed_at=stamp())
            print(f"MARKER {name}: {marker.get('note', '')}", flush=True)
            stop |= name == "stop"
        return stop

    def summary(self, reason):
        topics = {}
        for topic, times in self.times.items():
            topics[topic] = {
                "messages": len(times),
                "retained_deliveries": self.retained_counts[topic],
                "interarrival_ms": distribution(
                    [(b - a) / 1e6 for a, b in zip(times, times[1:], strict=False)]
                ),
                "first_callback_perf_counter_ns": times[0],
                "last_callback_perf_counter_ns": times[-1],
                "json_field_inventory": self.fields[topic],
                "json_array_lengths": {
                    key: distribution(values) for key, values in self.arrays[topic].items()
                },
            }
        return {
            "synthetic": self.synthetic,
            "started": self.started,
            "ended": stamp(),
            "stop_reason": reason,
            "topics": topics,
            "phase_message_counts": [
                {"phase": phase, "topic": topic, "count": count}
                for (phase, topic), count in self.phase_counts.items()
            ],
            "callback_to_log_ms": distribution(self.queue_delays),
            "markers": self.markers,
            "limitations": [
                "Receipt time is subscriber callback time, after forwarding and OS scheduling.",
                "No synchronized phone/device clock or device-side start timestamp is available.",
                "Manual markers timestamp human confirmation, not the phone action itself.",
                "A PPI batch received off-arm may include earlier on-arm measurements.",
                "JSON numbers remain Python integers when their literal is an integer.",
                "Array lengths are schema inventory, not assumed beat counts.",
                "QoS and retain flags describe subscriber delivery, not original publication.",
            ],
        }

    def close(self, reason):
        if self.closed:
            return
        self.closed = True
        self.write("capture_ended", reason=reason)
        self.file.close()
        with (self.directory / "summary.json").open("x", encoding="utf-8") as output:
            json.dump(self.summary(reason), output, indent=2, allow_nan=False)


def make_broker(port):
    from amqtt.broker import Broker

    return Broker(
        {
            "listeners": {
                "default": {"type": "tcp", "bind": f"127.0.0.1:{port}", "max_connections": 8}
            },
            "plugins": {
                "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}
            },
            "timeout_disconnect_delay": 0,
            "session_expiry_interval": 60,
        }
    )


class Subscriber:
    def __init__(self, loop, queue):
        import paho.mqtt.client as mqtt

        self.loop = loop
        self.queue = queue
        self.ready = asyncio.Event()
        self.error = None
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"prism-throwaway-capture-{uuid.uuid4().hex[:8]}",
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )
        self.client.on_connect = self.on_connect
        self.client.on_subscribe = self.on_subscribe
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect
        self.stopping = False

    def fail(self, reason):
        self.error = str(reason)
        self.ready.set()

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.loop.call_soon_threadsafe(self.fail, f"MQTT connect: {reason_code}")
            return
        result, _mid = client.subscribe([("#", 2), ("$SYS/#", 2)])
        if result != 0:
            self.loop.call_soon_threadsafe(self.fail, f"MQTT subscribe returned {result}")

    def on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if len(reason_codes) != 2 or any(reason.is_failure for reason in reason_codes):
            self.loop.call_soon_threadsafe(self.fail, f"MQTT subscribe rejected: {reason_codes}")
        else:
            self.loop.call_soon_threadsafe(self.ready.set)

    def on_message(self, client, userdata, message):
        # Only timestamp and copy here: neither JSON parsing nor disk I/O in this thread.
        received = stamp()
        item = (
            message.topic,
            bytes(message.payload),
            message.qos,
            message.retain,
            message.dup,
            received,
        )
        self.loop.call_soon_threadsafe(self.queue.put_nowait, item)

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        if not self.stopping:
            self.loop.call_soon_threadsafe(
                self.fail, f"Capture subscriber disconnected: {reason_code}"
            )

    async def start(self, port):
        await asyncio.to_thread(self.client.connect, "127.0.0.1", port, 30)
        self.client.loop_start()
        await asyncio.wait_for(self.ready.wait(), 10)
        if self.error:
            raise RuntimeError(self.error)

    async def stop(self):
        self.stopping = True
        self.client.disconnect()
        await asyncio.to_thread(self.client.loop_stop)


class Relay:
    def __init__(self, phone_ip, broker_port, log):
        self.phone_ip = phone_ip
        self.broker_port = broker_port
        self.log = log
        self.tasks = set()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream_writer = None
        transfers = []
        peer = writer.get_extra_info("peername")
        try:
            if not allowed_peer(peer, self.phone_ip):
                self.log("relay_rejected", peer_ip=peer[0] if peer else None)
                return
            self.log("phone_tcp_connected", peer_ip=peer[0], peer_port=peer[1])
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", self.broker_port
            )

            async def copy(source, destination):
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()

            transfers = [
                asyncio.create_task(copy(reader, upstream_writer)),
                asyncio.create_task(copy(upstream_reader, writer)),
            ]
            done, _pending = await asyncio.wait(transfers, return_when=asyncio.FIRST_COMPLETED)
            for completed in done:
                completed.result()
        except (ConnectionError, OSError) as error:
            self.log("relay_connection_error", error=str(error))
        finally:
            for transfer in transfers:
                transfer.cancel()
            await asyncio.gather(*transfers, return_exceptions=True)
            for stream in (upstream_writer, writer):
                if stream:
                    stream.close()
                    with contextlib.suppress(ConnectionError, OSError):
                        await stream.wait_closed()
            self.tasks.discard(task)
            if allowed_peer(peer, self.phone_ip):
                self.log("phone_tcp_disconnected", peer_ip=peer[0])

    async def stop(self):
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def capture(args):
    directory = args.capture or ROOT / "private-data" / "physiology" / "captures" / (
        "polar-mqtt-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    )
    data = Capture(directory)
    data.write(
        "capture_started",
        bind=args.bind,
        phone_ip=args.phone_ip,
        port=args.port,
        broker_bind=f"127.0.0.1:{args.broker_port}",
        max_duration_s=args.duration,
        python=platform.python_version(),
        platform=platform.platform(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        versions={name: importlib.metadata.version(name) for name in ("amqtt", "paho-mqtt")},
    )
    broker = make_broker(args.broker_port)
    queue = asyncio.Queue()
    subscriber = Subscriber(asyncio.get_running_loop(), queue)
    relay = Relay(args.phone_ip, args.broker_port, data.write)
    server = None
    broker_started = False
    reason = "unknown"

    def drain():
        while not queue.empty():
            data.message(payload_record(*queue.get_nowait()))

    try:
        await broker.start()
        broker_started = True
        await subscriber.start(args.broker_port)
        data.write("subscriber_ready", subscriptions=["#", "$SYS/#"])
        server = await asyncio.start_server(
            relay.handle, args.bind, args.port, family=socket.AF_INET
        )
        data.write("phone_listener_ready")
        print(f"CAPTURE {directory}", flush=True)
        print(f"READY tcp://{args.bind}:{args.port}; phone only {args.phone_ip}", flush=True)
        print(
            "No TLS; username/password blank. No router forwarding. Trusted LAN only.", flush=True
        )
        end = time.monotonic() + args.duration
        last_status = 0
        while time.monotonic() < end:
            drain()
            if data.poll_markers():
                reason = "manual_stop_marker"
                break
            if subscriber.error:
                raise RuntimeError(subscriber.error)
            now = time.monotonic()
            if now - last_status >= 10:
                counts = {topic: len(times) for topic, times in data.times.items()}
                print(f"STATUS {stamp()['utc']} phase={data.phase} topics={counts}", flush=True)
                last_status = now
            await asyncio.sleep(0.05)
        else:
            reason = "maximum_duration"
    except asyncio.CancelledError:
        reason = "interrupted"
        raise
    except Exception as error:
        reason = f"{type(error).__name__}: {error}"
        data.write("capture_error", error=reason)
        raise
    finally:
        if server:
            server.close()
            await server.wait_closed()
        await relay.stop()
        await subscriber.stop()
        # Run queued thread callbacks before the last drain and closing the raw file.
        await asyncio.sleep(0)
        drain()
        if broker_started:
            await broker.shutdown()
        data.close(reason)
        print(f"STOPPED {reason}; capture and summary: {directory}", flush=True)


def write_marker(directory, name, note):
    if not directory or not (directory / "raw.jsonl").is_file():
        raise ValueError("--capture must name an existing capture directory")
    if (directory / "summary.json").exists():
        raise ValueError("Capture is already finished; refusing a misleading late marker")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
        raise ValueError("Marker names must contain only lowercase letters, digits and underscores")
    marker = {"name": name, "note": note, **stamp()}
    path = directory / "markers" / f"{marker['time_ns']}-{uuid.uuid4().hex}.json"
    with path.open("x", encoding="utf-8") as output:
        json.dump(marker, output)
    print(json.dumps({"marker_path": str(path), **marker}), flush=True)


async def self_test():
    """Only loopback listeners and synthetic bytes; never creates a hardware fixture."""
    import paho.mqtt.client as mqtt

    ns = 843212345678901234
    payload = json.dumps({"timestamp": ns, "data": [{"ppi": 812}]}).encode()
    decoded = payload_record("test", payload, 1, False)
    assert decoded["json"]["timestamp"] == ns
    assert isinstance(decoded["json"]["timestamp"], int)
    assert base64.b64decode(decoded["payload_base64"]) == payload
    assert "decode_error" in payload_record("test", b"\xff\x00", 0, False)
    assert allowed_peer(("192.168.1.5", 12), "192.168.1.5")
    assert not allowed_peer(("192.168.1.6", 12), "192.168.1.5")
    for address in ("0.0.0.0", "8.8.8.8", "127.0.0.1", "169.254.1.1", "::1"):
        try:
            private_ipv4(address)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Unsafe bind accepted: {address}")
    assert private_ipv4("192.168.1.5") == "192.168.1.5"

    broker = make_broker(18884)
    queue = asyncio.Queue()
    subscriber = Subscriber(asyncio.get_running_loop(), queue)
    events = []
    relay = Relay("127.0.0.1", 18884, lambda kind, **fields: events.append((kind, fields)))
    reject = Relay("192.168.1.5", 18884, lambda kind, **fields: events.append((kind, fields)))
    publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
    server = reject_server = None
    try:
        await broker.start()
        await subscriber.start(18884)
        server = await asyncio.start_server(relay.handle, "127.0.0.1", 18883)
        reject_server = await asyncio.start_server(reject.handle, "127.0.0.1", 18885)
        rejected_reader, rejected_writer = await asyncio.open_connection("127.0.0.1", 18885)
        assert await asyncio.wait_for(rejected_reader.read(1), 2) == b""
        rejected_writer.close()
        await rejected_writer.wait_closed()
        assert any(kind == "relay_rejected" for kind, _ in events)
        await asyncio.to_thread(publisher.connect, "127.0.0.1", 18883, 30)
        publisher.loop_start()
        publication = publisher.publish("synthetic/ppi", payload, qos=1)
        await asyncio.to_thread(publication.wait_for_publish, 5)
        received = payload_record(*await asyncio.wait_for(queue.get(), 5))
        assert received["topic"] == "synthetic/ppi"
        assert base64.b64decode(received["payload_base64"]) == payload
        assert received["json"]["timestamp"] == ns
        print(
            "SELF-TEST PASSED: raw bytes, integer ns, allowlist, MQTT relay/pub/sub; SYNTHETIC ONLY"
        )
    finally:
        publisher.disconnect()
        await asyncio.to_thread(publisher.loop_stop)
        for listener in (server, reject_server):
            if listener:
                listener.close()
                await listener.wait_closed()
        await relay.stop()
        await reject.stop()
        await subscriber.stop()
        await broker.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", help="One laptop RFC1918 IPv4 on your trusted private LAN")
    parser.add_argument("--phone-ip", help="Only this Android phone IPv4 may reach the LAN relay")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--broker-port", type=int, default=1884)
    parser.add_argument("--duration", type=float, default=1800)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--mark")
    parser.add_argument("--note", default="")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    if args.mark:
        write_marker(args.capture, args.mark, args.note)
        return
    if args.self_test:
        asyncio.run(self_test())
        return
    if not args.bind or not args.phone_ip:
        parser.error("--bind and --phone-ip are required; use only a trusted private LAN")
    try:
        args.bind = private_ipv4(args.bind)
        args.phone_ip = private_ipv4(args.phone_ip)
    except ValueError as error:
        parser.error(str(error))
    if args.bind == args.phone_ip:
        parser.error("Laptop and phone addresses must differ")
    if not all(1024 <= port <= 65535 for port in (args.port, args.broker_port)):
        parser.error("Ports must be between 1024 and 65535")
    if args.port == args.broker_port:
        parser.error("LAN relay and localhost broker ports must differ")
    if not math.isfinite(args.duration) or not 1 <= args.duration <= 3600:
        parser.error("--duration must be 1 to 3600 seconds")
    try:
        asyncio.run(capture(args))
    except KeyboardInterrupt:
        print("Interrupted; broker and listener shut down.", flush=True)


if __name__ == "__main__":
    main()
