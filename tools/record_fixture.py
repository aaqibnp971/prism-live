"""Record the canonical synthetic session: tools/fixtures/synthetic-clean.jsonl.

    python -m tools.record_fixture

Runs the synthetic armband (tools/synthetic_rr.py, no faults) through the bridge offline, on a
simulated T_engine with 20 ms ticks and 40 ms of link latency: the beat scheduler, the PSV model
and the session state machine (bridge/session.py), whose state messages carry authority from
bridge/authority.py. The file is the first session's log exactly as bridge/logging.py writes it
live, the session's own events included, from idle until reset ends.

Every value is real output of the bridge, but the heart is synthetic: a profile of rates with
white-noise variability, no breathing, no real person. Build clients against the stream's shape
and timing, and tune nothing to its numbers.

The profile brings the heart rate down slowly enough that regulate runs past its nominal 75 s, so
every client built against this meets segment_elapsed_ms > segment_nominal_ms. Baseline also runs
past its 45 s while the result comes in.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

from bridge.beat_scheduler import BeatScheduler
from bridge.contract import MIN_LEAD_MS, validate
from bridge.logging import SessionLog
from bridge.psv import PsvModel
from bridge.session import Session
from tools.synthetic_rr import Profile, generate

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic-clean.jsonl"
TICK_MS = 20
LATENCY_MS = 40
SEED = 1
START_MS = 10_000  # the attendant presses start after 10 s of idle
# At rest through idle, baseline and its hold, climbing through load, then a fall slow enough that
# regulate meets its threshold 90 s in, 15 s past nominal.
PROFILE = "68:61,68-100:75,100-72:115,72:150"
LIMIT_MS = 400_000


def record(path: Path = FIXTURE, today: date | None = None) -> dict:
    notes = list(generate(Profile.from_spec(PROFILE), (), SEED))
    now = 0
    counts = {"beat": 0, "state": 0}

    with tempfile.TemporaryDirectory() as tmp:
        log = SessionLog(tmp, clock=lambda: now)
        session_id = log.start_session(today)
        first = log.path
        scheduler, model = BeatScheduler(), PsvModel()

        def send(msg: dict) -> None:
            validate(msg, "out")
            if msg["type"] == "beat" and msg["quality"] != "rejected":
                assert msg["t_play"] - now >= MIN_LEAD_MS, msg
            log.message("out", msg)
            if msg["session"] == session_id:
                counts[msg["type"]] += 1

        session = Session(model, log, send, now_ms=now)
        i = 0
        while session.session == session_id:
            if now > LIMIT_MS:
                raise RuntimeError(f"the session was still in {session.segment} at {now} ms")
            while i < len(notes) and notes[i].t_s * 1000 + LATENCY_MS <= now:
                arrival = notes[i].t_s * 1000 + LATENCY_MS
                result = scheduler.on_packet(arrival, notes[i].payload)
                model.on_packet(arrival, result)
                for event in result.events:
                    send(session.beat_message(event))
                i += 1
            if now == START_MS:
                refused = session.start(now)
                assert refused is None, refused
            for event in scheduler.tick(now):
                send(session.beat_message(event))
            session.tick(now)
            now += TICK_MS
        log.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(first, path)
    return {"session": session_id, "path": path, "duration_s": now / 1000, **counts}


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
