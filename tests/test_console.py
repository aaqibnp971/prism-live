"""3.6's local button, countdown, honest warnings and restart screen (light review)."""

import asyncio
import contextlib
import json
import time
from types import SimpleNamespace

import pytest

from bridge.console import (
    AttendantConsole,
    ConsoleLog,
    render,
    run_console,
    screen_lines,
    write_status,
)
from bridge.live import LiveLoop, LiveLoopError
from bridge.phase import PhaseTracker, StartAlignment
from bridge.session import Schedule
from tools.synthetic_rr import Profile, generate


class StubBridge:
    def __init__(self, refusal=None):
        self.session = SimpleNamespace(
            segment="idle",
            session="S-20260921-0001",
            signal_lost=False,
            schedule=Schedule("idle", 0, None, None),
        )
        self.clock = lambda: 1000
        self.start_alignment = StartAlignment(0, 480_000)
        self.phase = SimpleNamespace(frame=lambda: 240_000)
        self.log = SimpleNamespace(baseline_notice="")
        self.psv_feed = SimpleNamespace(source="body")
        self.fire = asyncio.Event()
        self.refusal = refusal
        self.starts = self.stops = self.cancels = 0

    async def attendant_start(self):
        try:
            await self.fire.wait()
        except asyncio.CancelledError:
            self.cancels += 1
            raise
        self.starts += 1
        if self.refusal == "bad_time":
            raise LiveLoopError("bad_time")
        if self.refusal is None:
            self.session.segment = "baseline"
        return self.refusal

    def attendant_stop(self):
        self.stops += 1
        self.session.segment = "reset"
        return True


@pytest.mark.parametrize(
    "status, expected",
    [
        ("connecting", "NO PPI YET"),
        ("waiting_for_ppi", "WAITING FOR PPI"),
        ("flowing", "does not confirm the armband is worn"),
        ("stale", "DATA NOT FLOWING"),
        ("disconnected", "MQTT DISCONNECTED"),
    ],
)
def test_mqtt_console_labels_warmup_and_flow_without_inferring_wear(status, expected):
    bridge = StubBridge()
    bridge.packet_source = SimpleNamespace(status=status)
    view = AttendantConsole(bridge).snapshot()
    text = "\n".join(screen_lines(view, columns=160))
    assert expected in text
    assert "BUFFERED PLAYBACK" in text
    assert "SYNTHETIC" not in text
    assert view["action"] == "ARM START"  # host still decides any refusal at firing


def test_one_button_arms_cancels_then_rearms_starts_stops_and_waits():
    async def scenario():
        bridge = StubBridge()
        ui = AttendantConsole(bridge)
        assert ui.snapshot()["action"] == "ARM START"
        await ui.press()
        assert ui.armed and ui.snapshot()["countdown_s"] == 5
        assert ui.snapshot()["action"] == "CANCEL COUNTDOWN"
        assert bridge.session.segment == "idle"
        await ui.press()
        assert not ui.armed and bridge.starts == 0 and bridge.cancels == 1
        await ui.press()
        bridge.fire.set()
        await ui.start_task
        assert ui.snapshot()["action"] == "STOP - 3 SECOND RESET"
        await ui.press()
        assert bridge.stops == 1 and ui.snapshot()["state"] == "RESET"
        await ui.press()
        assert bridge.stops == 1 and bridge.starts == 1
        assert ui.snapshot()["action"] == "WAIT FOR RESET"

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["running", "resetting", "no_signal", "bad_time"])
def test_all_start_refusals_are_visible_and_disarm(reason):
    async def scenario():
        bridge = StubBridge(reason)
        ui = AttendantConsole(bridge)
        bridge.fire.set()
        await ui.press()
        await ui.start_task
        assert not ui.armed
        assert reason in ui.snapshot()["notice"]
        assert isinstance(ui.fault, LiveLoopError) == (reason == "bad_time")
        assert bridge.stops == 0

    asyncio.run(scenario())


def test_cancelled_console_shutdown_never_fires_later():
    async def scenario():
        bridge, ui = StubBridge(), None
        ui = AttendantConsole(bridge)
        await ui.press()
        await ui.close()
        bridge.fire.set()
        await asyncio.sleep(0)
        assert bridge.starts == 0

    asyncio.run(scenario())


def test_restart_notice_clears_only_when_new_visit_really_starts():
    async def scenario():
        bridge = StubBridge()
        ui = AttendantConsole(bridge, generation=2)
        await ui.press()
        await ui.press()
        assert ui.snapshot()["restart_notice"]
        await ui.press()
        bridge.fire.set()
        await ui.start_task
        assert ui.snapshot()["restart_notice"] == ""

    asyncio.run(scenario())


def test_restart_notice_and_whole_pane_signal_alarm_do_not_stop_session():
    bridge = StubBridge()
    bridge.session.segment = "load"
    bridge.session.signal_lost = True
    view = AttendantConsole(bridge, generation=1).snapshot()
    rendered = render(view, 52, 48)
    assert "\x1b[41;97m\x1b[2J" in rendered
    assert "SIGNAL LOST" in rendered and "###" in rendered
    assert "VISITOR MUST START AGAIN" in " ".join(screen_lines(view))
    assert "STOP - 3 SECOND RESET" in rendered
    assert bridge.stops == 0 and bridge.session.segment == "load"


