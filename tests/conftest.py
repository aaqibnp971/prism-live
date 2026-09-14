"""Shared test drivers: the synthetic armband through the scheduler and the HRV cleaner, offline."""

import json
import math
import random
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from bridge import session as machine
from bridge.beat_scheduler import BeatScheduler, Interval
from bridge.contract import MIN_LEAD_MS, validate
from bridge.hrm import parse_hrm
from bridge.hrv import HrvInterval, IntervalCleaner
from bridge.logging import SessionLog
from bridge.psv import PsvEstimate, PsvModel
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


@dataclass
class Session:
    """A synthetic session through scheduler and PsvModel, read like the bridge reads it."""

    model: PsvModel
    estimates: dict[float, PsvEstimate]  # by read time, T_engine ms
    baseline_start_ms: float | None

    def at(self, t_s: float) -> PsvEstimate:
        """The estimate read at or just before t_s."""
        times = [t for t in self.estimates if t <= t_s * 1000]
        return self.estimates[max(times)]

    def series(self, from_s: float, until_s: float) -> list[PsvEstimate]:
        return [e for t, e in self.estimates.items() if from_s * 1000 <= t <= until_s * 1000]


def run_session(
    spec: str,
    *faults: str,
    seed: int = 1,
    baseline_at_s: float | None = 20.0,
    tick_ms: float = 2000.0,
    phase_ms: float = 0.0,
    extra_reads_s: tuple[float, ...] = (),
) -> Session:
    """Packets arrive LATENCY_MS after the device sends them. Estimates are read every tick_ms
    from phase_ms, through disconnects too, plus at extra_reads_s; a baseline starts at
    baseline_at_s, just before the read at that time."""
    profile = Profile.from_spec(spec)
    notes = list(generate(profile, [Fault.from_spec(f) for f in faults], seed))
    end_ms = profile.duration_s * 1000
    reads = {phase_ms + k * tick_ms for k in range(int((end_ms - phase_ms) // tick_ms) + 1)}
    reads |= {s * 1000 for s in extra_reads_s}
    start_ms = None if baseline_at_s is None else baseline_at_s * 1000
    scheduler, model = BeatScheduler(), PsvModel()
    estimates: dict[float, PsvEstimate] = {}
    started = start_ms is None
    k = 0
    for read in sorted(reads):
        while k < len(notes) and notes[k].t_s * 1000 + LATENCY_MS <= read:
            now = notes[k].t_s * 1000 + LATENCY_MS
            if not started and now >= start_ms:
                started = model.start_baseline(start_ms)
            model.on_packet(now, scheduler.on_packet(now, notes[k].payload))
            k += 1
        if not started and read >= start_ms:
            started = model.start_baseline(start_ms)
        estimates[read] = model.estimate(read)
    return Session(model, estimates, start_ms)


@pytest.fixture
def session():
    return run_session


TODAY = date(2026, 9, 13)


class PinnedLog(SessionLog):
    """Every session is dated TODAY, including those the state machine opens itself."""

    def start_session(self, today: date | None = None) -> str:
        return super().start_session(today or TODAY)


@dataclass
class Live:
    """A synthetic armband through scheduler, PsvModel and a real Session, as the bridge runs it."""

    session: machine.Session
    model: PsvModel
    sent: list[dict]  # every message published, in order
    log_dir: Path
    refusals: list[tuple[float, str | None]]  # (t_s, what start returned)

    def states(self) -> list[dict]:
        return [m for m in self.sent if m["type"] == "state"]

    def beats(self) -> list[dict]:
        return [m for m in self.sent if m["type"] == "beat"]

    def records(self) -> list[tuple[str, dict]]:
        """Every log line, as (file's session id, record), files in id order."""
        out = []
        for path in sorted(self.log_dir.glob("S-*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                out.append((path.stem, json.loads(line)))
        return out

    def events(self, name: str) -> list[dict]:
        return [r for _, r in self.records() if r.get("event") == name]

    def segments(self) -> list[tuple[str, float]]:
        """(segment, start on T_engine) for each segment entered, from the session log."""
        return [(e["segment"], e["at_ms"]) for e in self.events("segment")]


def run_live(
    spec: str,
    *faults: str,
    log_dir: Path,
    seed: int = 1,
    start_s: tuple[float, ...] = (10.0,),
    stop_s: tuple[float, ...] = (),
    until_s: float | None = None,
    tick_ms: float = 100.0,
    tick_jitter_ms: float = 0.0,
    skip_s: tuple[tuple[float, float], ...] = (),
    timings: machine.Timings | None = None,
) -> Live:
    """Packets reach the scheduler and the model at their arrival, in order. The session ticks every
    tick_ms, late by up to tick_jitter_ms, and not at all inside each (from, to) of skip_s. The
    attendant presses start at each of start_s and stop at each of stop_s."""
    profile = Profile.from_spec(spec)
    notes = list(generate(profile, [Fault.from_spec(f) for f in faults], seed))
    end_ms = (profile.duration_s if until_s is None else until_s) * 1000
    clock = [0.0]
    log = PinnedLog(log_dir, clock=lambda: clock[0])
    log.start_session()
    scheduler, model, sent, refusals = BeatScheduler(), PsvModel(), [], []

    def publish(msg: dict) -> None:
        validate(msg, "out")
        if msg["type"] == "beat" and msg["quality"] != "rejected":
            assert msg["t_play"] - clock[0] >= MIN_LEAD_MS - 1, msg
        sent.append(msg)
        log.message("out", msg, t_engine=clock[0])

    session = machine.Session(model, log, publish, now_ms=0.0, timings=timings)
    rng = random.Random(f"{seed}:ticks")
    presses = sorted([(t * 1000, "start") for t in start_s] + [(t * 1000, "stop") for t in stop_s])
    k, n = 0, 0
    while n * tick_ms <= end_ms:
        now = n * tick_ms + rng.uniform(0.0, tick_jitter_ms)
        n += 1
        if any(a * 1000 <= now < b * 1000 for a, b in skip_s):
            continue
        while presses and presses[0][0] <= now:
            at, press = presses.pop(0)
            while k < len(notes) and notes[k].t_s * 1000 + LATENCY_MS <= at:
                k = _deliver(notes, k, scheduler, model, session, publish, clock)
            clock[0] = max(clock[0], at)
            if press == "start":
                refusals.append((at / 1000, session.start(at)))
            else:
                session.stop(at)
        while k < len(notes) and notes[k].t_s * 1000 + LATENCY_MS <= now:
            k = _deliver(notes, k, scheduler, model, session, publish, clock)
        clock[0] = max(clock[0], now)
        for event in scheduler.tick(now):
            publish(session.beat_message(event))
        session.tick(now)
    log.close()
    return Live(session, model, sent, log_dir, refusals)


def _deliver(notes, k, scheduler, model, session, publish, clock) -> int:
    arrival = notes[k].t_s * 1000 + LATENCY_MS
    clock[0] = max(clock[0], arrival)
    result = scheduler.on_packet(arrival, notes[k].payload)
    model.on_packet(arrival, result)
    for event in result.events:
        publish(session.beat_message(event))
    return k + 1


def c_code(text, keep_strings=False):
    """C or C++ source with comments removed, and string and character literals emptied unless
    keep_strings."""
    code, i, n = [], 0, len(text)
    while i < n:
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            code.append(" ")
        elif text[i] in "\"'":
            quote, j = text[i], i + 1
            while j < n and text[j] != quote:
                j += 2 if text[j] == "\\" else 1
            code.append(text[i : j + 1] if keep_strings else quote * 2)
            i = j + 1
        else:
            code.append(text[i])
            i += 1
    return "".join(code)
