"""Shared test drivers: the synthetic armband through the scheduler and the HRV cleaner, offline."""

import math
import random
from dataclasses import dataclass

import pytest

from bridge.beat_scheduler import BeatScheduler, Interval
from bridge.hrm import parse_hrm
from bridge.hrv import HrvInterval, IntervalCleaner
from tools.synthetic_rr import Beat, Fault, Profile, apply_beat_faults, generate, true_beats

LATENCY_MS = 40


@dataclass
class Pipeline:
    profile: Profile
    seed: int
    intervals: list[Interval]  # everything the scheduler reported
    classified: list[HrvInterval]  # all but the last guard's length, still pending
    real: list[bool]  # per reported interval: a true heartbeat interval, or a false one
    scheduler: BeatScheduler

    def true_beats(self) -> list[Beat]:
        return list(true_beats(self.profile, random.Random(f"{self.seed}:beats")))


def run_pipeline(spec: str, *faults: str, seed: int = 1) -> Pipeline:
    profile = Profile.from_spec(spec)
    fault_list = [Fault.from_spec(f) for f in faults]
    scheduler, cleaner = BeatScheduler(), IntervalCleaner()
    intervals, classified = [], []
    for note in generate(profile, fault_list, seed):
        result = scheduler.on_packet(note.t_s * 1000 + LATENCY_MS, note.payload)
        intervals += result.intervals
        classified += cleaner.add(result.intervals)
    # Which beats reached the host: the sensor's beats, faults included, minus any in lost packets.
    beat_faults = [
        f for f in fault_list if f.kind in ("doubled_beat", "missed_beat", "artefact_burst")
    ]
    detected = list(
        apply_beat_faults(
            true_beats(profile, random.Random(f"{seed}:beats")),
            beat_faults,
            random.Random(f"{seed}:faults"),
        )
    )
    arrived = {note.t_s for note in generate(profile, fault_list, seed)}
    delivered, k = [], 0
    for note in generate(profile, beat_faults, seed):
        count = len(parse_hrm(note.payload).rr_raw)
        if note.t_s in arrived:
            delivered += detected[k : k + count]
        k += count
    truth = {_key(b) for b in true_beats(profile, random.Random(f"{seed}:beats"))}
    real = [_key(b) in truth for b in delivered]
    assert len(real) == len(intervals)
    return Pipeline(profile, seed, intervals, classified, real, scheduler)


def _key(beat: Beat) -> tuple[float, float]:
    return round(beat.t_s, 9), round(beat.rr_ms, 6)


def false_data(run: Pipeline) -> tuple[int, int]:
    """(false intervals classified clean, successive differences touching a false interval)."""
    pairs = list(zip(run.real, run.classified, strict=False))
    clean = sum(1 for ok, c in pairs if c.clean and not ok)
    diffs = sum(
        1
        for k, (ok, c) in enumerate(pairs)
        if c.diff_ms is not None and (not ok or not run.real[k - 1])
    )
    return clean, diffs


def true_rmssd(beats: list[Beat], start_s: float, end_s: float) -> float:
    diffs = [
        b.rr_ms - a.rr_ms
        for a, b in zip(beats, beats[1:], strict=False)
        if start_s < b.t_s <= end_s
    ]
    return math.sqrt(sum(d * d for d in diffs) / len(diffs))


def true_hr(beats: list[Beat], start_s: float, end_s: float) -> float:
    inside = [b for b in beats if start_s < b.t_s <= end_s]
    return 60_000 * len(inside) / sum(b.rr_ms for b in inside)


@pytest.fixture
def pipeline():
    return run_pipeline
