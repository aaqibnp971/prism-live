"""The recorded fixture, fake_sender's replay, and fake_receiver's view of it."""

import argparse
import asyncio
import json
from datetime import date

import pytest

from bridge.clock_sync import ClockSync
from bridge.contract import MIN_LEAD_MS, SEGMENTS, STATE_INTERVAL_MS, validate
from bridge.logging import SessionLog
from bridge.server import LiveServer
from tools import fake_receiver, fake_sender
from tools.fake_sender import DEFAULT_FIXTURE, load_outbound, rebase
from tools.record_fixture import record


def fixture_records():
    return [json.loads(line) for line in DEFAULT_FIXTURE.read_text(encoding="utf-8").splitlines()]


# --- the fixture ---


def test_the_fixture_is_one_clean_session_that_obeys_the_contract():
    everything = fixture_records()
    assert {r["dir"] for r in everything} == {"out", "event"}  # as the bridge logs a session
    events = [r for r in everything if r["dir"] == "event"]
    assert events[0]["event"] == "session_start" and events[-1]["event"] == "session_end"
    assert events[-1]["outcome"] == "completed"
    records = [r for r in everything if r["dir"] == "out"]
    for r in records:
        validate(r["msg"], "out")
    msgs = [r["msg"] for r in records]
    assert {m["session"] for m in msgs} == {events[0]["session"]}
    beats = [r for r in records if r["msg"]["type"] == "beat"]
    assert len(beats) > 280
    assert all(b["msg"]["quality"] == "ok" for b in beats)
    assert all(b["msg"]["t_play"] - b["t_engine"] >= MIN_LEAD_MS for b in beats)
    for kind in ("beat", "state"):
        seqs = [m["seq"] for m in msgs if m["type"] == kind]
        assert seqs == list(range(1, len(seqs) + 1))


def test_the_fixture_walks_every_segment_and_overruns_regulate():
    states = [r["msg"] for r in fixture_records() if r.get("msg", {}).get("type") == "state"]
    order = []
    for s in states:
        if not order or order[-1] != s["segment"]:
            order.append(s["segment"])
    assert order == list(SEGMENTS)
    regulate = [s for s in states if s["segment"] == "regulate"]
    assert max(s["segment_elapsed_ms"] for s in regulate) > regulate[0]["segment_nominal_ms"]
    gaps = [b["t_engine"] - a["t_engine"] for a, b in zip(states, states[1:], strict=False)]
    assert max(gaps) <= STATE_INTERVAL_MS
    assert all(s["hr_base"] is None for s in states if s["segment"] in ("idle", "baseline"))
    assert all(s["hr_base"] is not None for s in states if s["segment"] in ("load", "regulate"))


def test_the_fixture_is_what_the_recorder_makes_today(tmp_path):
    made = record(tmp_path / "fixture.jsonl", today=date(2026, 9, 13))
    assert made["path"].read_bytes() == DEFAULT_FIXTURE.read_bytes(), (
        "tools/fixtures/synthetic-clean.jsonl no longer matches what the bridge makes of the "
        "synthetic armband. If that change was deliberate, re-record: "
        "python -m tools.record_fixture"
    )


# --- fake_sender ---


def test_rebase_moves_engine_times_only_and_copies():
    beat = {"type": "beat", "session": "S-20260913-0001", "t_play": 1500, "seq": 4}
    state = {"type": "state", "session": "S-20260913-0001", "t_engine": 2000, "t_session": 900}
    moved = rebase(beat, 10_000.4, "S-20260913-0007")
    assert moved == dict(beat, t_play=11_500, session="S-20260913-0007")
    assert beat["t_play"] == 1500
    assert rebase(state, 10_000.4, "S-20260913-0007")["t_session"] == 900
    assert rebase(state, 10_000.4, "S-20260913-0007")["t_engine"] == 12_000


def test_load_outbound_keeps_only_what_the_laptop_streams(tmp_path):
    path = tmp_path / "S-20260913-0001.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"t_engine": 1, "dir": "event", "event": "connected"},
                {"t_engine": 2, "dir": "in", "msg": {"type": "hello"}},
                {"t_engine": 3, "dir": "out", "msg": {"type": "clock"}},
                {"t_engine": 4, "dir": "out", "msg": {"type": "state"}},
                {"t_engine": 5, "dir": "out", "msg": {"type": "beat"}},
            ]
        ),
        encoding="utf-8",
    )
    assert [m["type"] for _, m in load_outbound(path)] == ["state", "beat"]


