"""bridge/server.py over real sockets on localhost."""

import asyncio
import json
import threading

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from bridge import server as live
from bridge.clock import t_engine_ms
from bridge.contract import decode, encode, validate
from bridge.logging import SessionLog

HELLO = {"type": "hello", "v": 1, "client": "spectator", "build": "test"}
ZERO = {"arousal": 0.0, "valence": 0.0, "cognitive_load": 0.0, "readiness": 0.0}


def idle_state(session, t_engine, seq=1):
    return {
        "type": "state", "v": 1, "session": session, "seq": seq, "t_engine": round(t_engine),
        "t_session": None, "segment": "idle", "segment_elapsed_ms": 0, "segment_nominal_ms": 0,
        "psv": dict(ZERO, valence=0.5), "confidence": ZERO, "authority": ZERO,
        "hr_bpm": None, "hr_base": None,
        "signal": {"contact": True, "rr_accepted_pct": 0.0, "baseline_quality": 0.0},
    }  # fmt: skip


def beat(session, t_play, quality="ok", seq=1):
    return {"type": "beat", "v": 1, "session": session, "seq": seq, "t_play": round(t_play),
            "rr_ms": 800.0, "hr_bpm": 75.0, "quality": quality}  # fmt: skip


def with_server(tmp_path, scenario, **kwargs):
    """Run scenario(server, url) against a LiveServer on a free port; return the log lines.

    The server's clock starts a minute in, so a time in the past is never negative however
    young this test process is.
    """
    kwargs.setdefault("clock", lambda: 60_000 + t_engine_ms())

    async def main():
        log = SessionLog(tmp_path)
        log.start_session()
        async with live.LiveServer(log, host="127.0.0.1", port=0, **kwargs) as server:
            await scenario(server, f"ws://127.0.0.1:{server.port}/live")
        log.close()
        return [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]

    return asyncio.run(main())


async def closed_with(ws):
    with pytest.raises(ConnectionClosed):
        while True:
            await asyncio.wait_for(ws.recv(), 3)
    return ws.close_code, ws.close_reason


def test_hello_then_a_ping_gets_a_pong(tmp_path):
    async def scenario(server, url):
        async with connect(url) as ws:
            await ws.send(encode(HELLO))
            await ws.send(
                encode({"type": "clock", "v": 1, "role": "ping", "t_client_sent": 4021.9})
            )
            pong = decode(await asyncio.wait_for(ws.recv(), 3))
            validate(pong, "out")
            assert pong["role"] == "pong" and pong["t_client_sent"] == 4021.9
            assert abs(pong["t_engine"] - server.clock()) < 1000

    lines = with_server(tmp_path, scenario)
    kinds = [(r["dir"], r.get("msg", {}).get("type") or r.get("event")) for r in lines]
    assert ("in", "hello") in kinds and ("in", "clock") in kinds and ("out", "clock") in kinds
    hello = next(r for r in lines if r.get("msg", {}).get("type") == "hello")
    assert hello["client"] == "c1/spectator"


@pytest.mark.parametrize(
    ("first", "code", "reason"),
    [
        (
            encode({"type": "clock", "v": 1, "role": "ping", "t_client_sent": 1}),
            1008,
            "first message must be hello",
        ),
        (encode(dict(HELLO, v=2)), 1008, "v2"),
        (encode(dict(HELLO, client="phone")), 1008, "client"),
        (encode(dict(HELLO, client="名" * 80)), 1008, "client"),
        ("{not json", 1007, "not JSON"),
        ('{"type":"hello","v":1,"client":"spectator","build":"\\ud800"}', 1007, "surrogate"),
        ("[" * 3000 + "]" * 3000, 1007, "not JSON"),
        (b"\x00\x01", 1003, "text frames only"),
    ],
    ids=[
        "ping first",
        "wrong version",
        "unknown client",
        "long non-ascii client",
        "bad json",
        "lone surrogate",
        "deep nesting",
        "binary",
    ],
)
def test_a_bad_first_message_is_refused_and_logged(tmp_path, first, code, reason):
    async def scenario(server, url):
        async with connect(url) as ws:
            await ws.send(first)
            got_code, got_reason = await closed_with(ws)
            assert got_code == code and reason in got_reason
            assert server.stats.refused == 1

    lines = with_server(tmp_path, scenario)
    assert any(r["dir"] == "in" for r in lines)  # logged as received, before refusing
    (gone,) = [r for r in lines if r.get("event") == "disconnected"]
    assert gone["sent_code"] == code
    assert not any(r.get("event") == "handler_failed" for r in lines)


def test_a_client_that_never_says_hello_is_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "HELLO_TIMEOUT_S", 0.2)

    async def scenario(server, url):
        async with connect(url) as ws:
            assert await closed_with(ws) == (1008, "send hello first")
            assert server.stats.refused == 1

    lines = with_server(tmp_path, scenario)
    (gone,) = [r for r in lines if r.get("event") == "disconnected"]
    assert (gone["sent_code"], gone["sent_reason"]) == (1008, "send hello first")


