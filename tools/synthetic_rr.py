"""Imitates what a Polar Verity Sense sends over Bluetooth, so the pipeline can be built
before the armband arrives.

The device does not send one message per heartbeat. It notifies about once a second, and
each notification carries the RR intervals for the beats that completed since the last
one: usually one, sometimes two, sometimes none. RR intervals are in units of 1/1024 s.
Nothing here converts them. The parse side does (bridge/hrm.py), so the conversion is
exercised end to end.

Run from the repo root:  python -m tools.synthetic_rr --help
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from bridge.hrm import encode_hrm, parse_hrm, rr_ms_to_raw

NOTIFY_INTERVAL_S = 1.0
NOTIFY_JITTER_S = 0.08
RR_SD_MS_AT_60_BPM = 30.0  # beat-to-beat variability; shrinks with the square of the rate
HR_MAX_REPORTABLE = 0xFF  # the device reports heart rate as one byte

# --- heart rate profile ----------------------------------------------------------------------


@dataclass(frozen=True)
class Ramp:
    seconds: float
    start_bpm: float
    end_bpm: float


@dataclass(frozen=True)
class Profile:
    """A piecewise-linear heart rate curve."""

    ramps: tuple[Ramp, ...]

    @property
    def duration_s(self) -> float:
        return sum(r.seconds for r in self.ramps)

    def hr_at(self, t_s: float) -> float:
        t = t_s
        for ramp in self.ramps:
            if t <= ramp.seconds:
                return ramp.start_bpm + (ramp.end_bpm - ramp.start_bpm) * (t / ramp.seconds)
            t -= ramp.seconds
        return self.ramps[-1].end_bpm

    @classmethod
    def from_spec(cls, spec: str) -> Profile:
        """'68:45,68-105:75' is 45 s at 68 bpm, then 75 s climbing from 68 to 105."""
        ramps = []
        for part in spec.split(","):
            bpm, _, seconds = part.partition(":")
            start, _, end = bpm.partition("-")
            if not seconds or float(seconds) <= 0:
                raise ValueError(f"profile segment needs a positive duration: {part!r}")
            ramps.append(Ramp(float(seconds), float(start), float(end or start)))
        return cls(tuple(ramps))


DEFAULT_PROFILE_SPEC = "68:45,68-105:75,105-75:75,75:45"
DEFAULT_PROFILE = Profile.from_spec(DEFAULT_PROFILE_SPEC)

# --- beats -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Beat:
    t_s: float  # when the device detected the beat, on its own clock
    rr_ms: float  # the interval this beat closed


def true_beats(profile: Profile, rng: random.Random) -> Iterator[Beat]:
    """The heart as it beats: the profile's rate plus a little variability."""
    t = 0.0
    while True:
        bpm = profile.hr_at(t)
        sd_ms = RR_SD_MS_AT_60_BPM * (60.0 / bpm) ** 2
        rr_ms = max(300.0, 60_000.0 / bpm + rng.gauss(0.0, sd_ms))
        t += rr_ms / 1000.0
        if t > profile.duration_s:
            return
        yield Beat(t, rr_ms)


# --- faults ----------------------------------------------------------------------------------

FAULT_KINDS = (
    "dropped_packet",
    "doubled_beat",
    "missed_beat",
    "artefact_burst",
    "disconnect",
    "contact_lost",
)
DEFAULT_FAULT_SECONDS = {"artefact_burst": 5.0, "disconnect": 10.0, "contact_lost": 5.0}


@dataclass(frozen=True)
class Fault:
    kind: str
    at_s: float  # device time at which it fires
    seconds: float = 0.0  # length, for artefact_burst, disconnect and contact_lost

    @classmethod
    def from_spec(cls, spec: str) -> Fault:
        """'doubled_beat@60' or 'disconnect@100:8', i.e. kind@seconds[:length]."""
        kind, _, when = spec.partition("@")
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault {kind!r}; one of {', '.join(FAULT_KINDS)}")
        at, _, length = when.partition(":")
        if not at:
            raise ValueError(f"fault needs a time, kind@seconds: {spec!r}")
        seconds = float(length) if length else DEFAULT_FAULT_SECONDS.get(kind, 0.0)
        return cls(kind, float(at), seconds)


