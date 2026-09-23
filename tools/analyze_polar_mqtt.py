"""Offline, stdlib-only analysis of probe_polar_mqtt captures; never feeds the bridge.

    python -m tools.analyze_polar_mqtt CAPTURE_DIR --stdout
    python -m tools.analyze_polar_mqtt CAPTURE_DIR --output NEW_REPORT.json
    python -m tools.analyze_polar_mqtt --self-test

Classify PPI by the JSON ppi array, NOT the topic (this app publishes PPI on /ecg).
Header timeStamp is inspected as observed; the apparent Unix-ms offset is NOT
one-way latency. Host/phone clocks are not synchronized. No per-beat timestamp is
invented. Source flags remain separate from physical on/off-arm confirmation.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

FIELDS = ("hr", "ppi", "errorEstimate", "blockerBit", "skinContactStatus")


def stats(values):
    values = sorted(values)
    if not values:
        return {"count": 0}
    return dict(
        count=len(values),
        min=values[0],
        median=statistics.median(values),
        p95=values[math.ceil(len(values) * 0.95) - 1],
        max=values[-1],
    )


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def classify(data):
    return "ppi" if isinstance(data.get("ppi"), list) else "hr" if "hr" in data else "other"


def consecutive(a, b):
    return a["session"] == b["session"] and b["index"] == a["index"] + 1


def gap(a, b):
    return (b["perf_counter_ns"] - a["perf_counter_ns"]) / 1e6


def samples(rows):
    return [
        sample for row in rows for sample in row["data"].get("ppi", []) if isinstance(sample, dict)
    ]


def conditional_age_floor(values):
    intervals = [sample["ppi"] for sample in values if number(sample.get("ppi"))]
    if len(intervals) == len(values) and intervals and all(value > 0 for value in intervals):
        return sum(intervals[1:])
    return None


def burst_analysis(rows, threshold):
    groups = []
    for row in rows:
        if (
            not groups
            or not consecutive(groups[-1][-1], row)
            or gap(groups[-1][-1], row) > threshold
        ):
            groups.append([])
        groups[-1].append(row)
    details = []
    for group in groups:
        values = samples(group)
        intervals = [sample["ppi"] for sample in values if number(sample.get("ppi"))]
        details.append(
            {
                "first_utc": group[0]["utc"],
                "messages": len(group),
                "samples": len(values),
                "delivery_span_ms": gap(group[0], group[-1]),
                "sum_intervals_ms": sum(intervals),
                "conditional_oldest_age_floor_ms": conditional_age_floor(values),
                "blocker_true": sum(sample.get("blockerBit") is True for sample in values),
                "contact_false": sum(sample.get("skinContactStatus") is False for sample in values),
            }
        )
    onset_gaps = [
        gap(a[0], b[0])
        for a, b in zip(groups, groups[1:], strict=False)
        if consecutive(a[-1], b[0])
    ]
    return {
        "group_if_successive_gap_at_most_ms": threshold,
        "bursts": len(groups),
        "onset_interarrival_ms": stats(onset_gaps),
        "messages_per_burst": stats([item["messages"] for item in details]),
        "samples_per_burst": stats([item["samples"] for item in details]),
        "sum_intervals_per_burst_ms": stats([item["sum_intervals_ms"] for item in details]),
        "conditional_oldest_age_floor_ms": stats(
            [
                item["conditional_oldest_age_floor_ms"]
                for item in details
                if item["conditional_oldest_age_floor_ms"] is not None
            ]
        ),
        "details": details,
    }


def stream_summary(rows):
    live = [row for row in rows if not row.get("retain", False)]
    pairs = [(a, b) for a, b in zip(live, live[1:], strict=False) if consecutive(a, b)]
    gaps = [gap(a, b) for a, b in pairs]
    timestamps = [row["data"]["timeStamp"] for row in live if number(row["data"].get("timeStamp"))]
    fields = sorted({key for row in rows for key in row["data"]})
    result = {
        "messages": len(rows),
        "nonretained_messages": len(live),
        "retained_excluded_from_timing": len(rows) - len(live),
        "dup_flag_count_not_deduplicated": sum(bool(row.get("dup")) for row in rows),
        "topics": dict(Counter(row["topic"] for row in rows)),
        "header_fields": fields,
        "first_utc": live[0]["utc"] if live else None,
        "last_utc": live[-1]["utc"] if live else None,
        "interarrival_ms": stats(gaps),
        "gaps_exceeding_ms": {
            str(limit): sum(value > limit for value in gaps) for limit in (1500, 2500, 5000)
        },
        "near_simultaneous_successive_messages_10ms": sum(value <= 10 for value in gaps),
        "timeStamp": {
            "values": stats(timestamps),
            "zero_count": timestamps.count(0),
            "types": dict(Counter(type(row["data"].get("timeStamp")).__name__ for row in live)),
            "integer_digit_counts": dict(
                Counter(len(str(abs(value))) for value in timestamps if type(value) is int)
            ),
        },
        "header_timestamp_deltas_raw": stats(
            [
                b["data"]["timeStamp"] - a["data"]["timeStamp"]
                for a, b in pairs
                if number(a["data"].get("timeStamp")) and number(b["data"].get("timeStamp"))
            ]
        ),
        "apparent_host_unix_ms_minus_header_if_unix_ms_NOT_latency": stats(
            [
                row["time_ns"] / 1e6 - row["data"]["timeStamp"]
                for row in live
                if number(row["data"].get("timeStamp"))
            ]
        ),
    }
    values = samples(rows)
    if any(row["stream"] == "ppi" for row in rows):
        result["ppi"] = {
            "samples": len(values),
            "samples_per_message": stats([len(row["data"]["ppi"]) for row in rows]),
            "sample_keys": dict(Counter(key for sample in values for key in sample)),
            "hr_value_counts": dict(Counter(str(sample.get("hr")) for sample in values)),
            "conditional_within_message_oldest_age_floor_ms": stats(
                [age for row in live if (age := conditional_age_floor(samples([row]))) is not None]
            ),
            "missing_fields": {key: sum(key not in sample for sample in values) for key in FIELDS},
            "ranges": {
                key: stats([sample[key] for sample in values if number(sample.get(key))])
                for key in ("ppi", "errorEstimate", "hr")
            },
            "flags": {
                key: dict(Counter(json.dumps(sample.get(key)) for sample in values))
                for key in ("blockerBit", "skinContactStatus")
            },
            "burst_sensitivity": {
                str(threshold): burst_analysis(live, threshold) for threshold in (100, 250, 500)
            },
        }
    else:
        result["hr_value_counts"] = dict(Counter(str(row["data"].get("hr")) for row in rows))
        result["hr_bpm"] = stats(
            [row["data"]["hr"] for row in rows if number(row["data"].get("hr"))]
        )
    return result


def start_measurement(first, request, confirmed, records):
    result = {
        "first_ppi_utc": first["utc"],
        "session": first["session"],
        "first_minus_request_s": gap(request, first) / 1000,
    }
    confirms = [item for item in confirmed if item["perf_counter_ns"] >= request["perf_counter_ns"]]
    if confirms:
        lower = max(0, gap(confirms[0], first) / 1000)
        result["tap_to_first_packet_bound_s"] = [lower, gap(request, first) / 1000]
        result["first_minus_confirmation_s"] = gap(confirms[0], first) / 1000
    connects = [
        row
        for row in records
        if row.get("kind") == "phone_tcp_connected"
        and request["perf_counter_ns"] <= row["perf_counter_ns"] <= first["perf_counter_ns"]
    ]
    if connects:
        result["first_minus_last_phone_tcp_connect_proxy_s"] = gap(connects[-1], first) / 1000
    return result


def strict_windows(rows, markers, records):
    """Request->confirmation is unknown transition, not guaranteed physical wear state."""
    by_name = defaultdict(list)
    for marker in markers:
        by_name[marker["name"]].append(marker)
    ppi = [row for row in rows if row["stream"] == "ppi" and not row.get("retain", False)]
    boundaries = {}
    if ppi and by_name["off_requested"]:
        boundaries["seated_before_removal"] = (ppi[0], by_name["off_requested"][0])
    if by_name["off"] and by_name["on_requested"]:
        boundaries["confirmed_off_arm"] = (by_name["off"][-1], by_name["on_requested"][-1])
    if by_name["off"] and by_name["on"]:
        last_on = by_name["on"][-1]
        if last_on["perf_counter_ns"] > by_name["off"][-1]["perf_counter_ns"]:
            end = by_name["stop"][-1] if by_name["stop"] else records[-1]
            boundaries["recovered_on_arm"] = (last_on, end)
    result = {}
    for name, (start, end) in boundaries.items():
        groups = defaultdict(list)
        for row in rows:
            if start["perf_counter_ns"] <= row["perf_counter_ns"] < end["perf_counter_ns"]:
                groups[row["stream"]].append(row)
        result[name] = {
            "start_utc": start.get("utc"),
            "end_utc": end.get("utc"),
            "duration_s": gap(start, end) / 1000,
            "streams": {stream: stream_summary(values) for stream, values in groups.items()},
        }
    return result


def analyze(records):
    markers = sorted(
        [row["marker"] for row in records if row.get("kind") == "marker"],
        key=lambda value: value["perf_counter_ns"],
    )
    rows, indices, undecoded = [], Counter(), Counter()
    for raw in records:
        if raw.get("kind") != "mqtt_message":
            continue
        try:
            data = json.loads(base64.b64decode(raw["payload_base64"]))
        except (ValueError, UnicodeDecodeError):
            undecoded[raw.get("topic", "missing")] += 1
            continue
        if not isinstance(data, dict):
            undecoded[raw.get("topic", "missing")] += 1
            continue
        row = {
            **raw,
            "data": data,
            "session": str(data.get("sessionId", "missing")),
            "stream": classify(data),
        }
        phase = "unconfirmed_wear_state"
        for marker in markers:
            if marker["perf_counter_ns"] > row["perf_counter_ns"]:
                break
            if marker["name"] in ("on", "off"):
                phase = f"user_confirmed_{marker['name']}_arm"
        row["phase"] = phase
        key = (row["session"], row["stream"])
        row["index"] = indices[key]
        if not row.get("retain", False):
            indices[key] += 1
        rows.append(row)
    sessions = defaultdict(lambda: defaultdict(list))
    phases = defaultdict(lambda: defaultdict(list))
    for row in rows:
        sessions[row["session"]][row["stream"]].append(row)
        phases[row["phase"]][row["stream"]].append(row)
    requested = [item for item in markers if item["name"] == "controlled_ppi_start_requested"]
    confirmed = [item for item in markers if item["name"] == "controlled_ppi_start_confirmed"]
    startup = {"device_ack_available": False}
    if requested:
        request = requested[-1]
        candidates = [
            row
            for row in rows
            if row["stream"] == "ppi"
            and row["data"]["ppi"]
            and not row.get("retain", False)
            and row["perf_counter_ns"] >= request["perf_counter_ns"]
        ]
        if candidates:
            startup.update(start_measurement(candidates[0], request, confirmed, records))
            predicates = {
                "first_unblocked_contact_sample": lambda sample: (
                    sample.get("blockerBit") is False and sample.get("skinContactStatus") is True
                ),
                "first_positive_hr_sample": lambda sample: (
                    number(sample.get("hr")) and sample["hr"] > 0
                ),
                "first_unblocked_contact_positive_hr_sample": lambda sample: (
                    sample.get("blockerBit") is False
                    and sample.get("skinContactStatus") is True
                    and number(sample.get("hr"))
                    and sample["hr"] > 0
                ),
            }
            startup["milestones"] = {}
            for name, predicate in predicates.items():
                matching = [row for row in candidates if any(predicate(s) for s in samples([row]))]
                startup["milestones"][name] = (
                    start_measurement(matching[0], request, confirmed, records)
                    if matching
                    else None
                )
    return {
        "sessions": {
            key: {stream: stream_summary(values) for stream, values in groups.items()}
            for key, groups in sessions.items()
        },
        "phases": {
            key: {stream: stream_summary(values) for stream, values in groups.items()}
            for key, groups in phases.items()
        },
        "markers": markers,
        "strict_windows": strict_windows(rows, markers, records),
        "unparsed_or_nonobject_payloads_by_topic": dict(undecoded),
        "controlled_startup": startup,
        "limitations": [
            "Retained data excluded from cadence/bursts/startup, included in field/sample counts.",
            "Duplicate flags counted; deliveries not deduplicated or treated as unique beats.",
            "Cadence excludes cross-session gaps and gaps across omitted phases.",
            "Phases follow user-confirmation marker timestamps, not sample acquisition timestamps.",
            "Phase labels include request->confirmation transitions; use strict_windows for wear.",
            "Header Unix-ms is a hypothesis; apparent offset is NOT one-way latency.",
            "Oldest-age floor assumes consecutive intervals, including blockers; not proven.",
            "Invalid/contact-false samples are counted, never accepted as reliable physical beats.",
            "Burst grouping is an arrival heuristic, not proof of a common device batch.",
            "No bridge/scheduler simulation, beat timestamps or absolute beat age inferred.",
        ],
    }


def self_test():
    def row(at, *, retained=False, blocked=False):
        data = {
            "sessionId": "synthetic",
            "timeStamp": 1893456000000 + at,  # invented 2030 fixture epoch, not a capture
            "ppi": [
                {
                    "ppi": 700,
                    "hr": 86,
                    "errorEstimate": 10,
                    "blockerBit": blocked,
                    "skinContactStatus": False,
                }
            ],
        }
        return {
            "kind": "mqtt_message",
            "perf_counter_ns": at * 1_000_000,
            "time_ns": (1893456000400 + at) * 1_000_000,
            "utc": str(at),
            "topic": "synthetic/ecg",
            "retain": retained,
            "dup": False,
            "payload_base64": base64.b64encode(json.dumps(data).encode()).decode(),
        }

    records = [row(50, retained=True), row(100, blocked=True), row(350), row(601)]
    report = analyze(records)
    ppi = report["sessions"]["synthetic"]["ppi"]
    assert ppi["retained_excluded_from_timing"] == 1
    assert ppi["interarrival_ms"]["min"] == 250
    assert ppi["ppi"]["burst_sensitivity"]["250"]["bursts"] == 2
    assert ppi["ppi"]["flags"]["blockerBit"]["true"] == 1
    assert ppi["ppi"]["flags"]["skinContactStatus"]["false"] == 4
    assert ppi["apparent_host_unix_ms_minus_header_if_unix_ms_NOT_latency"]["min"] == 400
    records += [
        {"kind": "marker", "marker": {"name": name, "perf_counter_ns": at * 1_000_000}}
        for name, at in (
            ("controlled_ppi_start_requested", 60),
            ("controlled_ppi_start_confirmed", 90),
            ("off", 200),
        )
    ]
    report = analyze(records)
    assert report["controlled_startup"]["tap_to_first_packet_bound_s"] == [0.01, 0.04]
    assert report["phases"]["user_confirmed_off_arm"]["ppi"]["messages"] == 2
    assert report["controlled_startup"]["milestones"]["first_unblocked_contact_sample"] is None
    assert conditional_age_floor([{"ppi": 700}, {"ppi": 800}, {"ppi": 900}]) == 1700
    assert conditional_age_floor([{"ppi": 700}, {"ppi": 0}]) is None
    records += [
        {"kind": "marker", "marker": {"name": name, "perf_counter_ns": at * 1_000_000}}
        for name, at in (
            ("on", 50),
            ("off_requested", 150),
            ("on_requested", 400),
            ("on", 500),
            ("stop", 700),
        )
    ]
    windows = analyze(records)["strict_windows"]
    assert windows["confirmed_off_arm"]["duration_s"] == 0.2
    assert windows["confirmed_off_arm"]["streams"]["ppi"]["messages"] == 1
    assert windows["recovered_on_arm"]["streams"]["ppi"]["messages"] == 1
    assert windows["seated_before_removal"]["streams"]["ppi"]["messages"] == 1
    print("SELF-TEST PASSED: synthetic schema, retention, flags, timestamp, burst/start boundaries")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, nargs="?")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--stdout", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if not args.capture:
        parser.error("capture directory is required")
    if not args.stdout and not (args.capture / "summary.json").exists():
        parser.error("Capture is still running; use --stdout for a provisional read-only snapshot")
    raw = (args.capture / "raw.jsonl").read_bytes()
    lines = raw.splitlines(keepends=True)
    complete = [line for line in lines if line.endswith(b"\n")]
    result = analyze([json.loads(line) for line in complete])
    result.update(
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        raw_bytes_read=len(raw),
        incomplete_last_line_ignored=len(lines) != len(complete),
        capture_ended=(args.capture / "summary.json").exists(),
    )
    text = json.dumps(result, indent=2)
    if args.stdout:
        print(text)
    else:
        output = args.output or args.capture / "analysis.json"
        with output.open("x", encoding="utf-8") as destination:
            destination.write(text + "\n")
        print(output)


if __name__ == "__main__":
    main()
