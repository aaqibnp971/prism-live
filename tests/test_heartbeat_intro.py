"""First measured-beat baseline intro; all inputs are synthetic, never a person's recording."""

import math
from types import SimpleNamespace

import numpy as np
import pytest
from test_live_stalls import event, make_live

from bridge.engine import RENDER_FN, Shim, ShimError
from bridge.engine_feed import (
    HEARTBEAT_BASELINE_RAMP_MS,
    HEARTBEAT_CHAIN_LATENCY_MS,
    HEARTBEAT_RESTART_RAMP_MS,
    HeartbeatLevel,
)


class Log:
    def __init__(self):
        self.rows = []

    def event(self, name, **fields):
        self.rows.append(dict(event=name, **fields))


class Sink:
    def __init__(self):
        self.commands = []

    def set_heartbeat_level(self, level, ramp):
        self.commands.append((level, ramp))


def state(now=0, segment="baseline", elapsed=None, session="visitor-1"):
    return dict(
        session=session,
        t_engine=now,
        segment=segment,
        segment_elapsed_ms=now if elapsed is None else elapsed,
        segment_nominal_ms=56_000,
        hr_bpm=None,
        hr_base=None,
    )


def beat(at, quality="ok", session="visitor-1"):
    return dict(type="beat", session=session, seq=1, t_play=at, rr_ms=800, quality=quality)


@pytest.mark.parametrize("first", [500.0, 12_575.0, 30_000.0])
def test_intro_waits_for_first_play_not_session_start_or_publication(first):
    sink, log = Sink(), Log()
    controller = HeartbeatLevel(sink, log)
    assert controller.on_state(state()) == ((-18.0, 0.0),)
    assert controller.tick(first - 400) is None
    assert controller.on_state(state(first - 400)) is None
    assert controller.on_beat(beat(first), t_engine=first - 300)
    assert sink.commands == [(-18.0, 0.0)]
    assert controller.on_beat(beat(first + 800), t_engine=first - 200) is False
    due = first - HEARTBEAT_CHAIN_LATENCY_MS
    assert controller.tick(due - 0.001) is None
    assert controller.tick(due) == pytest.approx((-13.0, 12_000.0))
    assert controller.tick(due + 12_000) is None
    assert controller.on_state(state(due + 12_000)) is None
    logged = [r for r in log.rows if r["event"] == "heartbeat_baseline_first_beat"]
    assert len(logged) == 1 and logged[0]["t_play"] == first
    assert logged[0]["ramp_end_t_play"] == first + 12_000


@pytest.mark.parametrize(
    "candidate",
    [
        beat(13_000, "rejected"),
        beat(13_000, "interpolated"),
        beat(13_000, session="previous-visitor"),
        beat(500),
        beat(math.nan),
    ],
)
def test_ineligible_beats_do_not_start_the_intro(candidate):
    sink = Sink()
    controller = HeartbeatLevel(sink, Log())
    controller.on_state(state())
    assert controller.on_beat(candidate, t_engine=1_000) is False
    assert controller.tick(30_000) is None
    assert sink.commands == [(-18.0, 0.0)]
    assert controller.on_beat(beat(31_000), t_engine=30_500)


@pytest.mark.parametrize("segment", ["load", "reset", "idle"])
def test_leaving_baseline_cancels_the_pending_intro(segment):
    controller = HeartbeatLevel(Sink(), Log())
    controller.on_state(state())
    controller.on_beat(beat(13_000), t_engine=12_500)
    controller.on_state(state(12_600, segment, elapsed=0))
    assert controller.tick(13_000) is None
    assert controller.on_beat(beat(14_000), t_engine=13_500) is False


@pytest.mark.parametrize("session", ["visitor-1", "visitor-2"])
def test_new_baseline_rearms_even_without_an_intervening_idle(session):
    controller = HeartbeatLevel(Sink(), Log())
    controller.on_state(state())
    controller.on_beat(beat(13_000), t_engine=12_500)
    controller.tick(13_000)
    controller.on_state(state(40_000, "reset", elapsed=0))
    controller.on_state(state(43_000, elapsed=0, session=session))
    assert controller.tick(55_000) is None
    assert controller.on_beat(beat(56_000, session=session), t_engine=55_500)
    assert controller.tick(56_000 - HEARTBEAT_CHAIN_LATENCY_MS) == pytest.approx((-13.0, 12_000.0))


def test_session_id_change_cancels_old_intro_even_when_segment_stays_baseline():
    controller = HeartbeatLevel(Sink(), Log())
    controller.on_state(state())
    controller.on_beat(beat(13_000), t_engine=12_500)
    controller.on_state(state(12_600, elapsed=0, session="visitor-2"))
    assert controller.tick(13_000) is None
    assert controller.on_beat(beat(13_100), t_engine=12_700) is False
    assert controller.on_beat(beat(25_000, session="visitor-2"), t_engine=24_500)


def test_stop_latch_and_restart_before_first_beat_do_not_spend_intro_in_silence():
    sink = Sink()
    controller = HeartbeatLevel(sink, Log())
    controller.on_state(state())
    controller.fade_out(t_engine=5_000)
    assert not controller.on_beat(beat(13_000), t_engine=12_500)
    assert controller.tick(20_000) is None
    controller.resume()
    assert controller.on_state(state(20_000)) == ((-18.0, HEARTBEAT_RESTART_RAMP_MS),)
    assert controller.tick(30_000) is None
    assert controller.on_beat(beat(31_000), t_engine=30_500)
    assert controller.tick(31_000 - HEARTBEAT_CHAIN_LATENCY_MS) == pytest.approx((-13.0, 12_000.0))


