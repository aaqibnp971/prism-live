"""Offline timing sweep using the capture-matched synthetic PPI burst pattern.

No hardware latency claim: times are reconstructed T_engine and simulated receipt times.
Measures raw baseline-window availability, finalization against the 11 s cap, signal-age
margin, and every interval's guarded HRV classification delay. The ectopic threshold is unchanged.
"""

from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path
from statistics import median

from bridge.ppi_scheduler import PpiBeatScheduler
from bridge.psv import BaselinePhase, PsvModel
from tools.synthetic_rr import Profile, generate_ppi


def distribution(values):
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min_ms": min(ordered),
        "median_ms": median(ordered),
        "p95_ms": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))],
        "max_ms": max(ordered),
    }


def run_case(bpm: float, seed: int, start: float, duration_s: float):
    scheduler = PpiBeatScheduler()
    model = PsvModel(scheduler.t)
    closes, cap = start + 45_000, start + 56_000
    actions = [(start, "start", None), (cap, "cap", None)]
    actions += [
        (note.t_s * 1000, "packet", note.payload)
        for note in generate_ppi(Profile.from_spec(f"{bpm}:{duration_s}"), seed=seed)
    ]
    ages, classified_delays = [], []
    awaiting = deque()
    raw_gate_ready = None
    for now, kind, payload in sorted(actions, key=lambda action: action[0]):
        if kind == "start":
            assert model.start_baseline(now)
            awaiting.clear()  # pre-visitor pending HRV was deliberately discarded
        elif kind == "cap":
            model.close_baseline(now)
        else:
            last = model.last_trusted_beat_ms
            if last is not None and now >= start:
                ages.append(now - last)  # just before the next burst: maximum between arrivals
            result = scheduler.on_packet(now, payload)
            awaiting.extend(i.t_beat for i in result.intervals
                            if i.t_beat >= (start if now >= start else 0))
            model.on_packet(now, result)
            horizon = model._cleaner.horizon_ms
            if horizon is not None:
                while awaiting and awaiting[0] <= horizon:
                    classified_delays.append(now - awaiting.popleft())
            if raw_gate_ready is None and model.baseline_reported_ms is not None:
                evidence = model._capture.result(now)
                if evidence.passed and evidence.hr_base_bpm is not None:
                    raw_gate_ready = now
    baseline = model.baseline
    assert model.estimate(actions[-1][0]).phase is BaselinePhase.READY
    assert baseline is not None and raw_gate_ready is not None
    assert model.baseline_decided_ms <= cap
    return (
        {
            "bpm": bpm,
            "seed": seed,
            "baseline_start_ms": start,
            "raw_gate_hr_wait_ms": raw_gate_ready - closes,
            "snapshot_wait_ms": model.baseline_decided_ms - closes,
            "hrv_complete_at_snapshot": baseline.hrv_complete,
            "baseline_rmssd_differences": baseline.rmssd_base_differences,
            "maximum_accepted_beat_age_ms": max(ages),
            "old_6200_signal_margin_ms": 6200 - max(ages),
            "signal_margin_ms": model.signal_lost_ms - max(ages),
            "settlement_margin_ms": model.measurement_settle_ms - max(ages),
            "classification_delay": distribution(classified_delays),
        },
        ages,
        classified_delays,
    )


def measure(duration_s=180):
    cases, ages, delays = [], [], []
    for bpm in (45, 48, 60, 68, 95, 120, 180):
        for seed in (1, 2, 3):
            for start in (20_000, 21_000, 22_499, 24_900):
                case, beat_ages, classified_delays = run_case(bpm, seed, start, duration_s)
                cases.append(case)
                ages.extend(beat_ages)
                delays.extend(classified_delays)
    tuning = PpiBeatScheduler().t
    return {
        "kind": "offline capture-matched synthetic PPI; not hardware acquisition latency",
        "duration_per_case_s": duration_s,
        "cases": len(cases),
        "tuning": {
            "buffer_ms": tuning.buffer_ms,
            "anchor_lag_ms": tuning.anchor_lag_ms,
            "max_lag_ms": tuning.max_lag_ms,
            "signal_lost_ms": PsvModel(tuning).signal_lost_ms,
            "measurement_settle_ms": tuning.packet_settle_ms,
        },
        "raw_gate_hr_wait": distribution([c["raw_gate_hr_wait_ms"] for c in cases]),
        "snapshot_wait": distribution([c["snapshot_wait_ms"] for c in cases]),
        "hrv_complete_snapshots": sum(c["hrv_complete_at_snapshot"] for c in cases),
        "accepted_beat_age_before_burst": distribution(ages),
        "minimum_signal_margin_ms": min(c["signal_margin_ms"] for c in cases),
        "minimum_settlement_margin_ms": min(c["settlement_margin_ms"] for c in cases),
        "cases_falsely_exceeding_old_6200_ms": sum(
            c["old_6200_signal_margin_ms"] < 0 for c in cases
        ),
        "classification_delay": distribution(delays),
        "detail": cases,
    }


def measure_capture_baselines(capture: Path | None = None):
    """Diagnostic 45 s windows wholly inside the confirmed seated receipt stretch.

    These are alternative baseline placements in one recording, not five independent
    people or a claim of known acquisition-time wear. No threshold is fitted to them.
    """
    from tools.render_ppi_capture import DEFAULT_CAPTURE, replay_capture

    packets = []
    replay = replay_capture(
        DEFAULT_CAPTURE if capture is None else capture,
        on_result=lambda now, result: packets.append((now, result)),
    )
    seated = next(w for w in replay.windows if w.name == "seated_before_removal")
    cases = []
    for offset in (10_000, 20_000, 30_000, 40_000, 50_000):
        start = seated.start_ms + offset
        cap = start + 56_000
        assert cap < seated.end_ms
        model = PsvModel(PpiBeatScheduler().t)
        actions = [(start, "start", None), (cap, "cap", None)]
        actions += [(now, "packet", result) for now, result in packets if now <= cap]
        for now, kind, result in sorted(actions, key=lambda a: a[0]):
            if kind == "start":
                model.start_baseline(now)
            elif kind == "cap":
                model.close_baseline(now)
            else:
                model.on_packet(now, result)
        baseline = model.baseline
        cases.append({
            "start_after_first_ppi_s": offset / 1000,
            "phase_at_hold_cap": model.estimate(cap).phase.value,
            "snapshot_wait_ms": (None if model.baseline_decided_ms is None else
                                 model.baseline_decided_ms - (start + 45_000)),
            "accepted_ms": baseline.accepted_ms if baseline else None,
            "hr_base_bpm": baseline.hr_base_bpm if baseline and baseline.passed else None,
            "hrv_complete": baseline.hrv_complete if baseline else False,
            "problems": list(baseline.problems) if baseline else [],
        })
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("logs/ppi-timing-measurements.json"))
    parser.add_argument("--capture", type=Path,
                        help="Optional private capture; the default sweep is synthetic only")
    args = parser.parse_args()
    report = measure()
    if args.capture is not None:
        report["real_capture_seated_baseline_placements"] = measure_capture_baselines(args.capture)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "detail"}, indent=2))
    print(f"Saved {args.output.resolve()}")


if __name__ == "__main__":
    main()
