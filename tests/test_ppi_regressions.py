"""Self-check fixes from the single three-agent heavy-review round."""

import asyncio
from dataclasses import replace

import pytest

from bridge.beat_scheduler import Interval, PacketResult, Tuning
from bridge.contract import validate
from bridge.live import LiveLoop
from bridge.logging import SessionLog
from bridge.mqtt_source import MqttConfig, PpiDecoder
from bridge.packets import PpiPacket, PpiSample
from bridge.ppi_scheduler import PpiBeatScheduler
from bridge.psv import PsvModel
from bridge.server import _shutdown_runtime
from tests.test_mqtt_source import TOPIC, payload


@pytest.mark.parametrize("ppi,error", [(0, 1), (-1, 1), (65535, 1), (800, -1), (.01, 1)])
def test_bad_numeric_ppi_is_rejected_at_boundary_not_a_loop_exception(ppi, error):
    decoder = PpiDecoder(MqttConfig())
    assert decoder.decode(TOPIC, payload(samples=[{
        "ppi": ppi, "errorEstimate": error, "blockerBit": False,
    }]), 5000) is None
    assert decoder.counters["malformed"] == 1


def test_overlong_rejected_startup_burst_never_emits_negative_wire_time():
    scheduler = PpiBeatScheduler()
    result = scheduler.on_packet(5250, PpiPacket(
        (PpiSample(1000, 1, True),) * 20, "phone", 5000,
    ))
    assert scheduler.stats.rejected == 20
    assert len(result.events) < 20
    for event in result.events:
        validate(event.message("S-20260923-0001"), "out")


def test_start_fences_queued_and_later_arriving_previous_visitor_beats(tmp_path):
    scheduler = PpiBeatScheduler()
    model = PsvModel(scheduler.t)
    published = []
    log = SessionLog(tmp_path)
    loop = LiveLoop(object(), published.append, log, scheduler=scheduler, model=model,
                    clock=lambda: 0)
    try:
        first = PpiPacket((PpiSample(1000, 1, False),) * 5, "phone", 10_000)
        model.on_packet(10_250, scheduler.on_packet(10_250, first))
        assert loop._start_now(10_250, None) is None
        old = replace(first, arrived_ms=20_000)
        model.on_packet(20_250, scheduler.on_packet(20_250, old))
        assert loop.session.stop(21_000)
        loop.session.tick(24_000)
        assert loop._start_now(24_000, None) is None
        assert model.estimate(24_000).hr_bpm is None
        late = replace(first, arrived_ms=25_000)
        result = scheduler.on_packet(25_250, late)
        model.on_packet(25_250, result)
        assert model.estimate(25_250).hr_bpm is None
        for event in scheduler.tick(25_520):
            loop._publish_beat(event)
        assert not [m for m in published if m["type"] == "beat"]
        newer = replace(first, arrived_ms=30_000)
        result = scheduler.on_packet(30_250, newer)
        model.on_packet(30_250, result)
        assert model.estimate(30_250).hr_bpm == 60
        assert all(t >= 24_000 for t, _ in model._trusted)
        assert all(t - scheduler.t.buffer_ms >= 24_000 for t, _, _ in scheduler._pending)
    finally:
        log.close()


def test_cap_rollback_clears_hrv_cached_against_late_complete_baseline():
    model = PsvModel(Tuning.ppi())
    model.start_baseline(10_000)

    def feed(first, last, arrival, jitter):
        intervals = tuple(Interval(t, 1000 + (jitter if t // 1000 % 2 else -jitter),
                                   True, arrival) for t in range(first, last + 1, 1000))
        model.on_packet(arrival, PacketResult(intervals, ()))

    feed(11_000, 55_000, 55_100, 10)
    feed(56_000, 70_000, 70_100, 70)
    assert model.baseline.hrv_complete
    assert model._z_rmssd is not None
    model.close_baseline(66_000)
    assert not model.baseline.hrv_complete
    assert model.baseline.rmssd_base_ms is None
    assert model._z_rmssd is None and not model._z_rmssd_now
    feed(71_000, 76_000, 76_100, 70)
    assert model._z_rmssd is None and not model._z_rmssd_now


def test_empty_stale_result_does_not_refresh_ppi_confidence():
    model = PsvModel(Tuning.ppi())
    model.on_packet(10_000, PacketResult((Interval(8000, 800, True, 10_000),), ()))
    model.on_packet(20_000, PacketResult((), ()))
    assert model._last_arrival == 10_000
    assert model.estimate(20_000).signal.silence_factor == 0


def test_synthetic_poor_quality_capture_is_not_forced_through_the_gate(tmp_path):
    from tests.synthetic_ppi_capture import write_synthetic_capture
    from tools.check_ppi_timing import measure_capture_baselines

    cases = measure_capture_baselines(write_synthetic_capture(tmp_path / "synthetic-capture"))
    assert len(cases) == 5
    assert all(c["phase_at_hold_cap"] == "failed" and c["hr_base_bpm"] is None for c in cases)
    assert all(c["accepted_ms"] < 35_000 and c["problems"] for c in cases)


def test_broker_teardown_failure_cannot_skip_audio_fade_close_or_log_close():
    calls = []

    class Host:
        async def stop(self, *args):
            calls.append("fade")

        def close(self):
            calls.append("audio_closed")

    class Source:
        async def close(self):
            calls.append("source_closed")

    class Broker:
        async def close(self):
            assert calls[:2] == ["fade", "audio_closed"]
            await asyncio.sleep(.01)
            raise RuntimeError("broker failed")

    class Log:
        def close(self):
            calls.append("log_closed")

    with pytest.raises(RuntimeError, match="broker failed"):
        asyncio.run(_shutdown_runtime(Host(), None, None, Log(), Source(), Broker(), True))
    assert calls == ["fade", "audio_closed", "source_closed", "log_closed"]


def test_synthetic_ppi_stall_keeps_original_arrival(monkeypatch):
    from tools import synthetic_rr

    elapsed = [0.0]

    class LoopClock:
        def time(self):
            return elapsed[0]

    async def delayed_sleep(seconds):
        elapsed[0] += seconds + 10  # deliberate scheduler/event-loop stall

    async def run():
        source = synthetic_rr.SyntheticPacketSource(
            synthetic_rr.Profile.from_spec("75:10"), ppi_bursts=True, repeat=False,
        )
        stream = source.__aiter__()
        result = await anext(stream)
        await stream.aclose()
        return result

    monkeypatch.setattr(synthetic_rr.asyncio, "get_running_loop", lambda: LoopClock())
    monkeypatch.setattr(synthetic_rr.asyncio, "sleep", delayed_sleep)
    monkeypatch.setattr(synthetic_rr, "t_engine_ms", lambda: elapsed[0] * 1000)
    result = asyncio.run(run())
    assert elapsed[0] * 1000 - result.arrived_ms == pytest.approx(10_250)
    scheduler = PpiBeatScheduler()
    assert scheduler.on_packet(elapsed[0] * 1000, result).intervals == ()
    assert scheduler.stats.skipped == len(result.samples)
