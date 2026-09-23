"""Beat rejection is event-local; invalid lifecycle/device faults still stop the bridge."""

import ctypes
import json
import sys
from array import array

import pytest

from bridge.beat_scheduler import BeatEvent
from bridge.engine import RENDER_FN, Shim, ShimError
from bridge.engine_feed import HeartbeatLevel
from bridge.live import LiveLoop
from bridge.logging import SessionLog


def event(t_play=2000, rr_ms=800, quality="ok"):
    return BeatEvent(1, t_play, rr_ms, 60_000 / rr_ms, quality, 1000, 800, 0)


def make_live(tmp_path, sink, publish=None):
    def clock():
        return 1000.0

    log = SessionLog(tmp_path, clock=clock)
    return LiveLoop((), publish or (lambda msg: True), log, clock=clock, beat_sink=sink)


@pytest.mark.parametrize("result", [1, 5])
def test_native_beat_refusal_is_logged_counted_not_retried_and_next_beat_works(tmp_path, result):
    class Sink:
        def __init__(self):
            self.calls = []

        def push_beat(self, *args):
            self.calls.append(args)
            if len(self.calls) == 1:
                raise ShimError("pls_push_beat", result)

    sink = Sink()
    live = make_live(tmp_path, sink)
    try:
        live._publish_beat(event())
        live._publish_beat(event(2800))
        assert len(sink.calls) == 2
        assert live.audio_beats_dropped == 1
        records = [json.loads(line) for line in live.log.path.read_text().splitlines()]
        drops = [r for r in records if r.get("event") == "heartbeat_beat_dropped"]
        assert len(drops) == 1
        assert drops[0]["beat_seq"] == 1
        assert drops[0]["rr_ms"] == 800
        assert drops[0]["dropped"] == 1
        assert drops[0]["reason"] == ShimError("pls_push_beat", result).name
    finally:
        live.log.close()


@pytest.mark.parametrize(
    "error",
    [
        ShimError("pls_push_beat", 2),
        ShimError("pls_push_beat", 3),
        ShimError("pls_push_beat", 4),
        ShimError("pls_push_beat", 6),
        ShimError("other_call", 1),
        RuntimeError("unrelated bug"),
    ],
)
def test_unrelated_audio_faults_are_not_hidden(tmp_path, error):
    class Sink:
        def push_beat(self, *args):
            raise error

    live = make_live(tmp_path, Sink())
    try:
        with pytest.raises(type(error), match=str(error)):
            live._publish_beat(event())
        assert live.audio_beats_dropped == 0
    finally:
        live.log.close()


def test_physically_rejected_and_unsent_beats_never_reach_audio(tmp_path):
    class Sink:
        def push_beat(self, *args):
            pytest.fail("This beat must never be pushed")

    published = []
    live = make_live(tmp_path, Sink(), published.append)
    try:
        live._publish_beat(event(rr_ms=6000, quality="rejected"))
        assert published[0]["quality"] == "rejected"
        live.publish = lambda msg: False
        live._publish_beat(event())
        assert live.audio_beats_dropped == 0
    finally:
        live.log.close()


@pytest.mark.skipif(sys.platform != "win32", reason="committed Windows shim")
def test_real_shim_invalid_and_late_beats_do_not_prevent_future_audio(tmp_path):
    @RENDER_FN
    def silence(core, out, frames):
        ctypes.memset(out, 0, frames * 4)
        return 0

    shim = Shim(silence)
    live = make_live(tmp_path, shim)
    try:
        shim.set_clock_anchor(0, 0)
        shim.set_heartbeat_level(-13, 0)
        live._publish_beat(event(500, 6000))  # Real ABI rejection: no command queued.
        assert live.audio_beats_dropped == 1
        first = array("f", [0]) * 48_000
        shim.render_offline(first)
        assert not any(first)
        live._publish_beat(event(500))  # Valid ABI, but already late on the audio timeline.
        live._publish_beat(event(1500))
        after = array("f", [0]) * 48_000
        shim.render_offline(after)
        stats = shim.stats()
        assert stats.beats_dropped_late == 1
        assert stats.beats_played == 1
        assert stats.render_errors == 0
        assert any(after)
        assert not any(after[:24_000])  # Future heartbeat, never the late one replayed now.
    finally:
        live.log.close()
        shim.close()


@pytest.mark.skipif(sys.platform != "win32", reason="committed Windows shim")
@pytest.mark.parametrize("control_first", [False, True])
def test_native_resumed_beat_is_digital_silence_after_missed_resolve_end(tmp_path, control_first):
    @RENDER_FN
    def silence(core, out, frames):
        ctypes.memset(out, 0, frames * 4)
        return 0

    shim = Shim(silence)
    log = SessionLog(tmp_path)
    log.start_session()
    level = HeartbeatLevel(shim, log)
    try:
        shim.set_clock_anchor(40_000, 0)
        level.on_state(
            dict(
                t_engine=40_000,
                segment="resolve",
                segment_elapsed_ms=40_000,
                segment_nominal_ms=45_000,
            )
        )
        # Native chain keeps advancing; neither state nor level ticks run for ten seconds.
        shim.render_offline(array("f", [0]) * 480_000)
        if control_first:
            level.tick(50_000)
        level.on_state(
            dict(
                t_engine=50_000,
                segment="reset",
                segment_elapsed_ms=5000,
                segment_nominal_ms=20_000,
            )
        )
        level.tick(50_000)
        shim.push_beat(50_500, 800)
        out = array("f", [0]) * 48_000
        shim.render_offline(out)
        assert shim.stats().beats_played == 1  # Counter alone is not proof of audibility.
        assert not any(out)  # The voice is muted, even if session-state recovery ran first.
    finally:
        log.close()
        shim.close()