def test_a_ping_is_logged_even_when_the_client_closes_before_its_pong(tmp_path):
    connections = 20

    async def scenario(server, url):
        for i in range(connections):
            async with connect(url) as ws:
                await ws.send(encode(HELLO))
                await ws.send(encode({"type": "clock", "v": 1, "role": "ping", "t_client_sent": i}))
        await asyncio.sleep(0.3)

    lines = with_server(tmp_path, scenario)
    pings = [r for r in lines if r["dir"] == "in" and r.get("msg", {}).get("type") == "clock"]
    assert len(pings) == connections


def test_every_interface_needs_a_fixed_port(tmp_path):
    with pytest.raises(ValueError, match="explicit host"):
        live.LiveServer(SessionLog(tmp_path), host=None, port=0)


def test_a_second_hello_is_refused(tmp_path):
    async def scenario(server, url):
        async with connect(url) as ws:
            await ws.send(encode(HELLO))
            await ws.send(encode(HELLO))
            assert (await closed_with(ws))[0] == 1008

    with_server(tmp_path, scenario)


def test_the_link_is_only_at_live(tmp_path):
    async def scenario(server, url):
        with pytest.raises(InvalidStatus) as refused:
            async with connect(url.replace("/live", "/other")):
                pass
        assert refused.value.response.status_code == 404

    with_server(tmp_path, scenario)


def test_the_stream_reaches_only_clients_past_hello(tmp_path):
    async def scenario(server, url):
        async with connect(url) as greeted, connect(url) as silent:
            await greeted.send(encode(HELLO))
            await server.wait_for_client()
            assert server.publish(idle_state(server.log.session, server.clock()))
            assert decode(await asyncio.wait_for(greeted.recv(), 3))["type"] == "state"
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(silent.recv(), 0.3)

    with_server(tmp_path, scenario)


def test_a_beat_without_300_ms_of_headroom_is_not_sent(tmp_path):
    async def scenario(server, url):
        async with connect(url) as ws:
            await ws.send(encode(HELLO))
            await server.wait_for_client()
            session = server.log.session
            assert not server.publish(beat(session, server.clock() + 100, seq=1))
            assert server.publish(beat(session, server.clock() - 5000, quality="rejected", seq=2))
            assert server.publish(beat(session, server.clock() + 500, seq=3))
            got = [decode(await asyncio.wait_for(ws.recv(), 3)) for _ in range(2)]
            assert [m["seq"] for m in got] == [2, 3]
            assert server.stats.beats_not_sent == 1

    lines = with_server(tmp_path, scenario)
    assert [r["event"] for r in lines if r["dir"] == "event"].count("beat_not_sent") == 1


def test_a_contract_breaking_publish_is_a_caller_bug(tmp_path):
    async def scenario(server, url):
        bad = idle_state(server.log.session, server.clock())
        bad["confidence"] = dict(ZERO, valence=0.2)
        with pytest.raises(live.ContractError):
            server.publish(bad)

    with_server(tmp_path, scenario)


def test_task_events_reach_the_handler_and_the_log(tmp_path):
    received = []

    async def scenario(server, url):
        async with connect(url) as ws:
            await ws.send(encode(dict(HELLO, client="task-screen")))
            await ws.send(encode({
                "type": "task_event", "v": 1, "session": server.log.session, "t_client": 40530.5,
                "event": "lock", "dwell_ms": 470, "split_interval_ms": 3100, "difficulty": 0.4,
            }))  # fmt: skip
            for _ in range(50):
                if received:
                    break
                await asyncio.sleep(0.02)

    lines = with_server(
        tmp_path,
        scenario,
        on_task_event=lambda msg, client: received.append((msg["event"], client.label)),
    )
    assert received == [("lock", "c1/task-screen")]
    assert any(r.get("msg", {}).get("type") == "task_event" for r in lines)


def test_a_client_too_far_behind_is_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "SLOW_CLIENT_BYTES", -1)  # every client counts as behind

    async def scenario(server, url):
        async with connect(url) as ws:
            await ws.send(encode(HELLO))
            await server.wait_for_client()
            server.publish(idle_state(server.log.session, server.clock()))
            assert (await closed_with(ws))[0] == 1013
            assert server.stats.too_slow == 1

    with_server(tmp_path, scenario)


def test_publish_from_another_thread(tmp_path):
    async def scenario(server, url):
        async with connect(url) as ws:
            await ws.send(encode(HELLO))
            await server.wait_for_client()
            msg = idle_state(server.log.session, server.clock())
            threading.Thread(target=server.publish_threadsafe, args=(msg,)).start()
            assert decode(await asyncio.wait_for(ws.recv(), 3)) == msg

    with_server(tmp_path, scenario)