def test_short_terminal_keeps_action_and_restart_warning():
    bridge = StubBridge()
    bridge.session.signal_lost = True
    text = render(AttendantConsole(bridge, generation=1).snapshot(), 50, 25)
    assert "VISITOR MUST START AGAIN" in text
    assert "NEXT PRESS: ARM START" in text


def test_failed_gate_never_uses_slope_quality_as_success_and_next_baseline_clears(tmp_path):
    log = ConsoleLog(tmp_path)
    try:
        log.event("baseline_end", baseline_quality=1.0, outcome="failed", problems=["contact"])
        assert log.baseline_notice == "BASELINE: FAILED - contact"
        log.event("regulate_result", outcome="regulated", drop_bpm=37)
        assert log.baseline_notice == "BASELINE: FAILED - contact"
        log.event("segment", segment="baseline")
        assert log.baseline_notice == ""
    finally:
        log.close()


def test_console_never_reads_regulate_result_or_offers_a_close_verdict():
    class NoVerdict:
        segment = "resolve"
        session = "S-20260921-0001"
        signal_lost = False
        schedule = Schedule("resolve", 1000, 46000, 46000)

        @property
        def regulate_result(self):
            raise AssertionError("The console must not read regulate_result")

    bridge = StubBridge()
    bridge.session = NoVerdict()
    text = "\n".join(screen_lines(AttendantConsole(bridge).snapshot()))
    assert "peaked-at minus left-at" in text
    assert "drop_bpm" not in text


def test_supervisor_status_is_a_file_and_replaces_stale_generation(tmp_path):
    path = tmp_path / "status.json"
    write_status(path, {"ready": True, "generation": 0})
    write_status(path, {"ready": False, "generation": 1})
    assert json.loads(path.read_text()) == {"ready": False, "generation": 1}
    assert not path.with_suffix(".tmp").exists()


def test_status_reader_collision_does_not_crash_bridge(tmp_path, monkeypatch):
    from pathlib import Path

    def denied(*args):
        raise PermissionError("another process is reading")

    monkeypatch.setattr(Path, "replace", denied)
    write_status(tmp_path / "status.json", {"ready": True})


def test_quit_retries_its_final_marker_after_a_transient_reader_collision(tmp_path, monkeypatch):
    from bridge import console as module

    writes = []

    class QuitKeys:
        def poll(self):
            return ["q"]

        def close(self):
            pass

    def collided_once(path, value):
        writes.append(value)
        return len(writes) > 1

    monkeypatch.setattr(module, "PipeKeys", QuitKeys)
    monkeypatch.setattr(module, "write_status", collided_once)
    asyncio.run(run_console(StubBridge(), input_mode="pipe", status_file=tmp_path / "status.json"))
    assert len(writes) == 2 and all(value["shutdown_requested"] for value in writes)


def test_real_loop_cancel_logs_wait_and_never_starts_then_stop_is_exactly_three_seconds(tmp_path):
    class Burst:
        async def __aiter__(self):
            for note in list(generate(Profile.from_spec("120:12")))[:8]:
                await asyncio.sleep(0.03)
                yield note.payload

    async def scenario():
        origin = time.perf_counter()
        log = ConsoleLog(tmp_path)
        bridge = LiveLoop(
            Burst(),
            lambda msg: True,
            log,
            clock=lambda: (time.perf_counter() - origin) * 1000,
            phase=PhaseTracker(SimpleNamespace(frames_rendered=lambda: 0), block_frames=256),
        )
        runtime = asyncio.create_task(bridge.run())
        await asyncio.sleep(0)
        ui = AttendantConsole(bridge)
        try:
            await asyncio.wait_for(bridge.wait_for_signal(), 1)
            await ui.press()
            assert bridge.start_alignment is not None
            await ui.press()
            assert bridge.start_alignment is None and bridge.session.segment == "idle"
            bridge.phase = None  # remove only the alignment wait for the rest of this test
            await ui.press()
            await ui.start_task
            assert bridge.session.segment == "baseline"
            await ui.press()
            schedule = bridge.session.schedule
            assert schedule.segment == "reset"
            assert schedule.ends_at_latest_ms - schedule.started_ms == 3000
            bridge.session.tick(schedule.ends_at_latest_ms)
            assert bridge.session.segment == "idle"
        finally:
            await ui.close()
            runtime.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runtime
            log.close()
        records = [
            json.loads(line)
            for path in tmp_path.glob("*.jsonl")
            for line in path.read_text().splitlines()
        ]
        events = [r["event"] for r in records if r.get("dir") == "event"]
        assert events.count("attendant_start_pressed") == 2
        assert events.count("attendant_start_armed") == 2
        assert events.count("attendant_start_cancelled") == 1
        assert events.count("attendant_start_fired") == 1
        assert events.index("attendant_start_cancelled") < events.index("attendant_start_fired")

    asyncio.run(scenario())


def test_final_alignment_window_yields_so_a_cancel_can_interrupt(tmp_path):
    async def scenario():
        log = ConsoleLog(tmp_path)
        bridge = LiveLoop(
            None,
            lambda msg: True,
            log,
            phase=PhaseTracker(SimpleNamespace(frames_rendered=lambda: 0), 256),
        )
        task = asyncio.create_task(bridge._wait_for_frame(240))  # final 5 ms window
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        log.close()

    asyncio.run(scenario())
