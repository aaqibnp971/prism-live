"""Offline comparison only: load an inspected wheel without installing it or using BLE.

Usage: python -m tools.compare_polar_probe WHEEL CAPTURE_DIRECTORY
Actual capture comparisons and synthetic edge cases are reported separately.
"""

import argparse
import dataclasses
import hashlib
import json
import sys
from pathlib import Path

from tools.probe_verity import decode_hr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.wheel.resolve()))
    # Only parser entry points are called; PolarDevice is never constructed.
    from polar_python.models.measurement_settings import MeasurementSettings
    from polar_python.models.pmd_data_frame import PmdDataFrame
    from polar_python.models.ppi_data import PPIData
    from polar_python.parsers.hr import parse_hr_data

    def parse_ppi(raw):
        return PPIData.from_dataframe(PmdDataFrame.from_bytes(bytearray(raw), lambda _: 1))

    rows = [json.loads(line) for line in (args.capture / "raw.jsonl").read_text().splitlines()]
    result = {
        "wheel": args.wheel.name,
        "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
        "method": "Offline parser replay; library not installed or used for live connection",
        "actual_capture": {
            "hr_packets": 0,
            "ppi_packets": 0,
            "ppi_samples": 0,
            "mismatches": [],
            "raw_zero_timestamp_outputs": [],
        },
        "synthetic_edge_cases": {},
    }
    actual = result["actual_capture"]
    for row in rows:
        if row["kind"] not in ("hr", "ppi") or "decoded" not in row:
            continue
        raw = bytes.fromhex(row["hex"])
        ours = row["decoded"]
        if row["kind"] == "hr":
            theirs = parse_hr_data(bytearray(raw))
            actual["hr_packets"] += 1
            if theirs.heartrate != ours["hr_bpm"] or theirs.rr_intervals != ours["rr_ms"]:
                actual["mismatches"].append({"hex": row["hex"], "library": repr(theirs)})
        else:
            theirs = parse_ppi(raw)
            actual["ppi_packets"] += 1
            actual["ppi_samples"] += len(theirs.samples)
            expected = [
                (
                    s["ppi_ms"],
                    s["error_estimate_ms"],
                    s["hr_bpm"],
                    s["blocker"],
                    s["skin_contact"],
                    s["skin_contact_supported_sdk"],
                )
                for s in ours["samples"]
            ]
            decoded = [
                (
                    s.ppi,
                    s.error_estimate,
                    s.hr,
                    s.invalid_ppi,
                    s.skin_contact_status,
                    s.skin_contact_supported,
                )
                for s in theirs.samples
            ]
            if expected != decoded:
                actual["mismatches"].append({"hex": row["hex"], "library": repr(theirs)})
            if ours["device_timestamp_ns_raw"] == 0:
                actual["raw_zero_timestamp_outputs"].append(theirs.timestamp)
    synthetic = result["synthetic_edge_cases"]
    raw_hr = bytearray.fromhex("0948010a00")
    synthetic["hr_16bit_plus_energy_no_rr"] = {
        "hex": raw_hr.hex(),
        "direct": decode_hr(raw_hr),
        "library": dataclasses.asdict(parse_hr_data(raw_hr)),
    }
    sample = bytes.fromhex("03" + "00" * 9 + "4820030c0006")
    synthetic["zero_timestamp"] = dataclasses.asdict(parse_ppi(sample))
    synthetic["trailing_byte_silently_ignored"] = len(parse_ppi(sample + b"\xff").samples)
    try:
        MeasurementSettings.from_bytes(bytearray.fromhex("f0020301"))
    except Exception as error:
        synthetic["four_byte_error_response"] = repr(error)
    output = args.capture / "library-decoder-comparison.json"
    with output.open("x", encoding="utf-8") as target:
        json.dump(result, target, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