def test_resume_same_session_keeps_the_beat_origin_not_baseline_elapsed():
    controller = HeartbeatLevel(Sink(), Log())
    controller.on_state(state())
    first = 13_000 + HEARTBEAT_CHAIN_LATENCY_MS
    controller.on_beat(beat(first), t_engine=12_500)
    controller.tick(13_000)
    controller.fade_out(0.0)
    controller.resume()
    assert controller.on_state(state(19_000)) == ((-15.5, HEARTBEAT_RESTART_RAMP_MS),)
    assert controller.tick(19_100) == (-13.0, 5_900.0)


def test_late_control_tick_shortens_remaining_ramp_without_replaying_any_beats():
    controller = HeartbeatLevel(Sink(), Log())
    controller.on_state(state())
    controller.on_beat(beat(13_000), t_engine=12_500)
    due = 13_000 - HEARTBEAT_CHAIN_LATENCY_MS
    assert controller.tick(due + 500) == pytest.approx((-13.0, 11_500.0))


@pytest.mark.parametrize("refusal", [1, 5])
def test_live_refused_beat_does_not_anchor_intro_but_next_success_does(tmp_path, refusal):
    class BeatSink(Sink):
        calls = 0

        def push_beat(self, *args):
            self.calls += 1
            if self.calls == 1:
                raise ShimError("pls_push_beat", refusal)

    sink = BeatSink()
    live = make_live(tmp_path, sink)
    controller = HeartbeatLevel(sink, Log())
    live.heartbeat = controller
    controller.on_state(state(session=live.session.session))
    try:
        live._publish_beat(event(2_000))
        assert controller.tick(2_000) is None
        live._publish_beat(event(2_800))
        assert controller.tick(2_800 - HEARTBEAT_CHAIN_LATENCY_MS) == (-13.0, 12_000.0)
    finally:
        live.log.close()


@pytest.mark.parametrize("mode", ["rejected", "unpublished", "no_audio", "idle_ppi"])
def test_live_skipped_beats_never_notify_intro(tmp_path, mode):
    sink = SimpleNamespace(push_beat=lambda *args: pytest.fail("must not push this beat"))
    live = make_live(tmp_path, sink)
    live.heartbeat = SimpleNamespace(on_beat=lambda *args, **kw: pytest.fail("must not anchor"))
    if mode == "unpublished":
        live.publish = lambda msg: False
    elif mode == "no_audio":
        live.beat_sink = None
    elif mode == "idle_ppi":
        live.scheduler = SimpleNamespace(t=SimpleNamespace(measured_ppi=True))
    try:
        live._publish_beat(event(quality="rejected" if mode == "rejected" else "ok"))
    finally:
        live.log.close()


@pytest.mark.parametrize("bpm", [45, 95, 180])
def test_native_output_intro_is_relative_to_first_beat_with_same_samples_and_timing(bpm):
    """Compare samplewise with a constant-level voice through the actual unchanged shim."""
    import ctypes
    import sys

    if sys.platform != "win32":
        pytest.skip("committed Windows shim")

    @RENDER_FN
    def silence(core, out, frames):
        ctypes.memset(out, 0, frames * 4)
        return 0

    actual, reference = Shim(silence), Shim(silence)
    first = 13_000.0
    rate = 48_000
    due = first - HEARTBEAT_CHAIN_LATENCY_MS
    stop_ms = first + 13_000
    count = round(stop_ms * 48)
    output = [np.empty(count, dtype=np.float32) for _ in range(2)]
    try:
        for shim in (actual, reference):
            shim.set_clock_anchor(0, 0)
        controller = HeartbeatLevel(actual, Log())
        controller.on_state(state())
        reference.set_heartbeat_level(-13.0, 0.0)
        rr = 60_000 / bpm
        beats = np.arange(first, stop_ms - 300, rr)
        for at in beats:
            for shim in (actual, reference):
                shim.push_beat(float(at), rr)
        controller.on_beat(beat(first), t_engine=first - 500)
        split = round(due * 48)
        for start, end in ((0, split), (split, count)):
            if start == split:
                assert controller.tick(due) == pytest.approx((-13.0, HEARTBEAT_BASELINE_RAMP_MS))
            for shim, samples in zip((actual, reference), output, strict=True):
                for index in range(start, end, 480):
                    shim.render_offline(samples[index : min(index + 480, end)])
        ramped, held = output
        onset = round(first * 48)
        assert np.count_nonzero(ramped[:onset]) == 0
        assert np.array_equal(ramped == 0, held == 0)
        frame = np.arange(count)
        fraction = np.clip((frame - onset + 1) / (HEARTBEAT_BASELINE_RAMP_MS * 48), 0, 1)
        expected = 10 ** ((-5 + 5 * fraction) / 20)
        audible = np.abs(held) > 1e-6
        np.testing.assert_allclose(ramped[audible] / held[audible], expected[audible], atol=3e-6)
        assert np.array_equal(ramped[onset + 12 * rate :], held[onset + 12 * rate :])
        for shim in (actual, reference):
            stats = shim.stats()
            assert stats.beats_played == len(beats)
            assert stats.beats_dropped_late == stats.beats_dropped_full == stats.render_errors == 0
            assert stats.limiter_min_gain == 1
    finally:
        actual.close()
        reference.close()
