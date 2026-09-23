"""Generated MQTT transport fixtures. No captured physiological data is used.

Only the public/aggregate transport shape is modelled: roughly five-second
bursts, at most five samples per fragment, mixed blocker/error quality, and
unreliable contact/HR diagnostics. The interval series, UTC epoch, marker times
and quality pattern below are invented independently, not resampled from a
person's recording. Generated files exist only in pytest temporary directories.
"""

from __future__ import annotations

import base64
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bridge.mqtt_source import MqttConfig

EPOCH = datetime(2020, 1, 1, tzinfo=UTC)
ORIGIN_NS = 10_000_000_000
DURATION_MS = 260_000
BURST_GAPS_MS = (5000, 5100, 4930, 5200, 4860, 5050)
MARKERS_MS = {
    "off_requested": 125_000,
    "off": 135_000,
    "on_requested": 180_000,
    "on": 195_000,
    "stop": DURATION_MS,
}


def _stamp(at_ms: float) -> dict:
    return {
        "perf_counter_ns": ORIGIN_NS + round(at_ms * 1e6),
        "utc": (EPOCH + timedelta(milliseconds=at_ms)).isoformat(),
    }


def synthetic_records() -> list[dict]:
    """Return a deterministic, wholly artificial probe-format message stream."""
    config = MqttConfig()
    records = []

    def publish(at_ms: float, suffix: str, fields: dict) -> None:
        message = {
            "clientId": config.client_id,
            "deviceId": config.device_id,
            "sessionId": "synthetic-fixture",
            "timeStamp": round(EPOCH.timestamp() * 1000 + at_ms),
            **fields,
        }
        encoded = json.dumps(message, separators=(",", ":")).encode()
        records.append({
            "kind": "mqtt_message",
            **_stamp(at_ms),
            "topic": config.topic_prefix + "/" + suffix,
            "retain": False,
            "payload_utf8": encoded.decode(),
            "payload_base64": base64.b64encode(encoded).decode(),
            "json": message,
        })

    for at_ms in range(0, DURATION_MS, 1000):
        # This diagnostic can stay positive during the invented off-arm period.
        publish(at_ms, "hr", {"hr": 75})

    beat_ms, index, burst_ms, burst_index = 0.0, 0, 5000.0, 0
    while burst_ms < DURATION_MS:
        samples = []
        while True:
            rr = round(800 + 22 * math.sin(index * 0.73) + 9 * math.cos(index * 0.21))
            if beat_ms + rr > burst_ms:
                break
            beat_ms += rr
            # Deliberately poor signal: about 40% quality failures, enough that
            # a baseline must fail rather than be made to pass by the tooling.
            quality_slot = index % 10
            samples.append({
                "ppi": rr,
                "errorEstimate": 120 if quality_slot == 3 else 4,
                "blockerBit": quality_slot < 3,
                "skinContactStatus": index % 4 != 0,
                "hr": 0 if index % 4 == 0 else 75,
            })
            index += 1
        for fragment, first in enumerate(range(0, len(samples), 5)):
            publish(burst_ms + fragment * 35, "ecg", {"ppi": samples[first:first + 5]})
        burst_ms += BURST_GAPS_MS[burst_index % len(BURST_GAPS_MS)]
        burst_index += 1

    for name, at_ms in MARKERS_MS.items():
        records.append({"kind": "marker", "marker": {"name": name, **_stamp(at_ms)}})
    return sorted(
        records,
        key=lambda record: record.get("perf_counter_ns", record.get("marker", {}).get(
            "perf_counter_ns", 0,
        )),
    )


def write_synthetic_capture(directory: Path) -> Path:
    """Materialize the test-only probe format, including strict-window metadata."""
    directory.mkdir(parents=True, exist_ok=True)
    records = synthetic_records()
    windows = {
        "seated_before_removal": (5000, MARKERS_MS["off_requested"]),
        "confirmed_off_arm": (MARKERS_MS["off"], MARKERS_MS["on_requested"]),
        "recovered_on_arm": (MARKERS_MS["on"], MARKERS_MS["stop"]),
    }
    analysis = {
        "synthetic": True,
        "provenance": "Algorithmically invented test data; no participant measurements.",
        "strict_windows": {
            name: {"start_utc": _stamp(start)["utc"], "end_utc": _stamp(end)["utc"]}
            for name, (start, end) in windows.items()
        },
    }
    (directory / "raw.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8",
    )
    (directory / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
    return directory
