"""Connect to the laptop and print every message with its scheduling headroom (contract §5).

For a beat, headroom is how far ahead of its t_play the beat arrived here, measured on this
machine's clock through the contract's clock exchange. Under 0 ms the beat is useless to a
client, which must drop it. Under 300 ms the link is eating the laptop's lead. This is how
the headroom is shown to be real before anything renders.

It identifies as a spectator, checks every message against the contract, and watches for
gaps in seq. A summary prints on exit.

    python -m tools.fake_receiver
    python -m tools.fake_receiver --url ws://192.168.8.10:8787/live --seconds 60
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, InvalidURI

from bridge.clock_sync import PING_INTERVAL_MS, ClockSync
from bridge.contract import (
    MIN_LEAD_MS,
    PATH,
    PORT,
    VERSION,
    ContractError,
    decode,
    encode,
    validate,
)

HELLO = {"type": "hello", "v": VERSION, "client": "spectator", "build": "fake_receiver"}


def local_ms() -> float:
    """This client's clock. perf_counter: time.monotonic moves in 15.6 ms steps on Windows."""
    return time.perf_counter() * 1000


@dataclass
class Summary:
    counts: dict[str, int] = field(default_factory=dict)
    qualities: dict[str, int] = field(default_factory=dict)
    headroom_ms: list[float] = field(default_factory=list)  # ok and interpolated beats
    unsynced_beats: int = 0  # arrived before the first clock sample
    state_age_ms: list[float] = field(default_factory=list)
    seq_gaps: int = 0
    contract_errors: int = 0
    clock_used: int = 0
    clock_discarded: int = 0
    offset_ms: float | None = None
    closed: str | None = None

    @property
    def under_lead(self) -> int:
        return sum(1 for h in self.headroom_ms if h < MIN_LEAD_MS)

    @property
    def late(self) -> int:
        return sum(1 for h in self.headroom_ms if h < 0)

    def report(self) -> str:
        beats = self.counts.get("beat", 0)
        lines = [
            f"beats {beats}  " + "  ".join(f"{q} {n}" for q, n in sorted(self.qualities.items()))
        ]
        if self.headroom_ms:
            h = self.headroom_ms
            lines.append(
                f"headroom  min {min(h):.0f} ms  median {statistics.median(h):.0f} ms  "
                f"max {max(h):.0f} ms  under {MIN_LEAD_MS} ms: {self.under_lead}  late: {self.late}"
                + (f"  before clock sync: {self.unsynced_beats}" if self.unsynced_beats else "")
            )
        if self.state_age_ms:
            lines.append(
                f"states {self.counts.get('state', 0)}  age median "
                f"{statistics.median(self.state_age_ms):.0f} ms"
            )
        offset = "none" if self.offset_ms is None else f"{self.offset_ms:+.1f} ms"
        lines.append(
            f"clock  samples used {self.clock_used}  discarded {self.clock_discarded}  "
            f"offset {offset}"
        )
        lines.append(f"seq gaps {self.seq_gaps}  contract violations {self.contract_errors}")
        if self.closed:
            lines.append(f"closed  {self.closed}")
        return "\n".join(lines)


async def receive(
    url: str,
    seconds: float | None = None,
    out: Callable[[str], None] = print,
    summary: Summary | None = None,
) -> Summary:
    summary = summary if summary is not None else Summary()
    sync = ClockSync()
    last_seq: dict[tuple[str, str], int] = {}
    start = local_ms()

    async with connect(url) as ws:
        await ws.send(encode(HELLO))

        async def ping() -> None:
            while True:
                await ws.send(encode(sync.ping(local_ms())))
                await asyncio.sleep(PING_INTERVAL_MS / 1000)

        pinger = asyncio.create_task(ping())
        try:
            async with asyncio.timeout(seconds):
                async for text in ws:
                    now = local_ms()
                    _show(text, now, now - start, sync, last_seq, summary, out)
        except TimeoutError:
            pass
        except ConnectionClosed:
            pass
        finally:
            pinger.cancel()
        if ws.close_code is not None:
            summary.closed = f"{ws.close_code} {ws.close_reason or ''}".strip()
    return summary


def _show(
    text: str,
    now: float,
    elapsed: float,
    sync: ClockSync,
    last_seq: dict[tuple[str, str], int],
    summary: Summary,
    out: Callable[[str], None],
) -> None:
    at = f"{elapsed / 1000:9.3f} s"
    try:
        msg = decode(text)
        validate(msg, "out")
    except ContractError as error:
        summary.contract_errors += 1
        out(f"{at}  CONTRACT VIOLATION  {error}  {text[:160]}")
        return
    kind = msg["type"]
    summary.counts[kind] = summary.counts.get(kind, 0) + 1

    if kind == "clock":
        rtt = now - msg["t_client_sent"]
        if sync.on_pong(msg, now):
            summary.clock_used += 1
        else:
            summary.clock_discarded += 1
        summary.offset_ms = sync.offset
        offset = "none" if sync.offset is None else f"{sync.offset:+.1f} ms"
        out(f"{at}  clock  offset {offset}  rtt {rtt:.1f} ms  samples {sync.samples}")
        return

    key = (kind, msg["session"])
    if key in last_seq and msg["seq"] != last_seq[key] + 1:
        summary.seq_gaps += 1
        out(f"{at}  GAP  {kind} seq {last_seq[key]} then {msg['seq']}")
    last_seq[key] = msg["seq"]

    if kind == "beat":
        quality = msg["quality"]
        summary.qualities[quality] = summary.qualities.get(quality, 0) + 1
        if not sync.ready:
            summary.unsynced_beats += 1
            ahead = "t_play  ?  (no clock sample yet)"
        else:
            headroom = sync.to_local(msg["t_play"]) - now
            if quality == "rejected":
                # Sent for logging, never rendered, so its headroom does not count.
                flag = "  not rendered"
            else:
                summary.headroom_ms.append(headroom)
                flag = (
                    "  LATE" if headroom < 0 else "  UNDER LEAD" if headroom < MIN_LEAD_MS else ""
                )
            ahead = f"t_play in {headroom:+5.0f} ms{flag}"
        out(f"{at}  beat   seq {msg['seq']:>5}  {quality:<12}  {ahead}  hr {msg['hr_bpm']:5.1f}")
        return

    age = ""
    if sync.ready:
        age_ms = now - sync.to_local(msg["t_engine"])
        summary.state_age_ms.append(age_ms)
        age = f"  age {age_ms:+.0f} ms"
    hr = "  -  " if msg["hr_bpm"] is None else f"{msg['hr_bpm']:5.1f}"
    progress = f"{msg['segment_elapsed_ms'] / 1000:5.1f}/{msg['segment_nominal_ms'] / 1000:.0f} s"
    out(f"{at}  state  seq {msg['seq']:>5}  {msg['segment']:<8}  {progress}  hr {hr}{age}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the live stream with its headroom.")
    parser.add_argument("--url", default=f"ws://localhost:{PORT}{PATH}")
    parser.add_argument("--seconds", type=float, default=None, help="stop after this long")
    args = parser.parse_args(argv)
    summary = Summary()
    try:
        asyncio.run(receive(args.url, args.seconds, summary=summary))
    except KeyboardInterrupt:
        pass
    except (OSError, InvalidHandshake, InvalidURI) as error:
        print(f"could not connect to {args.url}: {error}")
        if isinstance(error, InvalidStatus) and error.response.body:
            print(error.response.body.decode("utf-8", "replace").strip())
        return 1
    print()
    print(summary.report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
