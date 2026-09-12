"""Replay a recorded session over the live link, at real speed (contract §5).

Every client is built against this before the bridge exists. It reads a session log
(tools/fixtures/synthetic-clean.jsonl by default), takes what the laptop sent, ``beat`` and
``state``, and sends it again on a real LiveServer with the recorded spacing.

Timestamps on T_engine move to now by one fixed shift of whole milliseconds: beat ``t_play``
and state ``t_engine``. Everything else is relative and left alone, and each message goes out
at its shifted send time on T_engine, so a replayed beat leaves with the headroom it had when
it was recorded, give or take the event loop's wake-up. Each replay takes a fresh session id
and is logged like a live session, so a replay never writes into the recording it came from.

The server answers clock pings and logs hello and task_event exactly as the bridge will.

    python -m tools.fake_sender              waits for the first client, plays once
    python -m tools.fake_sender --loop       plays again, as a new session, until ctrl-c
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path

from bridge.contract import PATH, PORT
from bridge.logging import SessionLog
from bridge.server import Client, LiveServer

DEFAULT_FIXTURE = Path(__file__).parent / "fixtures" / "synthetic-clean.jsonl"
REPLAYED = ("beat", "state")
# asyncio's timer on Windows before Python 3.13 wakes in 15.6 ms steps, early or late. Sleep
# until this close to a send, then yield to the loop until T_engine says it is time.
FINE_WAIT_MS = 20.0


def load_outbound(path: str | Path) -> list[tuple[float, dict]]:
    """(t_engine sent, message) for every beat and state the laptop sent, in order."""
    records = []
    with Path(path).open(encoding="utf-8") as lines:
        for line in lines:
            if not line.strip():
                continue
            record = json.loads(line)
            msg = record.get("msg")
            if record.get("dir") == "out" and msg and msg.get("type") in REPLAYED:
                records.append((record["t_engine"], msg))
    if not records:
        raise ValueError(f"{path}: no beat or state messages to replay")
    return records


def rebase(msg: dict, shift_ms: float, session: str) -> dict:
    """A copy of msg, moved to now by shift_ms, in the replay's session."""
    out = dict(msg, session=session)
    if out["type"] == "beat":
        out["t_play"] = round(msg["t_play"] + shift_ms)
    else:
        out["t_engine"] = round(msg["t_engine"] + shift_ms)
    return out


async def replay(
    server: LiveServer, records: list[tuple[float, dict]], seconds: float | None = None
) -> tuple[int, int]:
    """Play records once, at real speed, into the server's current session.

    Returns (sent, not sent). A beat is not sent if the replay fell too far behind to give it
    300 ms of headroom, which the server logs.
    """
    session = server.log.session or server.log.start_session()
    first = records[0][0]
    shift = round(server.clock() - first)  # whole ms: shifted integer times stay exact
    sent = not_sent = 0
    for t_sent, msg in records:
        if seconds is not None and t_sent - first > seconds * 1000:
            break
        # Late would eat into a beat's headroom; early would send a state from the future.
        while (wait_ms := t_sent + shift - server.clock()) > 0:
            await asyncio.sleep((wait_ms - FINE_WAIT_MS) / 1000 if wait_ms > FINE_WAIT_MS else 0)
        if server.publish(rebase(msg, shift, session)):
            sent += 1
        else:
            not_sent += 1
    return sent, not_sent


async def run(args: argparse.Namespace) -> None:
    records = load_outbound(args.fixture)
    duration_s = (records[-1][0] - records[0][0]) / 1000
    log = SessionLog(args.log_dir)
    log.start_session()

    def task_event(msg: dict, client: Client) -> None:
        print(f"  {client.label}: task_event {msg['event']}")

    async with LiveServer(log, host=args.host, port=args.port, on_task_event=task_event) as server:
        print(f"fake_sender  ws://{args.host or 'localhost'}:{server.port}{PATH}")
        print(f"  {Path(args.fixture).name}: {len(records)} messages over {duration_s:.0f} s")
        replays = 0
        while True:
            if not args.no_wait and not server.clients:
                print("  waiting for a client to say hello")
                await server.wait_for_client()
            if replays:
                log.start_session()
            print(f"  session {log.session}: playing")
            sent, not_sent = await replay(server, records, args.seconds)
            print(f"  session {log.session}: sent {sent}, not sent {not_sent}  ({log.path})")
            replays += 1
            if not args.loop:
                break
        await asyncio.sleep(1.0)  # the last beats are still ahead of their t_play


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay a recorded session over the live link.")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help="a session JSONL log")
    parser.add_argument("--host", default=None, help="default: every interface")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument("--loop", action="store_true", help="replay again, as a new session")
    parser.add_argument("--no-wait", action="store_true", help="start without waiting for a client")
    parser.add_argument("--seconds", type=float, default=None, help="stop each replay early")
    args = parser.parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