def apply_beat_faults(
    beats: Iterator[Beat], faults: Sequence[Fault], rng: random.Random
) -> Iterator[Beat]:
    """What the sensor detects, as opposed to what the heart did."""
    for fault in faults:
        if fault.kind == "doubled_beat":
            beats = _doubled_beat(beats, fault.at_s, rng)
        elif fault.kind == "missed_beat":
            beats = _missed_beat(beats, fault.at_s)
        elif fault.kind == "artefact_burst":
            beats = _artefact_burst(beats, fault.at_s, fault.seconds, rng)
    return beats


def _doubled_beat(beats: Iterator[Beat], at_s: float, rng: random.Random) -> Iterator[Beat]:
    """One false extra detection: an interval splits into a short one and the remainder."""
    done = False
    for beat in beats:
        if not done and beat.t_s >= at_s:
            short_ms = rng.uniform(180.0, 290.0)
            t_prev = beat.t_s - beat.rr_ms / 1000.0
            yield Beat(t_prev + short_ms / 1000.0, short_ms)
            yield Beat(beat.t_s, beat.rr_ms - short_ms)
            done = True
        else:
            yield beat


def _missed_beat(beats: Iterator[Beat], at_s: float) -> Iterator[Beat]:
    """One beat not detected: two intervals merge into one about twice as long."""
    held: Beat | None = None
    armed = True
    for beat in beats:
        if held is not None:
            yield Beat(beat.t_s, held.rr_ms + beat.rr_ms)
            held = None
        elif armed and beat.t_s >= at_s:
            held = beat
            armed = False
        else:
            yield beat


def _artefact_burst(
    beats: Iterator[Beat], at_s: float, seconds: float, rng: random.Random
) -> Iterator[Beat]:
    """Arm movement: for a few seconds the sensor reports beats that are not there."""
    t_last = 0.0
    state = "before"
    for beat in beats:
        if state == "before" and beat.t_s >= at_s:
            t = t_last
            while t < at_s + seconds:
                rr_ms = rng.uniform(250.0, 1400.0)
                t += rr_ms / 1000.0
                yield Beat(t, rr_ms)
            t_last = t
            state = "resync"
            continue
        if state == "resync":
            if beat.t_s <= t_last:
                continue  # real beats the garbage ran over are gone
            # The first real detection after the burst closes the gap since the last false one.
            yield Beat(beat.t_s, (beat.t_s - t_last) * 1000.0)
            state = "after"
        else:
            yield beat
        t_last = beat.t_s


def apply_packet_faults(
    notes: Iterator[Notification], faults: Sequence[Fault]
) -> Iterator[Notification]:
    """What the link delivers, as opposed to what the device sent. Lost packets are lost:
    the device does not resend, so their beats never reach the host."""
    for fault in faults:
        if fault.kind == "dropped_packet":
            notes = _drop_one(notes, fault.at_s)
        elif fault.kind == "disconnect":
            notes = _drop_window(notes, fault.at_s, fault.at_s + fault.seconds)
        elif fault.kind == "contact_lost":
            notes = _lose_contact(notes, fault.at_s, fault.at_s + fault.seconds)
    return notes


def _drop_one(notes: Iterator[Notification], at_s: float) -> Iterator[Notification]:
    dropped = False
    for note in notes:
        if not dropped and note.t_s >= at_s:
            dropped = True
            continue
        yield note


def _drop_window(
    notes: Iterator[Notification], from_s: float, until_s: float
) -> Iterator[Notification]:
    for note in notes:
        if not from_s <= note.t_s < until_s:
            yield note


