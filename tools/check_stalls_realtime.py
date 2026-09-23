"""Block the real bridge loop while the native audio chain keeps rendering, without a device.

Run ``python -m tools.check_stalls_realtime`` for three parallel, production-length sessions.
Each injects an actual 2, 5 or 10 second ``time.sleep`` in all four running segments. Baseline's
injection is in the hold after the 45 second capture window; delayed interval classification
may still be pending, so a long stall can degrade the baseline. ``--baseline-at 20`` instead
exercises capture. Baseline results are recorded, never overridden to force later segments.
The native engine render address is pulled only by the shim, on a dedicated paced render thread.
This is not a DAC, a listening test, a real BLE source, or a supervisor recovery test. No device,
browser, network listener or supervisor is started. The latter would otherwise restart a stale
bridge and mask its in-process recovery. Native onset counters include voices at silent levels;
they are labelled voice starts, never measured audibility.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import subprocess
import sys
import threading
import time
import wave
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np

from bridge.clock import t_engine_ms
from bridge.engine import EngineHost, Shim
from bridge.engine_feed import HeartbeatLevel, PsvFeed, SessionGain
from bridge.live import LiveLoop
from bridge.logging import SessionLog
from bridge.phase import PhaseTracker
from bridge.server import LiveServer
from tools.synthetic_rr import Profile, SyntheticPacketSource

RATE = 48_000
BLOCK = 480
SEGMENTS = ("baseline", "load", "regulate", "resolve")
MAX_SECONDS = 360


class PacedRender:
    """A test driver outside the callback; actual DSP remains entirely native."""

    def __init__(self, shim: Shim, anchor_ms: float) -> None:
        self.shim = shim
        self.anchor_ms = anchor_ms
        self.samples = np.zeros(RATE * MAX_SECONDS, dtype=np.float32)
        self.blocks: list[dict] = []
        self.voice_starts: list[float] = []
        self.error: str | None = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="paced-native-render", daemon=True)

    def _run(self) -> None:
        frame = 0
        played = 0
        try:
            while not self.stop_event.is_set() and frame + BLOCK <= len(self.samples):
                scheduled = self.anchor_ms + frame * 1000 / RATE
                delay = (scheduled - t_engine_ms()) / 1000
                if delay > 0:
                    self.stop_event.wait(delay)
                if self.stop_event.is_set():
                    break
                actual = t_engine_ms()
                output = self.samples[frame : frame + BLOCK]
                self.shim.render_offline(output)
                stats = self.shim.stats()
                # The counter increments at the pre-limiter voice start. Add chain latency;
                # one-block resolution, not device-onset telemetry or a claim about the DAC.
                for _ in range(stats.beats_played - played):
                    self.voice_starts.append(
                        self.anchor_ms + (frame + Shim.latency_frames()) * 1000 / RATE
                    )
                played = stats.beats_played
                self.blocks.append(
                    {
                        "frame": frame,
                        "scheduled_ms": scheduled,
                        "render_lateness_ms": actual - scheduled,
                        "peak": float(np.max(np.abs(output))),
                        "rms": float(np.sqrt(np.mean(output.astype(np.float64) ** 2))),
                    }
                )
                frame += BLOCK
        except BaseException as error:
            self.error = repr(error)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(5)
        if self.thread.is_alive():
            raise RuntimeError("native render thread did not stop; do not destroy its engine")


class ObservedSink:
    def __init__(self, shim: Shim) -> None:
        self.shim = shim
        self.pushes: list[dict] = []

    def push_beat(self, t_play_ms: float, rr_ms: float, quality: int = 0) -> None:
        record = {
            "arrival_ms": t_engine_ms(),
            "t_play_ms": t_play_ms,
            "rr_ms": rr_ms,
            "quality": quality,
            "error": None,
        }
        self.pushes.append(record)
        try:
            self.shim.push_beat(t_play_ms, rr_ms, quality)
        except Exception as error:
            record["error"] = repr(error)
            raise


async def exercise(args: argparse.Namespace, directory: Path) -> dict:
    host = EngineHost()
    host.open()
    assert host.shim is not None and host.engine is not None
    shim = host.shim
    # Lead time lets the render thread start without changing the fixed sample/time map.
    anchor = t_engine_ms() + 100
    shim.set_clock_anchor(anchor, 0)
    render = PacedRender(shim, anchor)
    log = SessionLog(directory)
    server = LiveServer(log)  # publish/lead validation only; never start a listener
    sink = ObservedSink(shim)
    phase = PhaseTracker.for_shim(shim)
    transitions: list[dict] = []
    last_segment: str | None = None

    def publish(message: dict) -> bool:
        nonlocal last_segment
        if message["type"] == "state" and message["segment"] != last_segment:
            last_segment = message["segment"]
            transitions.append(
                {"segment": last_segment, "arrival_ms": t_engine_ms(), "state": message}
            )
        return server.publish(message)

    loop = LiveLoop(
        SyntheticPacketSource(Profile.from_spec("68:66,68-105:75,105-75:75,75:160")),
        publish,
        log,
        psv_feed=PsvFeed(host.engine, log, phase=phase),
        session_gain=SessionGain(shim, log),
        heartbeat=HeartbeatLevel(shim, log),
        beat_sink=sink,
        phase=phase,
    )
    stalls: list[dict] = []
    fault: str | None = None
    completed: dict | None = None
    runner: asyncio.Task | None = None
    render.start()
    try:
        runner = asyncio.create_task(loop.run())
        await asyncio.sleep(0)
        await asyncio.wait_for(loop.wait_for_signal(), 15)
        refusal = await loop.attendant_start()
        if refusal is not None:
            raise RuntimeError(f"start refused: {refusal}")
        session = loop.session.session
        injected: set[str] = set()
        deadline = t_engine_ms() + 325_000
        while t_engine_ms() < deadline:
            if runner.done():
                await runner
                raise RuntimeError("live loop ended before completion")
            if render.error is not None:
                raise RuntimeError(render.error)
            segment = loop.session.segment
            at_s = args.baseline_at if segment == "baseline" else 20
            elapsed = t_engine_ms() - loop.session.schedule.started_ms
            if segment in args.segments and segment not in injected and elapsed >= at_s * 1000:
                injected.add(segment)
                before = asdict(shim.stats())
                started = t_engine_ms()
                record = {
                    "segment": segment,
                    "segment_elapsed_ms": elapsed,
                    "baseline_decided_ms": loop.model.baseline_decided_ms,
                    "baseline_passed": None
                    if loop.model.baseline is None
                    else loop.model.baseline.passed,
                    "requested_seconds": args.seconds,
                    "start_ms": started,
                    "before": before,
                }
                stalls.append(record)
                print(f"{args.seconds}s: blocking {segment} at {elapsed / 1000:.2f}s", flush=True)
                time.sleep(args.seconds)  # Deliberately blocks the owning asyncio loop.
                record.update(
                    end_ms=t_engine_ms(),
                    actual_seconds=(t_engine_ms() - started) / 1000,
                    after=asdict(shim.stats()),
                )
            if any(item.session == session for item in loop.completed_metrics):
                completed = next(
                    item.as_dict() for item in loop.completed_metrics if item.session == session
                )
                await asyncio.sleep(1)
                break
            await asyncio.sleep(0.025)
        else:
            raise TimeoutError("production session did not complete within 325 seconds")
    except Exception as error:
        fault = repr(error)
    finally:
        if runner is not None:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runner
        # Keep the native chain running briefly after a loop failure, so outstanding voices
        # drain and its autonomous state is visible; this is not application crash teardown.
        await asyncio.sleep(1)
        render.close()
        stats = asdict(shim.stats())
        log.close()
        host.close()

    for stall in stalls:
        start, end = stall["start_ms"], stall["end_ms"]
        before_voice = [t for t in render.voice_starts if t <= start]
        during_voice = [t for t in render.voice_starts if start < t <= end]
        after_voice = [t for t in render.voice_starts if t > end]
        tail = before_voice[-1:] + during_voice
        last_voice = tail[-1] if tail else None
        first_after = after_voice[0] if after_voice else None
        blocks = [b for b in render.blocks if start <= b["scheduled_ms"] < end]
        stall.update(
            frames_advanced=stall["after"]["frames_rendered"] - stall["before"]["frames_rendered"],
            voices_started_during_stall=len(during_voice),
            last_voice_start_during_or_before_ms=last_voice,
            first_voice_after_ms=first_after,
            voice_resume_after_unfreeze_ms=None if first_after is None else first_after - end,
            no_heartbeat_support_ms=(
                None
                if last_voice is None or first_after is None
                else first_after - last_voice - 248
            ),
            summed_rms_during_stall=(
                math.sqrt(sum(b["rms"] ** 2 for b in blocks) / len(blocks)) if blocks else None
            ),
            summed_peak_during_stall=max((b["peak"] for b in blocks), default=None),
        )
    used = stats["frames_rendered"]
    if args.wav:
        # Diagnostic file I/O after native rendering has stopped, never inside the callback.
        pcm = np.clip(render.samples[:used], -1, 1)
        with wave.open(str(directory / "summed-output.wav"), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(RATE)
            handle.writeframes((pcm * 32767).astype("<i2").tobytes())
    report = {
        "mode": "real-clock live loop, synthetic RR, paced native offline render, no device",
        "supervisor": "bypassed to observe in-process recovery; no automatic restart",
        "clock": "fixed offline anchor; no hardware position/anchor correction claimed",
        "voice_timing": "native counter observations at 10 ms resolution, not audibility",
        "baseline_injection": f"{args.baseline_at}s after start; capture ends at45s",
        "seconds": args.seconds,
        "exception": fault,
        "completed": completed,
        "all_requested_segments_injected": {s["segment"] for s in stalls} == set(args.segments),
        "stalls": stalls,
        "native_stats": stats,
        "render_error": render.error,
        "render_lateness_ms": {
            "median": float(np.median([b["render_lateness_ms"] for b in render.blocks])),
            "max": max((b["render_lateness_ms"] for b in render.blocks), default=None),
        },
        "pushes": sink.pushes,
        "voice_starts_ms": render.voice_starts,
        "transitions": transitions,
        "summed_peak": float(np.max(np.abs(render.samples[:used]))) if used else 0,
        "final_second_peak": float(np.max(np.abs(render.samples[max(0, used - RATE) : used])))
        if used
        else 0,
    }
    (directory / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, choices=(2, 5, 10))
    parser.add_argument("--baseline-at", type=float, default=46)
    parser.add_argument("--segments", nargs="+", choices=SEGMENTS, default=list(SEGMENTS))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--wav", action="store_true")
    args = parser.parse_args(argv)
    root = args.output or Path("logs") / datetime.now().strftime("stalls-realtime-%Y%m%d-%H%M%S")
    root.mkdir(parents=True, exist_ok=True)
    if args.seconds is not None:
        result = asyncio.run(exercise(args, root))
        print(json.dumps({"results": str(root / "results.json"), "exception": result["exception"]}))
        return int(result["exception"] is not None or not result["all_requested_segments_injected"])
    children = []
    for seconds in (2, 5, 10):
        command = [
            sys.executable,
            "-m",
            "tools.check_stalls_realtime",
            "--seconds",
            str(seconds),
            "--output",
            str(root / f"{seconds}s"),
            "--baseline-at",
            str(args.baseline_at),
            "--segments",
            *args.segments,
        ]
        if args.wav:
            command.append("--wav")
        children.append(subprocess.Popen(command))
    try:
        statuses = [child.wait() for child in children]
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait()
    print(f"Real-clock results: {root}; exit statuses {statuses}")
    return int(any(statuses))


if __name__ == "__main__":
    raise SystemExit(main())
