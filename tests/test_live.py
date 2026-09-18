"""Prompt 2.9: the synthetic armband through the real asyncio bridge loop."""

import asyncio
import contextlib
import json
import time

import pytest

from bridge import session as machine
from bridge.baseline import Baseline
from bridge.beat_scheduler import PacketResult
from bridge.contract import MIN_LEAD_MS, validate
from bridge.live import LiveLoop
from bridge.logging import SessionLog
from bridge.psv import BaselinePhase, PsvModel
from bridge.session import Timings
from tools.synthetic_rr import Profile, generate


class RealClock:
    """A fresh T_engine origin backed by the real monotonic performance counter."""

    def __init__(self) -> None:
        self.origin = time.perf_counter_ns()

    def __call__(self) -> float:
        return (time.perf_counter_ns() - self.origin) / 1_000_000.0


class SyntheticBurst:
    """Valid packets made by synthetic_rr, then a silent armband."""

    def __init__(self, packets: int = 8) -> None:
        profile = Profile.from_spec("120:12")
        self.payloads = [note.payload for note in generate(profile)][:packets]

    async def __aiter__(self):
        for payload in self.payloads:
            # Give T_engine a positive, increasing arrival time.  Dumping the packets in one
            # event-loop turn would correctly look like duplicated data to the model.
            await asyncio.sleep(0.03)
            yield payload


class FastBaselineModel(PsvModel):
    """Only the 45 s capture is shortened; packet/model wiring remains the real implementation."""

    def start_baseline(self, t_ms: float) -> bool:
        if not super().start_baseline(t_ms):
            return False
        end = t_ms + machine.BASELINE_MS
        self._baseline = Baseline(
            start_ms=t_ms,
            end_ms=end,
            hr_base_bpm=120.0,
            rmssd_base_ms=None,
            ln_rmssd_base=None,
            slope_bpm_per_min=0.0,
            baseline_quality=1.0,
            accepted_ms=machine.BASELINE_MS,
            accepted_intervals=40,
            passed=True,
            problems=(),
            hr_sd_bpm=1.0,
            rmssd_base_differences=0,
        )
        self._baseline_decided_ms = end
        self._phase = BaselinePhase.READY
        self._capture = None
        return True


class FakeFeed:
    def __init__(self, order: list[tuple[str, object]]) -> None:
        self.order = order
        self.source = "body"

    def on_state(self, msg):
        self.order.append(("feed", msg["seq"]))

    def tick(self, now):
        return ()

    def set_source(self, source, *, t_engine=None):
        self.source = source
        self.order.append(("source", source))


class FakeGain:
    def __init__(self, order: list[tuple[str, object]]) -> None:
        self.order = order

    def on_state(self, msg):
        self.order.append(("gain", msg["seq"]))


class FakeHeartbeat(FakeGain):
    def on_state(self, msg):
        self.order.append(("heartbeat", msg["seq"]))

    def tick(self, now):
        return None


class FakeBeatSink:
    def __init__(self) -> None:
        self.beats = []

    def push_beat(self, t_play_ms, rr_ms, quality=0):
        self.beats.append((t_play_ms, rr_ms, quality))


def short_session(monkeypatch) -> Timings:
    # Session's production constants remain covered by test_session.py.  This integration test
    # keeps a real clock and real sleeps while shortening only its segment deadlines.
    monkeypatch.setattr(machine, "BASELINE_MS", 200.0)
    monkeypatch.setattr(machine, "WINDOW_MS", 200.0)
    return Timings(
        hold_ms=50.0,
        hold_to_end=True,
        load_ms=200.0,
        regulate_ms=300.0,
        extension_ms=0.0,
        resolve_ms=150.0,
        reset_ms=150.0,
        stopped_reset_ms=100.0,
    )