def _lose_contact(
    notes: Iterator[Notification], from_s: float, until_s: float
) -> Iterator[Notification]:
    """The sensor-contact bit reads false, and everything else is sent as before. Whether the
    Verity Sense keeps sending RR intervals without contact is not known yet."""
    for note in notes:
        if from_s <= note.t_s < until_s:
            packet = parse_hrm(note.payload)
            payload = encode_hrm(packet.hr_bpm, packet.rr_raw, contact_detected=False)
            note = Notification(note.t_s, payload)
        yield note


# --- notifications ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Notification:
    t_s: float  # when the notification leaves the device
    payload: bytes  # the characteristic value, exactly as a BLE client receives it


def notifications(
    beats: Iterator[Beat],
    end_s: float,
    rng: random.Random,
    interval_s: float = NOTIFY_INTERVAL_S,
    jitter_s: float = NOTIFY_JITTER_S,
) -> Iterator[Notification]:
    """Bundle beats into a notification about every second. The heart rate field is the
    device's own estimate, here the mean of its last four intervals."""
    recent: deque[float] = deque(maxlen=4)
    pending: list[Beat] = []

    def next_time(t: float) -> float:
        return t + interval_s + rng.uniform(-jitter_s, jitter_s)

    def flush(t: float) -> Notification:
        hr_bpm = round(60_000.0 * len(recent) / sum(recent)) if recent else 0
        rr_raw = [rr_ms_to_raw(b.rr_ms) for b in pending]
        payload = encode_hrm(min(hr_bpm, HR_MAX_REPORTABLE), rr_raw)
        pending.clear()
        return Notification(t, payload)

    t_next = next_time(0.0)
    for beat in beats:
        while beat.t_s > t_next:
            yield flush(t_next)
            t_next = next_time(t_next)
        pending.append(beat)
        recent.append(beat.rr_ms)
    while t_next <= end_s:
        yield flush(t_next)
        t_next = next_time(t_next)
    if pending:
        yield flush(t_next)


def generate(
    profile: Profile = DEFAULT_PROFILE, faults: Sequence[Fault] = (), seed: int = 1
) -> Iterator[Notification]:
    """The whole device, as a stream of notifications. Same seed, same stream."""
    beats = true_beats(profile, random.Random(f"{seed}:beats"))
    beats = apply_beat_faults(beats, faults, random.Random(f"{seed}:faults"))
    notes = notifications(beats, profile.duration_s, random.Random(f"{seed}:notify"))
    return apply_packet_faults(notes, faults)


# --- command line ----------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synthetic Polar Verity Sense: heart rate notifications as JSON lines.",
        epilog="Faults: " + ", ".join(FAULT_KINDS) + ". Example: --fault disconnect@100:8",
    )
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE_SPEC,
        metavar="SPEC",
        help="bpm[-bpm]:seconds, comma separated (default: %(default)s)",
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--fault",
        action="append",
        default=[],
        metavar="KIND@SECONDS[:LENGTH]",
        help="inject a fault; repeatable",
    )
    parser.add_argument(
        "--realtime",
        action="store_true",
        help="pace output to the device clock instead of dumping it at once",
    )
    args = parser.parse_args(argv)

    profile = Profile.from_spec(args.profile)
    faults = [Fault.from_spec(spec) for spec in args.fault]
    start = time.monotonic()
    for note in generate(profile, faults, args.seed):
        if args.realtime:
            delay = start + note.t_s - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        packet = parse_hrm(note.payload)
        line = {
            "t_ms": round(note.t_s * 1000),
            "payload": note.payload.hex(),
            "hr_bpm": packet.hr_bpm,
            "rr_ms": [round(ms, 1) for ms in packet.rr_ms],
        }
        print(json.dumps(line), flush=args.realtime)
    return 0


if __name__ == "__main__":
    sys.exit(main())
