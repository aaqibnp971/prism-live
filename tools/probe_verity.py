"""Throwaway Verity Sense hardware probe; NO bridge imports or project dependencies.

Use an isolated environment with Bleak. No SDK mode, firmware, recording or pairing changes.
Capture HR for 60 s, start PMD PPI, wait for first packet, observe on-arm for 90 s,
ask for removal, observe off-arm for >=30 s, ask for replacement, observe for 90 s.
Physical actions require explicit markers, not assumptions based on contact flags.

    python -m tools.probe_verity --address ADDRESS
    python -m tools.probe_verity --address ADDRESS --ppi-first
    python -m tools.probe_verity --capture CAPTURE_DIR --mark off --note "User confirmed"
    python -m tools.probe_verity --capture CAPTURE_DIR --mark on --note "User confirmed"
    python -m tools.probe_verity --self-test

Protocol: polarofficial/polar-ble-sdk technical_documentation/online_measurement.pdf,
Table 22 and current SDK PpiData.kt. The PDF inverts bit 2's description relative to
the SDK. Preserve flags_raw; label bit 2 according to the current SDK (1=supported).
Neither bit is ground truth for whether this model is being worn. PPI is optical,
NOT ECG RR. Preserve zero device timestamps as missing, not an epoch timestamp.
--ppi-first is a transport diagnostic: it skips the 60 s HR-only observation,
but keeps HR subscribed during PPI. It does not satisfy the HR-only test.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
import platform
import statistics
import struct
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

HR = "00002a37-0000-1000-8000-00805f9b34fb"
CP = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
DATA = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"
ROOT = Path(__file__).resolve().parents[1]


def utc():
    return datetime.now(UTC).isoformat()


def decode_hr(raw):
    if len(raw) < 2:
        raise ValueError("Truncated Heart Rate Measurement")
    flags = raw[0]
    width = 2 if flags & 1 else 1
    offset = 1 + width
    if len(raw) < offset:
        raise ValueError("Truncated heart rate value")
    result = {
        "flags_raw": flags,
        "hr_bpm": int.from_bytes(raw[1:offset], "little"),
        "hr_value_bits": 8 * width,
        "rr_present": bool(flags & 0x10),
        "contact_supported": bool(flags & 4),
        "contact_detected_bit": bool(flags & 2),
        "energy_expended_present": bool(flags & 8),
        "reserved_flags": flags & 0xE0,
    }
    if flags & 8:
        if len(raw) < offset + 2:
            raise ValueError("Truncated energy expended")
        result["energy_expended_kj"] = int.from_bytes(raw[offset : offset + 2], "little")
        offset += 2
    tail = raw[offset:]
    if (not flags & 0x10 and tail) or len(tail) % 2:
        raise ValueError("Unexpected/truncated HR tail")
    rr = [value[0] for value in struct.iter_unpack("<H", tail)]
    result.update(rr_raw_1024ths=rr, rr_ms=[v * 1000 / 1024 for v in rr])
    return result


def decode_ppi(raw):
    if len(raw) < 10:
        raise ValueError("Truncated PMD header")
    result = {
        "measurement_byte": raw[0],
        "measurement_type": raw[0] & 0x3F,
        "device_timestamp_ns_raw": int.from_bytes(raw[1:9], "little"),
        "frame_type": raw[9] & 0x7F,
        "compressed": bool(raw[9] & 0x80),
    }
    result["device_timestamp_available"] = result["device_timestamp_ns_raw"] != 0
    if result["measurement_type"] != 3 or raw[9] != 0:
        raise ValueError(f"Unsupported PMD type/frame: {raw[0]:02x}/{raw[9]:02x}")
    if (len(raw) - 10) % 6:
        raise ValueError("Truncated PPI sample (expected six bytes)")
    samples = []
    for hr, interval, error, flags in struct.iter_unpack("<BHHB", raw[10:]):
        samples.append(
            {
                "hr_bpm": hr,
                "ppi_ms": interval,
                "error_estimate_ms": error,
                "flags_raw": flags,
                "blocker": bool(flags & 1),
                "skin_contact": bool(flags & 2),
                "skin_contact_supported_sdk": bool(flags & 4),
                "reserved_flags": flags & 0xF8,
            }
        )
    return {**result, "interval_count": len(samples), "samples": samples}


def decode_ack(raw):
    if len(raw) < 4 or raw[0] != 0xF0:
        raise ValueError("Not a complete PMD control response")
    return {
        "opcode": raw[1],
        "measurement_type": raw[2],
        "status": raw[3],
        "more": bool(raw[4]) if len(raw) > 4 and raw[3] == 0 else False,
        "parameters_hex": raw[5:].hex(),
    }


class Capture:
    def __init__(self, path):
        path.mkdir(parents=True, exist_ok=False)
        self.path = path
        self.stream = (path / "raw.jsonl").open("x", encoding="utf-8", buffering=1)
        self.started = time.perf_counter_ns()
        self.phase = "connecting"
        self.events = []

    def emit(self, kind, **details):
        stamp = time.perf_counter_ns()
        row = {
            "kind": kind,
            "utc": utc(),
            "host_monotonic_ns": stamp,
            "elapsed_s": (stamp - self.started) / 1e9,
            "phase": self.phase,
            **details,
        }
        self.events.append(row)
        line = json.dumps(row, separators=(",", ":"))
        self.stream.write(line + "\n")
        self.stream.flush()
        print(line, flush=True)
        return row

    def phase_start(self, phase):
        self.phase = phase
        return self.emit("phase_start", name=phase)


def stats(values):
    if not values:
        return None
    ordered = sorted(values)
    return {
        "count": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "mean": statistics.mean(values),
        "p95": ordered[int(0.95 * (len(ordered) - 1))],
        "max": max(values),
    }


def summarize(events):
    def stream_summary(rows, ppi=False):
        decoded = [r["decoded"] for r in rows if "decoded" in r]
        times = [r["host_monotonic_ns"] / 1e9 for r in rows]
        out = {
            "packets": len(rows),
            "decode_errors": sum("decode_error" in r for r in rows),
            "arrival_interval_s": stats([b - a for a, b in zip(times, times[1:], strict=False)]),
            "raw_lengths": dict(Counter(r["length"] for r in rows)),
        }
        if ppi:
            samples = [s for d in decoded for s in d["samples"]]
            out.update(
                intervals=len(samples),
                intervals_per_packet=dict(Counter(d["interval_count"] for d in decoded)),
                device_timestamps_zero=sum(d["device_timestamp_ns_raw"] == 0 for d in decoded),
                hr_bpm=stats([s["hr_bpm"] for s in samples]),
                ppi_ms=stats([s["ppi_ms"] for s in samples]),
                error_estimate_ms=stats([s["error_estimate_ms"] for s in samples]),
                flags=dict(Counter(s["flags_raw"] for s in samples)),
                blocked=sum(s["blocker"] for s in samples),
                skin_contact=sum(s["skin_contact"] for s in samples),
                skin_contact_supported_sdk=sum(s["skin_contact_supported_sdk"] for s in samples),
                nonzero_hr=sum(s["hr_bpm"] != 0 for s in samples),
            )
        else:
            out.update(
                rr_present_packets=sum(d["rr_present"] for d in decoded),
                flags=dict(Counter(d["flags_raw"] for d in decoded)),
                hr_bpm=stats([d["hr_bpm"] for d in decoded]),
            )
        return out

    ppi = [r for r in events if r["kind"] == "ppi"]
    start = next((r for r in events if r["kind"] == "control_write" and r["hex"] == "0203"), None)
    ack = next(
        (
            r
            for r in events
            if r["kind"] == "control_ack" and r.get("decoded", {}).get("opcode") == 2
        ),
        None,
    )
    first = next((r for r in ppi if r.get("decoded", {}).get("interval_count", 0)), None)
    result = {
        "phases": {},
        "ppi_overall": stream_summary(ppi, True),
        "first_ppi_from_start_write_s": (first["host_monotonic_ns"] - start["host_monotonic_ns"])
        / 1e9
        if first and start
        else None,
        "first_ppi_from_start_ack_s": (first["host_monotonic_ns"] - ack["host_monotonic_ns"]) / 1e9
        if first and ack
        else None,
        "physical_action_markers": [r for r in events if r["kind"] == "action_confirmed"],
        "errors": [r for r in events if r["kind"] in ("error", "cleanup_error")],
        "limitations": [
            "Wear state is user-reported, marked on receipt, not measured by this script.",
            "Arrival-phase labels are not sample acquisition times; PPI is batched/delayed.",
            "Zero PMD timestamps remain missing; arrival cadence is not beat cadence.",
            "Contact bit 2 follows current SDK; 2024 PDF Table22 describes its inverse.",
            "No dropped samples are inferred: PPI has no sequence counter in this frame.",
        ],
    }
    for phase in dict.fromkeys(r["phase"] for r in events):
        group = [r for r in events if r["phase"] == phase]
        result["phases"][phase] = {
            "hr": stream_summary([r for r in group if r["kind"] == "hr"]),
            "ppi": stream_summary([r for r in group if r["kind"] == "ppi"], True),
        }
    return result


async def run(args, capture):
    from bleak import BleakClient, BleakScanner

    capture.emit(
        "environment",
        python=platform.python_version(),
        platform=platform.platform(),
        bleak=importlib.metadata.version("bleak"),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        mode="ppi_first_diagnostic" if args.ppi_first else "full_probe",
    )
    found = await BleakScanner.discover(timeout=12, return_adv=True)
    matches = [
        (d, a)
        for d, a in found.values()
        if (
            d.address.lower() == args.address.lower()
            if args.address
            else "polar sense" in (a.local_name or d.name or "").lower()
        )
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one target Polar, found {len(matches)}; no connection made")
    device, adv = matches[0]
    capture.emit(
        "target_advertisement",
        name=device.name,
        address=device.address,
        rssi=adv.rssi,
        local_name=adv.local_name,
        service_uuids=adv.service_uuids,
        manufacturer_data={str(k): v.hex() for k, v in adv.manufacturer_data.items()},
        service_data={k: v.hex() for k, v in adv.service_data.items()},
    )
    first_ppi = asyncio.Event()
    responses = asyncio.Queue()
    disconnected = asyncio.Event()

    def notification(kind, decoder):
        def receive(characteristic, payload):
            raw = bytes(payload)
            details = {"uuid": characteristic.uuid, "hex": raw.hex(), "length": len(raw)}
            try:
                details["decoded"] = decoder(raw)
            except (ValueError, struct.error) as error:
                details["decode_error"] = str(error)
            capture.emit(kind, **details)
            if kind == "control_ack":
                responses.put_nowait(details)
            if kind == "ppi" and details.get("decoded", {}).get("interval_count", 0):
                first_ppi.set()

        return receive

    def lost(_):
        capture.emit("disconnected")
        disconnected.set()

    async with BleakClient(
        device, disconnected_callback=lost, timeout=25, winrt={"use_cached_services": False}
    ) as client:
        capture.emit("connected", mtu=client.mtu_size)
        capture.emit(
            "gatt",
            services=[
                {
                    "uuid": s.uuid,
                    "characteristics": [
                        {"uuid": c.uuid, "properties": c.properties} for c in s.characteristics
                    ],
                }
                for s in client.services
            ],
        )
        for name, short in [
            ("manufacturer", "2a29"),
            ("model", "2a24"),
            ("firmware", "2a26"),
            ("software", "2a28"),
            ("hardware", "2a27"),
            ("battery", "2a19"),
        ]:
            uuid = f"0000{short}-0000-1000-8000-00805f9b34fb"
            if client.services.get_characteristic(uuid):
                raw = bytes(await client.read_gatt_char(uuid))
                capture.emit(
                    "read",
                    name=name,
                    uuid=uuid,
                    hex=raw.hex(),
                    text=raw.decode("utf-8", errors="replace")
                    if name != "battery"
                    else str(raw[0])
                    if raw
                    else "empty",
                )

        async def hold(seconds):
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                if disconnected.is_set():
                    raise RuntimeError("Target disconnected; partial capture preserved")
                await asyncio.sleep(min(0.2, max(0, until - time.monotonic())))

        async def command(opcode):
            raw = bytes([opcode, 3])
            capture.emit("control_write", uuid=CP, hex=raw.hex())
            await client.write_gatt_char(CP, raw, response=True)
            async with asyncio.timeout(10):
                while True:
                    reply = await responses.get()
                    decoded = reply.get("decoded")
                    if (
                        not decoded
                        or decoded["opcode"] != opcode
                        or decoded["measurement_type"] != 3
                    ):
                        capture.emit("unmatched_control_response", response=reply)
                        continue
                    if decoded["status"]:
                        raise RuntimeError(f"PPI command {opcode} rejected: {decoded}")
                    if not decoded["more"]:
                        return

        async def action(name):
            capture.emit(
                "action_requested",
                action=name,
                instruction="Remove the armband now"
                if name == "off"
                else "Put the armband back on now",
                marker_file=f"{name}.json",
            )
            deadline = time.monotonic() + 180
            marker = capture.path / f"{name}.json"
            while not marker.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"No user-confirmed {name} marker; not guessing wear state")
                await hold(0.2)
            confirmation = json.loads(marker.read_text(encoding="utf-8"))
            capture.emit(
                "action_confirmed",
                action=name,
                confirmation=confirmation,
                timing="receipt of user-reported action; not exact physical-action time",
            )

        hr_active = cp_active = data_active = start_attempted = False
        try:
            await client.start_notify(HR, notification("hr", decode_hr))
            hr_active = True
            hr_char = client.services.get_characteristic(HR)
            for descriptor in hr_char.descriptors:
                if descriptor.uuid.startswith("00002902"):
                    raw = bytes(await client.read_gatt_descriptor(descriptor.handle))
                    capture.emit("hr_cccd_readback", hex=raw.hex())
            if args.ppi_first:
                capture.emit("hr_only_skipped", reason="PPI-first transport diagnostic")
            else:
                capture.phase_start("hr_only_on_arm")
                await hold(60)
                capture.emit("hr_only_observation_complete", duration_s=60)
            capture.phase_start("ppi_setup_on_arm")
            feature = bytes(await client.read_gatt_char(CP))
            capture.emit("pmd_features", uuid=CP, hex=feature.hex())
            await client.start_notify(CP, notification("control_ack", decode_ack))
            cp_active = True
            await client.start_notify(DATA, notification("ppi", decode_ppi))
            data_active = True
            start_attempted = True
            await command(2)
            capture.phase_start("ppi_warmup_on_arm")
            async with asyncio.timeout(90):
                while not first_ppi.is_set():
                    await hold(0.2)
            capture.phase_start("ppi_on_arm_before")
            await hold(90)
            capture.phase_start("awaiting_off_confirmation")
            await action("off")
            capture.phase_start("ppi_off_arm")
            await hold(30)
            capture.phase_start("awaiting_on_confirmation")
            await action("on")
            capture.phase_start("ppi_on_arm_after")
            await hold(90)
            capture.emit("observation_complete", on_arm_ppi_observation_s=180, off_arm_min_s=30)
        finally:
            capture.phase_start("cleanup")
            if start_attempted and client.is_connected:
                try:
                    await command(3)
                except Exception as error:
                    capture.emit("cleanup_error", action="stop_ppi", detail=repr(error))
            for uuid, active in [(DATA, data_active), (CP, cp_active), (HR, hr_active)]:
                if active and client.is_connected:
                    try:
                        await client.stop_notify(uuid)
                    except Exception as error:
                        capture.emit(
                            "cleanup_error", action=f"unsubscribe {uuid}", detail=repr(error)
                        )


def self_test():
    assert decode_hr(bytes.fromhex("0048"))["rr_present"] is False
    assert decode_hr(bytes.fromhex("16482003"))["rr_ms"] == [781.25]
    assert decode_hr(bytes.fromhex("0948010a00"))["hr_bpm"] == 328
    raw = bytes.fromhex("03" + "00" * 9 + "4820030c0006" + "000000ffff07")
    decoded = decode_ppi(raw)
    assert decoded["interval_count"] == 2 and not decoded["device_timestamp_available"]
    assert decoded["samples"][0] == {
        "hr_bpm": 72,
        "ppi_ms": 800,
        "error_estimate_ms": 12,
        "flags_raw": 6,
        "blocker": False,
        "skin_contact": True,
        "skin_contact_supported_sdk": True,
        "reserved_flags": 0,
    }
    assert decoded["samples"][1]["blocker"]
    assert decoded["samples"][1]["error_estimate_ms"] == 65535
    assert decode_ack(bytes.fromhex("f0020300"))["status"] == 0
    assert decode_ack(bytes.fromhex("f0020301"))["status"] == 1
    for malformed in [b"", raw[:-1], b"\x03" + b"\x00" * 8 + b"\x80"]:
        try:
            decode_ppi(malformed)
        except ValueError:
            pass
        else:
            raise AssertionError("Malformed PPI accepted")
    print(
        "Decoder self-check passed (HR widths/RR, PPI fields/flags, malformed frames, short ACKs)."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address")
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--mark", choices=["off", "on"])
    parser.add_argument("--note", default="User confirmed physical action")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--ppi-first", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.mark:
        if not args.capture or not (args.capture / "raw.jsonl").exists():
            parser.error("--mark requires an existing capture directory")
        with (args.capture / f"{args.mark}.json").open("x", encoding="utf-8") as marker:
            json.dump({"action": args.mark, "utc": utc(), "note": args.note}, marker)
        print(f"Recorded user-reported {args.mark} action")
        return
    output = args.capture or ROOT / "private-data/physiology/captures" / (
        "verity-probe-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    capture = Capture(output)
    backend_log = logging.FileHandler(output / "backend-debug.txt", mode="x", encoding="utf-8")
    backend_log.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    backend_logger = logging.getLogger("bleak.backends.winrt.client")
    backend_logger.setLevel(logging.DEBUG)
    backend_logger.addHandler(backend_log)
    backend_logger.propagate = False
    print(f"CAPTURE_DIRECTORY={output.resolve()}", flush=True)
    failure = None
    try:
        asyncio.run(run(args, capture))
    except (Exception, KeyboardInterrupt) as error:
        failure = error
        capture.emit("error", detail=repr(error))
    finally:
        summary = summarize(capture.events)
        summary["completed"] = failure is None
        summary["mode"] = "ppi_first_diagnostic" if args.ppi_first else "full_probe"
        summary["hr_only_60s_completed"] = any(
            row["kind"] == "hr_only_observation_complete" for row in capture.events
        )
        with (output / "summary.json").open("x", encoding="utf-8") as target:
            json.dump(summary, target, indent=2)
        capture.stream.close()
        backend_logger.removeHandler(backend_log)
        backend_log.close()
        print("SUMMARY=" + json.dumps(summary), flush=True)
    if failure:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
