"""Recorded PPI -> production source/scheduler -> quiet offline listening clicks.

    python -m tools.render_ppi_capture

No Bluetooth, broker, audio device, bridge session, guessed beats or normalization.
The entire capture runs once through MqttSource's actual decoder/probation/burst
logic and PpiBeatScheduler. Only afterward are strict *receipt* windows selected.
An off-arm-arrival file does NOT claim the intervals were acquired while unworn,
or that accepted artefacts are physiological beats. The capture has no acquisition
timestamps. Leading wait, every scheduled gap and the delayed tail remain intact.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median

from bridge.beat_scheduler import OK, BeatEvent
from bridge.mqtt_source import MqttSource, PpiDecoder
from bridge.packets import PpiPacket, PpiSample
from bridge.ppi_scheduler import PpiBeatScheduler
from tools.click_track import FS, _click

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAPTURE = ROOT / "private-data/physiology/captures/polar-mqtt-20260923T181756.847142Z"
DEFAULT_OUTPUT = ROOT / "private-data/physiology/recordings/ppi-listening"
TICK_MS = 20.0
CLICK_GAIN = 0.25  # Fixed, about -12 dBFS peak; never normalize a quiet/sparse stretch.


@dataclass(frozen=True)
class Window:
    name: str
    start_ms: float
    end_ms: float
    start_utc: str
    end_utc: str


@dataclass(frozen=True)
class SampleReceipt:
    sample: PpiSample
    arrival_ms: float
    source_id: str


@dataclass(frozen=True)
class Played:
    event: BeatEvent
    receipt: SampleReceipt


@dataclass
class Replay:
    origin_ns: int
    windows: tuple[Window, ...]
    decoded: list[SampleReceipt]
    delivered: list[SampleReceipt]
    accepted: list[SampleReceipt]
    rejected: list[SampleReceipt]
    played: list[Played]
    source_counters: dict[str, int]
    scheduler_stats: dict[str, int]
    scheduler_quality_rejected: int
    raw_ppi_samples: int
    buffer_ms: float


class ReplayClock:
    value = 0.0

    def __call__(self) -> float:
        return self.value


class ReceiptDecoder(PpiDecoder):
    """Observe production decoding, retaining only offline provenance, not new rules."""

    def __init__(self, config):
        super().__init__(config)
        self.receipts: dict[int, SampleReceipt] = {}

    def decode(self, topic, payload, arrived_ms, *, retained=False):
        message = super().decode(topic, payload, arrived_ms, retained=retained)
        if message is not None:
            for sample in message.samples:
                self.receipts[id(sample)] = SampleReceipt(sample, arrived_ms, message.source_id)
        return message


def capture_windows(records: list[dict], origin_ns: int) -> tuple[Window, ...]:
    markers: dict[str, list[dict]] = {}
    for record in records:
        if record.get("kind") == "marker":
            marker = record["marker"]
            markers.setdefault(marker["name"], []).append(marker)
    ppi = [
        record
        for record in records
        if record.get("topic", "").endswith("/ecg") and not record.get("retain", False)
    ]
    if not ppi:
        raise ValueError("Capture has no PPI publications")
    # Exactly the strict receipt windows in the probe's analysis, not its coarse
    # phase strings (which include ambiguous request/confirmation transitions).
    pairs = (
        ("seated_before_removal", ppi[0], markers["off_requested"][0]),
        ("confirmed_off_arm", markers["off"][-1], markers["on_requested"][-1]),
        ("recovered_on_arm", markers["on"][-1], markers["stop"][-1]),
    )
    windows = []
    for name, start, end in pairs:
        start_ms = (start["perf_counter_ns"] - origin_ns) / 1e6
        end_ms = (end["perf_counter_ns"] - origin_ns) / 1e6
        if not 0 <= start_ms < end_ms:
            raise ValueError(f"Invalid strict window {name}")
        windows.append(Window(name, start_ms, end_ms, start["utc"], end["utc"]))
    return tuple(windows)


def replay_capture(directory: Path = DEFAULT_CAPTURE, *, on_result=None) -> Replay:
    records = [
        json.loads(line)
        for line in (directory / "raw.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    messages = sorted(
        (record for record in records if record.get("kind") == "mqtt_message"),
        key=lambda item: item["perf_counter_ns"],
    )
    if not messages:
        raise ValueError("Capture contains no MQTT messages")
    origin_ns = messages[0]["perf_counter_ns"]
    windows = capture_windows(records, origin_ns)
    expected = json.loads((directory / "analysis.json").read_text(encoding="utf-8"))
    for window in windows:
        prior = expected["strict_windows"][window.name]
        if (window.start_utc, window.end_utc) != (prior["start_utc"], prior["end_utc"]):
            raise ValueError("Raw markers disagree with the recorded strict-window analysis")
    clock = ReplayClock()
    source = MqttSource(clock=clock)
    decoder = ReceiptDecoder(source.config)
    source.decoder = decoder
    source.connected = True  # Offline equivalent of successful local-broker subscription.
    scheduler = PpiBeatScheduler()
    delivered, accepted, rejected, played = [], [], [], []
    provenance: dict[tuple[float, float], SampleReceipt] = {}

    def handle_packets(packets: list[PpiPacket]) -> None:
        for packet in packets:
            receipts = [decoder.receipts[id(sample)] for sample in packet.samples]
            delivered.extend(receipts)
            result = scheduler.on_packet(clock.value, packet)
            if on_result is not None:
                on_result(clock.value, result)
            # The scheduler may discard an overlapping *prefix*. All positive
            # PPIs after that advance strictly, so returned intervals are the
            # suffix, in input order. Fail loudly if that contract ever changes.
            count = len(result.intervals)
            selected = receipts[-count:] if count else []
            for receipt, interval in zip(selected, result.intervals, strict=True):
                if receipt.sample.rr_ms != interval.rr_ms:
                    raise AssertionError("Scheduler/source sample provenance no longer aligns")
                (accepted if interval.accepted else rejected).append(receipt)
                if interval.accepted:
                    target = interval.t_beat + scheduler.t.buffer_ms
                    provenance[(round(target, 6), interval.rr_ms)] = receipt
            if any(event.quality == OK for event in result.events):
                raise AssertionError("Measured PPI onset publication moved out of tick")

    index, now = 0, 0.0
    last_ms = (messages[-1]["perf_counter_ns"] - origin_ns) / 1e6
    end_ms = last_ms + scheduler.t.buffer_ms + 2000.0
    while now <= end_ms:
        while index < len(messages):
            message = messages[index]
            at = (message["perf_counter_ns"] - origin_ns) / 1e6
            if at > now:
                break
            clock.value = at
            source._mailbox.append(
                (
                    "message",
                    message["topic"],
                    base64.b64decode(message["payload_base64"]),
                    at,
                    bool(message.get("retain", False)),
                )
            )
            handle_packets(source._drain())
            index += 1
        clock.value = now
        handle_packets(source._drain())
        for event in scheduler.tick(now):
            if event.quality != OK:
                raise AssertionError("Recorded PPI listening must never manufacture a beat")
            receipt = provenance.pop((round(event.t_play, 6), event.rr_ms))
            played.append(Played(event, receipt))
        now += TICK_MS
    raw_ppi_samples = sum(len(record.get("json", {}).get("ppi", [])) for record in messages)
    return Replay(
        origin_ns,
        windows,
        list(decoder.receipts.values()),
        delivered,
        accepted,
        rejected,
        played,
        dict(decoder.counters),
        asdict(scheduler.stats),
        scheduler.quality_rejected,
        raw_ppi_samples,
        scheduler.t.buffer_ms,
    )


def distribution(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(values),
        "min": min(values),
        "median": median(values),
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "max": max(values),
    }


def in_window(receipt: SampleReceipt, window: Window) -> bool:
    return window.start_ms <= receipt.arrival_ms < window.end_ms


def render_window(replay: Replay, window: Window, path: Path) -> dict:
    import numpy as np

    selected = [beat for beat in replay.played if in_window(beat.receipt, window)]
    click = _click(1200.0, 30.0, CLICK_GAIN)
    end_ms = max([window.end_ms] + [beat.event.t_play + 30 for beat in selected]) + 100.0
    samples = np.zeros(math.ceil((end_ms - window.start_ms) * FS / 1000.0), dtype=np.float32)
    onsets = []
    for beat in selected:
        frame = round((beat.event.t_play - window.start_ms) * FS / 1000.0)
        if frame < 0:
            raise AssertionError("A selected beat predates the receipt window")
        samples[frame : frame + len(click)] += click
        onsets.append(
            {
                "frame": frame,
                "arrival_ms_from_window_start": beat.receipt.arrival_ms - window.start_ms,
                "scheduled_onset_ms_from_window_start": beat.event.t_play - window.start_ms,
                "callback_to_onset_ms": beat.event.t_play - beat.receipt.arrival_ms,
                "publish_lead_ms": beat.event.t_play - beat.event.t_emitted,
                "rr_ms": beat.event.rr_ms,
                "error_estimate_ms": beat.receipt.sample.error_ms,
                "blocker": beat.receipt.sample.blocked,
                "reported_contact_DIAGNOSTIC_ONLY": beat.receipt.sample.reported_contact,
            }
        )
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    if peak > 10 ** (-6 / 20):
        raise AssertionError("Listening file exceeds the fixed -6 dBFS ceiling")
    pcm = np.rint(samples * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(FS)
        output.writeframes(pcm.tobytes())
    counts = {
        name: sum(in_window(receipt, window) for receipt in getattr(replay, name))
        for name in ("decoded", "delivered", "accepted", "rejected")
    }
    return {
        "file": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "window": asdict(window),
        "receipt_counts": counts,
        "played_clicks": len(selected),
        "duration_s": len(samples) / FS,
        "first_click_s": onsets[0]["frame"] / FS if onsets else None,
        "sample_rate": FS,
        "channels": 1,
        "pcm_bits": 16,
        "fixed_click_gain": CLICK_GAIN,
        "normalization": False,
        "sample_peak_dbfs": 20 * math.log10(peak) if peak else None,
        "callback_to_onset_ms": distribution([item["callback_to_onset_ms"] for item in onsets]),
        "publish_lead_ms": distribution([item["publish_lead_ms"] for item in onsets]),
        "onsets": onsets,
    }


def generate(directory: Path, output: Path, *, overwrite: bool = False) -> dict:
    replay = replay_capture(directory)
    targets = [output / f"{window.name}.wav" for window in replay.windows]
    targets.append(output / "timing-report.json")
    if not overwrite and any(target.exists() for target in targets):
        raise FileExistsError(
            "Listening files exist; use --overwrite to regenerate these exact outputs"
        )
    output.mkdir(parents=True, exist_ok=True)
    reports = [
        render_window(replay, window, path)
        for window, path in zip(replay.windows, targets[:-1], strict=True)
    ]
    report = {
        "capture": str(directory.resolve()),
        "capture_sha256": hashlib.sha256((directory / "raw.jsonl").read_bytes()).hexdigest(),
        "pipeline": "MqttSource -> PpiBeatScheduler -> tools.click_track._click",
        "tick_ms": TICK_MS,
        "reconstructed_timeline_buffer_ms": replay.buffer_ms,
        "raw_ppi_samples": replay.raw_ppi_samples,
        "decoded_samples": len(replay.decoded),
        "source_delivered_samples": len(replay.delivered),
        "source_counters": replay.source_counters,
        "scheduler_stats": replay.scheduler_stats,
        "quality_rejected": replay.scheduler_quality_rejected,
        "played_clicks": len(replay.played),
        "all_played_callback_to_onset_ms": distribution(
            [item.event.t_play - item.receipt.arrival_ms for item in replay.played]
        ),
        "all_played_publish_lead_ms": distribution(
            [item.event.t_play - item.event.t_emitted for item in replay.played]
        ),
        "files": reports,
        "limitations": [
            "Off-arm means received in the strict confirmed-off window, not acquired while unworn.",
            "Accepted intervals are algorithm output, not proof of physiological truth or wear.",
            "Phone/optical times absent; callback-to-onset is NOT physical beat latency.",
            "Source probation and quality rejection create silence; nothing fills it.",
            "Each WAV keeps original timing, leading wait and delayed tail; no normalization.",
            "Offline 20 ms ticks verify scheduling, not DAC latency or live network reliability.",
        ],
    }
    targets[-1].write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, default=DEFAULT_CAPTURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    report = generate(args.capture, args.output, overwrite=args.overwrite)
    print(json.dumps({key: value for key, value in report.items() if key != "files"}, indent=2))
    for item in report["files"]:
        print(json.dumps({key: value for key, value in item.items() if key != "onsets"}, indent=2))


if __name__ == "__main__":
    main()
