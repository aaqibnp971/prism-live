"""Listen to the beat scheduler.

Runs the synthetic armband in real time through the scheduler and plays a short click at
every t_play on the default output device: a bright click for an ok beat, a duller one for
an interpolated beat, nothing for a rejected interval. Every event is printed as it is
emitted. This exists so a person can judge whether the result feels like a heartbeat. It is
not production code and the heartbeat layer proper is built elsewhere.

    python -m tools.click_track
    python -m tools.click_track --fault disconnect@100:8 --fault artefact_burst@150

Needs the audio extra:  pip install sounddevice numpy
"""

from __future__ import annotations

import argparse
import queue
import sys
import time  # perf_counter throughout: time.monotonic moves in 15.6 ms steps on Windows
from collections.abc import Sequence

from bridge.beat_scheduler import REJECTED, BeatEvent, BeatScheduler
from tools.synthetic_rr import DEFAULT_PROFILE_SPEC, FAULT_KINDS, Fault, Profile, generate

FS = 48_000
BLOCK = 256


def _click(freq_hz: float, length_ms: float, gain: float):
    import numpy as np

    t = np.arange(int(FS * length_ms / 1000)) / FS
    return (gain * np.sin(2 * np.pi * freq_hz * t) * np.exp(-t / (length_ms / 4000))).astype(
        np.float32
    )


class ClickPlayer:
    """Mixes clicks into the output stream at their scheduled times on time.perf_counter.

    Frame 0 of the first block is taken to play at the moment the first callback runs, so
    every click lands a constant output latency late. That is fine for listening.
    """

    def __init__(self) -> None:
        self.queue: queue.SimpleQueue = queue.SimpleQueue()
        self.pending: list = []
        self.origin_ms: float | None = None
        self.frames_done = 0

    def schedule(self, t_play_ms: float, sample) -> None:
        self.queue.put((t_play_ms, sample))

    def callback(self, out, frames: int, _time_info, status) -> None:
        if status:
            print(status, file=sys.stderr)
        if self.origin_ms is None:
            self.origin_ms = time.perf_counter() * 1000
        out.fill(0)
        block_start_ms = self.origin_ms + self.frames_done * 1000 / FS
        while True:
            try:
                self.pending.append(self.queue.get_nowait())
            except queue.Empty:
                break
        keep = []
        for t_play_ms, sample in self.pending:
            offset = int((t_play_ms - block_start_ms) / 1000 * FS)  # frames into this block
            if offset >= frames:
                keep.append((t_play_ms, sample))  # a later block
                continue
            start = max(offset, 0)
            src = start - offset
            n = min(frames - start, len(sample) - src)
            if n > 0:
                out[start : start + n, 0] += sample[src : src + n]
            if offset + len(sample) > frames:
                keep.append((t_play_ms, sample))  # its tail spills into the next block
        self.pending = keep
        self.frames_done += frames


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Play the beat scheduler's output as clicks, from the synthetic armband.",
        epilog="Faults: " + ", ".join(FAULT_KINDS) + ". Example: --fault disconnect@100:8",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE_SPEC, metavar="SPEC")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--fault", action="append", default=[], metavar="KIND@SECONDS[:LENGTH]")
    parser.add_argument("--seconds", type=float, default=None, help="stop after this long")
    parser.add_argument("--latency-ms", type=float, default=40.0, help="simulated link latency")
    args = parser.parse_args(argv)

    try:
        import sounddevice as sd
    except ImportError:
        print("click_track needs sounddevice and numpy:  pip install sounddevice numpy")
        return 1

    profile = Profile.from_spec(args.profile)
    faults = [Fault.from_spec(spec) for spec in args.fault]
    clicks = {"ok": _click(1200, 30, 0.6), "interpolated": _click(600, 45, 0.35)}
    player = ClickPlayer()
    sched = BeatScheduler()
    start = time.perf_counter()

    def handle(event: BeatEvent) -> None:
        print(
            f"{event.t_play / 1000 - start:8.3f} s  {event.quality:<12} rr {event.rr_ms:7.1f}  "
            f"hr {event.hr_bpm:5.1f}  step {event.phase_step_ms:+5.1f}"
        )
        if event.quality != REJECTED:
            player.schedule(event.t_play, clicks[event.quality])

    print(f"profile {args.profile}  seed {args.seed}  faults {args.fault or 'none'}")
    print("t_play      quality      rr          hr     phase step   (ctrl-c stops)")
    stream = sd.OutputStream(
        samplerate=FS, channels=1, dtype="float32", blocksize=BLOCK, callback=player.callback
    )
    try:
        with stream:
            for note in generate(profile, faults, args.seed):
                if args.seconds is not None and note.t_s > args.seconds:
                    break
                due = start + note.t_s + args.latency_ms / 1000
                while True:
                    now = time.perf_counter()
                    for event in sched.tick(now * 1000):
                        handle(event)
                    if now >= due:
                        break
                    time.sleep(min(0.02, due - now))
                for event in sched.on_packet(time.perf_counter() * 1000, note.payload).events:
                    handle(event)
            for _ in range(75):  # let the last scheduled beats play out
                for event in sched.tick(time.perf_counter() * 1000):
                    handle(event)
                time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    print(sched.stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
