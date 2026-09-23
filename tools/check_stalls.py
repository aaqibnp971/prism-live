"""Reproduce bridge stalls through the production loop and committed native audio chain.

Run ``python -m tools.check_stalls`` for 2, 5 and 10 seconds in every experience segment.
The full session uses production durations, packet generation, Session, LiveLoop and native
rendering. Only time is accelerated: while the bridge is frozen the audio clock and renderer
advance, but no Python packet, session, engine-control or publication callback can run. Pending
synthetic notifications are delivered on resume with their actual (late) arrival time, exactly
as SyntheticPacketSource does on a real loop.

No audio device is opened. The fixed sample anchor cannot establish DAC accuracy, headphone
click freedom, operating-system scheduling or real BLE backlog behavior. Reports label these
limits, save the complete bridge log and save a float32 WAV around each stall for listening.
``--baseline-revision REV`` reads scheduler/live modules from git without checking anything out;
it is useful for comparing the original failure to the working tree using the same harness.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import math
import struct
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import ModuleType

import numpy as np

from bridge import beat_scheduler as scheduler_types
from bridge.beat_scheduler import BeatScheduler
from bridge.contract import validate
from bridge.engine import SAMPLE_RATE, EngineHost
from bridge.engine_feed import HeartbeatLevel, PsvFeed, SessionGain
from bridge.live import LiveLoop
from bridge.logging import SessionLog
from bridge.phase import PhaseTracker
from tools.synthetic_rr import Profile, SyntheticPacketSource

ROOT = Path(__file__).resolve().parent.parent
SEGMENTS = ("baseline", "load", "regulate", "resolve")
STALL_SECONDS = (2, 5, 10)
PROFILE = Profile.from_spec("68:66,68-100:75,100-72:115,72:144")
START_S = 10.0  # pulse phase 10 s: LOAD begins at scene time 66 s
STALL_OFFSET_MS = 20_000.0
MAX_SECONDS = 360


class AudioTimeline:
    """An offline stream that advances even while bridge callbacks are withheld."""

    def __init__(self, host: EngineHost) -> None:
        assert host.shim is not None
        self.shim = host.shim
        self.frame = 0
        self.samples = np.zeros(MAX_SECONDS * SAMPLE_RATE, dtype=np.float32)
        self.peak = 0.0
        self.shim.set_clock_anchor(0.0, 0)

    def seconds(self) -> float:
        return self.frame / SAMPLE_RATE

    def milliseconds(self) -> float:
        return self.frame * 1000.0 / SAMPLE_RATE

    def advance(self, seconds: float) -> None:
        target = max(self.frame, math.ceil(seconds * SAMPLE_RATE - 1e-7))
        if target > len(self.samples):
            raise RuntimeError("diagnostic exceeded its bounded audio timeline")
        # The shim itself splits calls into max_block_frames. This has exactly the same native
        # command/voice processing as the device callback, without acquiring an output device.
        if target > self.frame:
            out = self.samples[self.frame : target]
            self.shim.render_offline(out)
            self.peak = max(self.peak, float(np.max(np.abs(out))))
            self.frame = target


class AudioClockLoop(asyncio.SelectorEventLoop):
    """Use real asyncio scheduling; replace idle timer waiting with native audio rendering.

    There are deliberately no sockets in this diagnostic. Publication goes through LiveLoop's
    real validation/controller path to a recording sink, not through a fake network client.
    """

    def __init__(self, audio: AudioTimeline) -> None:
        self.audio = audio
        super().__init__()
        # Windows' normal monotonic clock resolution is about 15.6 ms; asyncio would otherwise
        # promote future 5 ms control timers while this sample-based clock has not advanced.
        self._clock_resolution = 1e-9

    def time(self) -> float:
        return self.audio.seconds()

    def _run_once(self) -> None:
        if not self._ready and self._scheduled:
            wake = max(self.time(), self._scheduled[0]._when)
            self.audio.advance(wake)
            if self.time() < wake:
                # A deadline infinitesimally beyond an exact frame (float rounding) must not
                # fall through to a real selector sleep. Advance one frame, never wall-wait.
                self.audio.advance((self.audio.frame + 1) / SAMPLE_RATE)
        super()._run_once()


def _revision_module(revision: str, filename: str) -> ModuleType:
    """Read-only historical Python load, scoped to this diagnostic process."""
    source = subprocess.check_output(
        ["git", "show", f"{revision}:{filename}"], cwd=ROOT, text=True, encoding="utf-8"
    )
    name = "_stall_baseline_" + Path(filename).stem
    spec = importlib.util.spec_from_loader(name, loader=None)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolves its defining module while decorating
    exec(compile(source, f"git:{revision}:{filename}", "exec"), module.__dict__)
    return module


def implementations(revision: str | None):
    if revision is None:
        return LiveLoop, BeatScheduler
    # Resolve to one immutable commit before reading either module. No checkout/reset/write.
    commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=ROOT, text=True
    ).strip()
    historical = _revision_module(commit, "bridge/beat_scheduler.py")
    # PSV intentionally requires the canonical immutable packet/interval types. Their shapes
    # did not change in this fix; historical code must construct those same public values.
    for name in ("BeatEvent", "Interval", "PacketResult", "Tuning"):
        old, current = getattr(historical, name), getattr(scheduler_types, name)
        if tuple(old.__dataclass_fields__) != tuple(current.__dataclass_fields__):
            raise ValueError(f"historical {name} schema differs from this checkout")
        setattr(historical, name, current)
    scheduler = historical.BeatScheduler
    live = _revision_module(commit, "bridge/live.py").LiveLoop
    return live, scheduler


def write_float_wav(path: Path, samples: np.ndarray) -> None:
    """Diagnostic artifact: mono IEEE float32 WAV, at the original 48 kHz/level."""
    payload = np.asarray(samples, dtype="<f4").tobytes()
    fmt = struct.pack("<HHIIHH", 3, 1, SAMPLE_RATE, SAMPLE_RATE * 4, 4, 32)
    fact = struct.pack("<I", len(samples))
    body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"fact" + struct.pack("<I", len(fact)) + fact
    body += b"data" + struct.pack("<I", len(payload)) + payload
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def _db(value: float) -> float | None:
    return round(20.0 * math.log10(value), 3) if value > 0.0 else None


def _rms(samples: np.ndarray) -> float | None:
    if not len(samples):
        return None
    return _db(float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))))


def run_case(
    directory: Path,
    segment: str,
    stall_seconds: int,
    *,
    loop_class=LiveLoop,
    scheduler_class=BeatScheduler,
    offset_ms: float = STALL_OFFSET_MS,
) -> dict:
    """One full session, one stall; errors are evidence in the report, never hidden successes."""
    if segment not in SEGMENTS or stall_seconds not in STALL_SECONDS:
        raise ValueError("expected an experience segment and a 2, 5 or 10 second stall")
    directory.mkdir(parents=True, exist_ok=True)
    host = EngineHost()
    host.open()
    assert host.engine is not None and host.shim is not None
    audio = AudioTimeline(host)
    loop = AudioClockLoop(audio)
    log = SessionLog(directory, clock=audio.milliseconds)
    phase = PhaseTracker.for_shim(host.shim)
    feed = PsvFeed(host.engine, log, "pose", phase=phase)
    gain = SessionGain(host.shim, log)
    heartbeat = HeartbeatLevel(host.shim, log)
    states, beats, leads = [], [], []

    def publish(msg: dict) -> bool:
        validate(msg, "out")
        log.message("out", msg)
        if msg["type"] == "state":
            states.append({"arrival_ms": audio.milliseconds(), **msg})
        elif msg["type"] == "beat":
            beats.append({"arrival_ms": audio.milliseconds(), **msg})
            if msg["quality"] != "rejected":
                leads.append(float(msg["t_play"]) - audio.milliseconds())
        return True

    evidence = {"segment": segment, "stall_seconds": stall_seconds}

    class StallingSource:
        async def __aiter__(self):
            async for payload in SyntheticPacketSource(PROFILE, seed=1, repeat=False):
                state = states[-1]
                if (
                    "stall_start_ms" not in evidence
                    and state["segment"] == segment
                    and state["segment_elapsed_ms"] >= offset_ms
                ):
                    begin = audio.milliseconds()
                    evidence.update(stall_start_ms=begin, stats_before=asdict(host.shim.stats()))
                    log.event("diagnostic_stall_begin", duration_ms=stall_seconds * 1000)
                    # Block the source immediately before delivering this packet. On resume the
                    # pending packet reaches the scheduler before the tick, the fault-triggering
                    # ordering. A tick-first resume after >grace can stop/restart the lattice and
                    # mask the original bug, so the ordering is explicit, not left to chance.
                    audio.advance(audio.seconds() + stall_seconds)
                    end = audio.milliseconds()
                    evidence.update(stall_end_ms=end, stats_after_stall=asdict(host.shim.stats()))
                    log.event("diagnostic_stall_end", duration_ms=end - begin)
                yield payload

    live = loop_class(
        StallingSource(),
        publish,
        log,
        clock=audio.milliseconds,
        scheduler=scheduler_class(),
        psv_feed=feed,
        session_gain=gain,
        heartbeat=heartbeat,
        beat_sink=host.shim,
        phase=phase,
    )

    async def scenario() -> None:
        task = asyncio.create_task(live.run())
        try:
            await asyncio.sleep(START_S - 0.010)
            audio.advance(START_S)
            # The start frame is exact; the countdown/spin itself is not under test here.
            alignment = phase.start_alignment()
            if alignment.wait_frames != 0:
                raise RuntimeError("diagnostic start did not land on its aligned frame")
            refusal = live._start_now(audio.milliseconds(), alignment)
            if refusal is not None:
                raise RuntimeError(f"diagnostic start refused: {refusal}")
            session = live.session.session
            evidence["session"] = session
            while not task.done() and live.session.session == session:
                await asyncio.sleep(0.020)
            if "stall_end_ms" not in evidence:
                if task.done():
                    await task
                raise RuntimeError(f"session ended before the {segment} injection")
            end = evidence["stall_end_ms"]
            if task.done():
                try:
                    await task
                except Exception as exc:
                    evidence["loop_error"] = f"{type(exc).__name__}: {exc}"
                    evidence["loop_error_at_ms"] = audio.milliseconds()
                    # Match the server's graceful fatal-loop cleanup: both controllers latch a
                    # three-second fade and the existing EngineHost lifecycle drains its tail.
                    await host.stop(gain, heartbeat)
            evidence.setdefault("loop_error", None)
            evidence["session_reached_new_idle"] = live.session.session != session
            evidence["segment_after_run"] = live.session.segment
            if audio.milliseconds() < end + 6_000:
                await asyncio.sleep((end + 6_000 - audio.milliseconds()) / 1000.0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    began = time.perf_counter()
    try:
        loop.run_until_complete(scenario())
        stats = asdict(host.shim.stats())
        records = [
            json.loads(line)
            for line in (directory / f"{evidence['session']}.jsonl").read_text().splitlines()
        ]
        endings = [record for record in records if record.get("event") == "session_end"]
        evidence["session_outcome"] = endings[-1]["outcome"] if endings else None
        evidence["session_completed"] = evidence["session_outcome"] == "completed"
        evidence["audio_beats_dropped"] = getattr(live, "audio_beats_dropped", 0)
        evidence["stats_final"] = stats
        evidence["scheduler_stats"] = asdict(live.scheduler.stats)
        evidence["elapsed_simulated_s"] = audio.seconds()
        evidence["elapsed_wall_s"] = round(time.perf_counter() - began, 3)
        evidence["sample_peak_dbfs"] = _db(audio.peak)
        evidence["minimum_publish_lead_ms"] = round(min(leads), 3) if leads else None
        begin, end = evidence["stall_start_ms"], evidence["stall_end_ms"]
        accepted = [b for b in beats if b["quality"] != "rejected"]
        previous = [b for b in accepted if b["arrival_ms"] < begin]
        resumed = [b for b in accepted if b["arrival_ms"] >= end]
        evidence["last_pre_stall_beat"] = previous[-1] if previous else None
        evidence["first_post_stall_beat"] = resumed[0] if resumed else None
        evidence["heartbeat_gap_ms"] = (
            resumed[0]["t_play"] - previous[-1]["t_play"] if resumed and previous else None
        )
        evidence["post_stall_beats_played"] = (
            stats["beats_played"] - evidence["stats_after_stall"]["beats_played"]
        )
        evidence["recovery_onset_delay_ms"] = (
            resumed[0]["t_play"] - end
            if resumed and evidence["post_stall_beats_played"] > 0
            else None
        )
        evidence["native_render_errors"] = stats["render_errors"]
        resolves = [state for state in states if state["segment"] == "resolve"]
        if resolves:
            last = resolves[-1]
            deadline = last["t_engine"] - last["segment_elapsed_ms"] + last["segment_nominal_ms"]
            evidence["resolve_end_ms"] = deadline
            # After resolve's engine fade the mix is heartbeat alone. A peak well after its
            # deadline catches accidental resurrection on reset, independent of played counters
            # (which also count heartbeat voices rendered under a digitally silent level).
            tail = audio.samples[round((deadline + 100) * 48) : audio.frame]
            evidence["post_resolve_deadline_peak_dbfs"] = (
                _db(float(np.max(np.abs(tail)))) if len(tail) else None
            )
        evidence["states"] = [
            {key: state[key] for key in ("arrival_ms", "segment", "segment_elapsed_ms")}
            for state in states
            if begin - 2_000 <= state["arrival_ms"] <= end + 6_000
        ]
        lo = max(0, round((begin - 2_000) * SAMPLE_RATE / 1000))
        hi = min(audio.frame, round((end + 6_000) * SAMPLE_RATE / 1000))
        clip = audio.samples[lo:hi]
        write_float_wav(directory / "stall.wav", clip)
        evidence["wav_start_ms"] = lo * 1000 / SAMPLE_RATE
        middle = audio.samples[round((begin + 1_000) * 48) : round(end * 48)]
        evidence["stall_after_first_second_rms_dbfs"] = _rms(middle)
        evidence["final_segment"] = states[-1]["segment"]
        (directory / "result.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        return evidence
    finally:
        loop.close()
        log.close()
        host.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--segment", choices=SEGMENTS, action="append")
    parser.add_argument("--seconds", type=int, choices=STALL_SECONDS, action="append")
    parser.add_argument("--baseline-revision", help="read only: original scheduler/live git commit")
    parser.add_argument("--offset-ms", type=float, default=STALL_OFFSET_MS)
    args = parser.parse_args(argv)
    if not 0 <= args.offset_ms <= 40_000:
        parser.error("--offset-ms must be between 0 and 40000")
    output = args.output or ROOT / "logs" / datetime.now().strftime("stalls-%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=True)
    live_class, scheduler_class = implementations(args.baseline_revision)
    report = {
        "mode": "accelerated asyncio, production session, continuous native offline audio",
        "anchor": "fixed output-frame map; no device/DAC measurement",
        "packet_resume": "buffered synthetic notifications delivered once, at resume arrival time",
        "resume_order": "pending packet before scheduler tick (injection in packet-source task)",
        "baseline_revision": args.baseline_revision,
        "limitations": [
            "No physical device, headset, BLE receiver or WebSocket transport is exercised.",
            "This is deterministic callback withholding, not measured Windows process scheduling.",
            "Recovery uses scheduled t_play and native counters, not an acoustic measurement.",
            "The saved WAV includes engine and heartbeat together; it is not isolated heartbeat.",
        ],
        "cases": [],
    }
    for segment in args.segment or SEGMENTS:
        for seconds in args.seconds or STALL_SECONDS:
            case = run_case(
                output / f"{segment}-{seconds}s",
                segment,
                seconds,
                loop_class=live_class,
                scheduler_class=scheduler_class,
                offset_ms=args.offset_ms,
            )
            report["cases"].append(case)
            (output / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(
                f"{segment:8} {seconds:2}s: error={case['loop_error']!r}, "
                f"recovery={case['recovery_onset_delay_ms']} ms, "
                f"played_after={case['post_stall_beats_played']}, "
                f"session_completed={case['session_completed']}",
                flush=True,
            )
    print(output.resolve())
    return int(any(case["loop_error"] is not None for case in report["cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