def test_a_replay_reaches_a_receiver_with_its_headroom_intact(tmp_path):
    records = load_outbound(DEFAULT_FIXTURE)
    replay_s = 4.0
    recorded_headroom = [
        m["t_play"] - t
        for t, m in records
        if m["type"] == "beat" and t - records[0][0] <= replay_s * 1000
    ]

    async def main():
        log = SessionLog(tmp_path)
        log.start_session()
        async with LiveServer(log, host="127.0.0.1", port=0) as server:
            url = f"ws://127.0.0.1:{server.port}/live"
            lines = []
            receiver = asyncio.create_task(fake_receiver.receive(url, replay_s + 1.5, lines.append))
            await server.wait_for_client()
            await asyncio.sleep(0.2)  # the receiver's first clock sample
            result = await fake_sender.replay(server, records, replay_s)
            summary = await receiver
        log.close()
        return result, summary, lines

    (sent, not_sent), summary, lines = asyncio.run(main())
    assert not_sent == 0 and sent > 0
    assert summary.contract_errors == 0 and summary.seq_gaps == 0 and summary.unsynced_beats == 0
    assert any("t_play in" in line for line in lines)
    # Beat by beat, in order: nothing arrives with less headroom than it was recorded with, less
    # a margin for a loaded machine. A beat cannot gain headroom in a replay.
    assert len(summary.headroom_ms) == len(recorded_headroom)
    for received, recorded in zip(summary.headroom_ms, recorded_headroom, strict=True):
        assert received >= recorded - 60, (received, recorded)
    assert min(summary.headroom_ms) >= MIN_LEAD_MS


def test_a_looping_replay_starts_a_new_session_each_time(tmp_path, capsys):
    args = argparse.Namespace(
        fixture=str(DEFAULT_FIXTURE),
        host="127.0.0.1",
        port=0,
        log_dir=str(tmp_path),
        loop=True,
        no_wait=True,
        seconds=1.2,
    )
    with pytest.raises(TimeoutError):
        asyncio.run(asyncio.wait_for(fake_sender.run(args), 4.0))
    sessions = {}
    for path in sorted(tmp_path.glob("S-*.jsonl")):
        msgs = [json.loads(line)["msg"] for line in path.read_text(encoding="utf-8").splitlines()]
        sessions[path.stem] = [m for m in msgs if m["type"] in ("beat", "state")]
    replayed = {s: msgs for s, msgs in sessions.items() if msgs}
    assert len(replayed) >= 2
    for session, msgs in replayed.items():
        assert {m["session"] for m in msgs} == {session}
        assert [m["seq"] for m in msgs if m["type"] == "state"][0] == 1


# --- fake_receiver ---


def test_the_receiver_counts_gaps_per_session_and_leaves_rejected_beats_out():
    sync = ClockSync()
    offset = 50_000.0  # T_engine is ahead of the receiver's clock by this much
    lines = []
    summary = fake_receiver.Summary()
    last_seq = {}

    def show(msg, now):
        fake_receiver._show(json.dumps(msg), now, now, sync, last_seq, summary, lines.append)

    show(
        {"type": "clock", "v": 1, "role": "pong", "t_client_sent": 999.0, "t_engine": 51_000},
        1001.0,
    )
    assert sync.offset == offset

    def beat(session, seq, t_play, quality="ok"):
        return {"type": "beat", "v": 1, "session": session, "seq": seq, "t_play": t_play,
                "rr_ms": 800.0, "hr_bpm": 75.0, "quality": quality}  # fmt: skip

    a, b = "S-20260913-0001", "S-20260913-0002"
    show(beat(a, 1, 52_500), 2000.0)  # 500 ms of headroom
    show(beat(a, 3, 53_400), 3000.0)  # seq 2 went missing
    show(beat(b, 1, 54_400), 4000.0)  # a new session starts again at 1: not a gap
    show(beat(b, 2, 50_000, "rejected"), 5000.0)  # in the past, but never rendered
    assert summary.seq_gaps == 1
    assert summary.qualities == {"ok": 3, "rejected": 1}
    assert summary.headroom_ms == [500.0, 400.0, 400.0]
    assert summary.late == 0
    assert "not rendered" in lines[-1] and "LATE" not in lines[-1]