async def stop_task(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_a_whole_session_runs_on_the_real_loop_and_measures_its_timing(tmp_path, monkeypatch):
    timings = short_session(monkeypatch)

    async def scenario():
        clock = RealClock()
        log = SessionLog(tmp_path, clock=clock)
        sent, order = [], []
        feed, sink = FakeFeed(order), FakeBeatSink()
        gain, heartbeat = FakeGain(order), FakeHeartbeat(order)

        def publish(msg):
            validate(msg, "out")
            if msg["type"] == "beat" and msg["quality"] != "rejected":
                assert msg["t_play"] - clock() >= MIN_LEAD_MS
            order.append(("publish", (msg["type"], msg["seq"])))
            sent.append(msg)
            log.message("out", msg, t_engine=clock())
            return True

        live = LiveLoop(
            SyntheticBurst(),
            publish,
            log,
            clock=clock,
            tick_ms=20.0,
            timings=timings,
            model=FastBaselineModel(),
            psv_feed=feed,
            session_gain=gain,
            heartbeat=heartbeat,
            beat_sink=sink,
        )
        task = asyncio.create_task(live.run())
        await asyncio.sleep(0)
        await asyncio.wait_for(live.wait_for_signal(), 1.0)
        session = live.session.session
        assert await live.attendant_start() is None
        live.set_psv_source("pose")
        metrics = await asyncio.wait_for(live.wait_for_completion(session), 3.0)
        await stop_task(task)
        log.close()
        return live, sent, order, sink, metrics

    live, sent, order, sink, metrics = asyncio.run(scenario())
    states = [msg for msg in sent if msg["type"] == "state"]
    first_session = [msg for msg in states if msg["session"] == metrics.session]
    assert {msg["segment"] for msg in first_session} == {
        "idle",
        "baseline",
        "load",
        "regulate",
        "resolve",
        "reset",
    }
    assert states[-1]["segment"] == "idle"
    assert states[-1]["session"] != metrics.session
    assert live.session.session == states[-1]["session"]
    assert all(msg["session"] == metrics.session for msg in sent if msg["type"] == "beat")
    assert sink.beats

    tick = metrics.tick_interval_ms
    lead = metrics.beat_lead_ms
    assert tick.count > 20
    assert tick.minimum >= 20.0
    assert 20.0 <= tick.median <= 100.0
    assert 20.0 <= tick.p95 <= 100.0
    assert tick.maximum <= 100.0
    # The sink can also receive scheduled beats while idle before start; the measured window is
    # deliberately only start firing through the end of reset.
    assert len(sink.beats) >= lead.count > 0
    assert lead.minimum >= MIN_LEAD_MS

    # Each state reaches all three engine controls on this loop before network publication.
    first_seq = first_session[0]["seq"]
    first = [item for item in order if item[1] == first_seq or item[1] == ("state", first_seq)]
    assert first[:4] == [
        ("feed", first_seq),
        ("gain", first_seq),
        ("heartbeat", first_seq),
        ("publish", ("state", first_seq)),
    ]
    assert ("source", "pose") in order


def test_double_press_and_stop_are_local_session_controls(tmp_path, monkeypatch):
    timings = short_session(monkeypatch)

    async def scenario():
        clock, log = RealClock(), SessionLog(tmp_path)
        live = LiveLoop(
            SyntheticBurst(),
            lambda msg: True,
            log,
            clock=clock,
            tick_ms=20.0,
            timings=timings,
            model=FastBaselineModel(),
        )
        task = asyncio.create_task(live.run())
        await asyncio.sleep(0)
        await asyncio.wait_for(live.wait_for_signal(), 1.0)
        session = live.session.session
        first = await live.attendant_start()
        second = await live.attendant_start()
        stopped = live.attendant_stop()
        await asyncio.wait_for(live.wait_for_completion(session), 1.0)
        await stop_task(task)
        log.close()
        return first, second, stopped, log.path

    first, second, stopped, last_path = asyncio.run(scenario())
    assert first is None and second == "running" and stopped
    records = []
    for path in sorted(tmp_path.glob("S-*.jsonl")):
        records += [json.loads(line) for line in path.read_text().splitlines()]
    assert any(r.get("event") == "start_refused" and r["reason"] == "running" for r in records)
    assert last_path.exists()


def test_a_source_that_goes_silent_still_reaches_the_baseline_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(machine, "BASELINE_MS", 200.0)
    timings = Timings(
        hold_ms=100.0,
        hold_to_end=True,
        load_ms=1_000.0,
        regulate_ms=20_000.0,
        extension_ms=0.0,
        resolve_ms=100.0,
        reset_ms=100.0,
        stopped_reset_ms=100.0,
    )

    async def scenario():
        clock, log, sent = RealClock(), SessionLog(tmp_path), []
        live = LiveLoop(
            SyntheticBurst(), sent.append, log, clock=clock, tick_ms=20.0, timings=timings
        )
        task = asyncio.create_task(live.run())
        await asyncio.sleep(0)
        await asyncio.wait_for(live.wait_for_signal(), 1.0)
        assert await live.attendant_start() is None
        while live.session.segment == "baseline":
            await asyncio.sleep(0.02)
        await stop_task(task)
        log.close()
        return live.session.segment, log

    segment, _ = asyncio.run(scenario())
    assert segment == "load"
    records = []
    for path in tmp_path.glob("S-*.jsonl"):
        records += [json.loads(line) for line in path.read_text().splitlines()]
    fired = next(r for r in records if r.get("event") == "attendant_start_fired")
    baseline = next(
        r for r in records if r.get("event") == "segment" and r["segment"] == "baseline"
    )
    load = next(r for r in records if r.get("event") == "segment" and r["segment"] == "load")
    assert load["reason"] == "baseline_degraded"
    assert fired["t_engine"] == pytest.approx(baseline["at_ms"], abs=0.1)
    assert load["at_ms"] == pytest.approx(baseline["at_ms"] + 300.0, abs=0.001)


def test_restart_is_idle_on_a_fresh_clock_and_never_reuses_the_old_session(tmp_path):
    async def one(clock, sent, *, start):
        log = SessionLog(tmp_path, clock=clock)
        live = LiveLoop(SyntheticBurst(), sent.append, log, clock=clock, tick_ms=20.0)
        task = asyncio.create_task(live.run())
        await asyncio.sleep(0)
        if start:
            await asyncio.wait_for(live.wait_for_signal(), 1.0)
            assert await live.attendant_start() is None
            await asyncio.sleep(0.05)
        else:
            while not sent:
                await asyncio.sleep(0.01)
        await stop_task(task)
        session = live.session.session
        log.close()
        return session

    first_sent, second_sent = [], []
    first = asyncio.run(one(RealClock(), first_sent, start=True))
    second_clock = RealClock()
    second = asyncio.run(one(second_clock, second_sent, start=False))
    assert first != second
    assert second_sent[0]["type"] == "state" and second_sent[0]["segment"] == "idle"
    assert second_sent[0]["seq"] == 1 and second_sent[0]["t_engine"] < 100
    assert {msg["session"] for msg in second_sent} == {second}
    assert first not in {msg["session"] for msg in second_sent}


def test_each_packet_reaches_scheduler_before_model_and_only_once(tmp_path):
    calls = []

    class OnePacket:
        async def __aiter__(self):
            yield b"packet"

    class Scheduler:
        def on_packet(self, now, payload):
            calls.append(("scheduler", payload))
            return PacketResult((), ())

        def tick(self, now):
            return []

    class Model(PsvModel):
        def __init__(self, delivered):
            super().__init__()
            self.delivered = delivered

        def on_packet(self, now, result):
            calls.append(("model", result))
            self.delivered.set()

    async def scenario():
        delivered = asyncio.Event()
        clock, log = RealClock(), SessionLog(tmp_path)
        live = LiveLoop(
            OnePacket(),
            lambda msg: True,
            log,
            clock=clock,
            scheduler=Scheduler(),
            model=Model(delivered),
        )
        task = asyncio.create_task(live.run())
        await asyncio.wait_for(delivered.wait(), 1.0)
        await stop_task(task)
        log.close()

    asyncio.run(scenario())
    assert calls == [("scheduler", b"packet"), ("model", PacketResult((), ()))]
