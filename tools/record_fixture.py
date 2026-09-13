"""Record the canonical synthetic session: tools/fixtures/synthetic-clean.jsonl.

    python -m tools.record_fixture

Runs the synthetic armband (tools/synthetic_rr.py, no faults) through the beat scheduler
offline, on a simulated T_engine with 20 ms ticks and 40 ms of link latency, and writes what
the laptop would send through bridge/logging.py, exactly as a live session is logged.

The beats are real output of bridge/beat_scheduler.py. The state messages are not real yet:
the state machine (prompt 2.4) does not exist, so the stand-in below makes plausible PSV and
confidence values that obey the contract. Authority is the real rule, bridge/authority.py, applied
to those stand-in confidences. Build clients against the stream's shape and timing, and tune
nothing to its PSV numbers. Re-record once 2.4 lands.

Regulate deliberately runs 15 s past its nominal 75 s, so every client built against this
meets segment_elapsed_ms > segment_nominal_ms.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from bridge.authority import authority as grant
from bridge.beat_scheduler import BeatScheduler, Interval
from bridge.contract import MIN_LEAD_MS, STATE_INTERVAL_MS, validate
from bridge.hrm import parse_hrm
from bridge.logging import SessionLog
from tools.synthetic_rr import Profile, generate

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic-clean.jsonl"
TICK_MS = 20
LATENCY_MS = 40
SEED = 1

# (segment, how long it runs, nominal) in seconds. Idle is before the attendant presses start.
TIMELINE = (
    ("idle", 10, 0),
    ("baseline", 45, 45),
    ("load", 75, 75),
    ("regulate", 90, 75),
    ("resolve", 45, 45),
)
PROFILE = "68:10,68:45,68-105:75,105-75:75,75:15,75:45"


@dataclass(frozen=True)
class Span:
    segment: str
    start_ms: int
    end_ms: int
    nominal_ms: int


def spans() -> list[Span]:
    out, t = [], 0
    for segment, seconds, nominal in TIMELINE:
        out.append(Span(segment, t, t + seconds * 1000, nominal * 1000))
        t += seconds * 1000
    out.append(Span("reset", t, t, 0))
    return out


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


class StandIn:
    """Plausible PSV and confidence values until the state machine feeds real ones."""

    def __init__(self) -> None:
        self.intervals: list[Interval] = []
        self.contact = True
        self.hr_base: float | None = None
        self.baseline_quality = 0.0
        self.last_authority: dict[str, float] | None = None
        self.resolve_entry: dict[str, float] | None = None

    def state(self, now: int, span: Span, start_ms: int, seq: int, session: str) -> dict:
        accepted = [i for i in self.intervals if i.accepted]
        recent = accepted[-4:]
        hr = 60_000 * len(recent) / sum(i.rr_ms for i in recent) if recent else None
        window = [i for i in self.intervals if i.arrival > now - 30_000]
        clean = sum(i.accepted for i in window) / len(window) if window else 0.0
        elapsed = now - span.start_ms
        segment = span.segment

        if segment == "baseline":
            got = [i for i in accepted if i.arrival >= span.start_ms]
            self.baseline_quality = min(1.0, len(got) / 30) * clean
        if segment == "load" and self.hr_base is None:
            tail = [i for i in accepted if span.start_ms - 30_000 <= i.t_beat < span.start_ms]
            self.hr_base = 60_000 * len(tail) / sum(i.rr_ms for i in tail)

        base = 0.0 if segment == "idle" else round(self.baseline_quality * clean, 3)
        confidence = {
            "arousal": base,
            "valence": 0.0,
            "cognitive_load": round(0.6 * base, 3),
            "readiness": round(0.8 * base, 3),
        }
        if hr is None or self.hr_base is None:
            arousal = 0.5
        else:
            arousal = clamp01(0.5 + (hr - self.hr_base) / 50)
        progress = min(1.0, elapsed / span.nominal_ms) if span.nominal_ms else 0.0
        load = {"load": 0.5 + 0.25 * progress, "regulate": 0.75 - 0.4 * progress,
                "resolve": 0.35 + 0.15 * progress}.get(segment, 0.5)  # fmt: skip
        psv = {
            "arousal": round(arousal, 3),
            "valence": 0.5,
            "cognitive_load": round(load, 3),
            "readiness": round(clamp01(0.5 - 0.6 * (arousal - 0.5)), 3),
        }

        if segment == "resolve" and self.resolve_entry is None:
            self.resolve_entry = self.last_authority
        authority = grant(
            confidence,
            segment,
            segment_elapsed_ms=elapsed,
            segment_nominal_ms=span.nominal_ms,
            resolve_entry=self.resolve_entry,
        )
        self.last_authority = authority

        return {
            "type": "state",
            "v": 1,
            "session": session,
            "seq": seq,
            "t_engine": now,
            "t_session": None if segment == "idle" else now - start_ms,
            "segment": segment,
            "segment_elapsed_ms": 0 if segment == "reset" else elapsed,
            "segment_nominal_ms": span.nominal_ms,
            "psv": psv,
            "confidence": confidence,
            "authority": authority,
            "hr_bpm": None if hr is None else round(hr, 1),
            "hr_base": None if self.hr_base is None else round(self.hr_base, 1),
            "signal": {
                "contact": self.contact,
                "rr_accepted_pct": round(clean, 3),
                "baseline_quality": round(self.baseline_quality, 3),
            },
        }


def record(path: Path = FIXTURE, today: date | None = None) -> dict:
    timeline = spans()
    start_ms = timeline[1].start_ms  # the attendant presses start as baseline begins
    boundaries = {s.start_ms for s in timeline[1:]}
    notes = list(generate(Profile.from_spec(PROFILE), (), SEED))
    now = 0
    counts = {"beat": 0, "state": 0}

    with tempfile.TemporaryDirectory() as tmp:
        log = SessionLog(tmp, clock=lambda: now)
        session = log.start_session(today)
        scheduler = BeatScheduler()
        stand_in = StandIn()

        def send(msg: dict) -> None:
            validate(msg, "out")
            if msg["type"] == "beat" and msg["quality"] != "rejected":
                assert msg["t_play"] - now >= MIN_LEAD_MS, msg
            log.message("out", msg)
            counts[msg["type"]] += 1

        i = 0
        while now <= timeline[-1].end_ms:
            while i < len(notes) and notes[i].t_s * 1000 + LATENCY_MS <= now:
                arrival = notes[i].t_s * 1000 + LATENCY_MS
                result = scheduler.on_packet(arrival, notes[i].payload)
                stand_in.intervals += result.intervals
                stand_in.contact = parse_hrm(notes[i].payload).contact_detected is not False
                for event in result.events:
                    send(event.message(session))
                i += 1
            for event in scheduler.tick(now):
                send(event.message(session))
            if now % STATE_INTERVAL_MS == 0 or now in boundaries:
                span = next(s for s in reversed(timeline) if s.start_ms <= now)
                send(stand_in.state(now, span, start_ms, counts["state"] + 1, session))
            now += TICK_MS
        log.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(log.path, path)
    return {"session": session, "path": path, "duration_s": timeline[-1].end_ms / 1000, **counts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record the canonical synthetic session.")
    parser.add_argument("--out", type=Path, default=FIXTURE)
    args = parser.parse_args(argv)
    result = record(args.out)
    print(
        f"{result['path']}: session {result['session']}, {result['duration_s']:.0f} s, "
        f"{result['beat']} beats, {result['state']} states"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
