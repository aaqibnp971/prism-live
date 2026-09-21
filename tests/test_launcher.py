"""Light self-checks for process ownership, restart semantics and booth display routing."""

import asyncio
import json
import time
import urllib.parse
from pathlib import Path

import pytest

from tools import launch


@pytest.fixture
def config(tmp_path):
    return launch.Config(
        browser=Path("edge.exe"),
        python=Path("python.exe"),
        root=tmp_path,
        log_dir=tmp_path / "logs",
        status_dir=tmp_path / "status",
        headless=True,
        console_input="pipe",
    )


def displays():
    return [
        launch.Display(0, "laptop", 0, 0, 1920, 1080, True),
        launch.Display(1, "spectator", -1920, 0, 1920, 1080),
        launch.Display(2, "optional-task", 1920, 0, 2560, 1440),
    ]


def test_two_display_default_tiles_console_and_task(config):
    config.displays = launch.display_roles(displays(), 0, 0, 1)
    assert launch.role_bounds(config, "task") == (0, 0, 1248, 1080)
    assert launch.role_bounds(config, "console") == (1248, 0, 672, 1080)
    assert launch.role_bounds(config, "spectator") == (-1920, 0, 1920, 1080)


def test_three_distinct_displays_use_whole_screens(config):
    config.displays = launch.display_roles(displays(), 0, 2, 1)
    assert launch.role_bounds(config, "task") == (1920, 0, 2560, 1440)
    assert launch.role_bounds(config, "console") == (0, 0, 1920, 1080)


def test_missing_or_overlapping_spectator_is_refused():
    with pytest.raises(ValueError, match="unavailable"):
        launch.display_roles(displays()[:1], 0, 0, 1)
    with pytest.raises(ValueError, match="own display"):
        launch.display_roles(displays(), 0, 0, 0)


def test_commands_have_no_network_control_or_standalone_mode(config):
    command = launch.bridge_command(config, 4)
    assert command[:3] == ["python.exe", "-m", "bridge.server"]
    assert command[command.index("--restart-generation") + 1] == "4"
    assert command[command.index("--console-input") + 1] == "pipe"
    browser = launch.browser_command(config, "task", Path("private-profile"))
    assert "--headless=new" in browser
    parsed = urllib.parse.urlsplit(browser[-1])
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.scheme == "file"
    assert query["ws"] == ["ws://127.0.0.1:8787/live"]
    assert "standalone" not in query
    assert "--one-session" not in command


def test_tiled_task_uses_app_mode_and_scaled_physical_width(config):
    config.headless = False
    config.displays = launch.display_roles(displays(), 0, 0, 1)
    command = launch.browser_command(config, "task", Path("task-private"))
    assert "--kiosk" not in command
    url = command[-1].removeprefix("--app=")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert float(query["screen_width_cm"][0]) == pytest.approx(59.77 * 0.65)
    assert query["tiled"] == ["1"]
    assert query["monitor_width_px"] == ["1920"]
    assert query["monitor_width_cm"] == ["59.77"]
    spectator = launch.browser_command(config, "spectator", Path("spectator-private"))
    assert "--kiosk" in spectator
    assert "--window-position=-1920,0" in spectator


class FakeProcess:
    def __init__(self, pid=123, exit_code=None):
        self.pid = pid
        self.exit_code = exit_code
        self.terminated = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = -1

    kill = terminate

    def wait(self, timeout=None):
        return self.exit_code


class FakeJob:
    def __init__(self, pids=()):
        self.closed = False
        self.owned_pids = set(pids)

    def close(self):
        self.closed = True

    def pids(self):
        return self.owned_pids


def add_child(supervisor, role, *, exit_code=None, launched=None):
    now = time.monotonic() if launched is None else launched
    child = launch.ManagedChild(
        role,
        FakeProcess(len(supervisor.children) + 120, exit_code),
        FakeJob(),
        0,
        now,
        last_healthy=now,
        audio_changed=now,
    )
    supervisor.children[role] = child
    return child


def supervisor(config):
    config.status_dir.mkdir(parents=True)
    return launch.Supervisor(config)


def test_nonzero_exit_restarts_only_dead_child(config):
    async def check():
        sup = supervisor(config)
        dead = add_child(sup, "bridge", exit_code=1)
        task = add_child(sup, "task")
        spectator = add_child(sup, "spectator")
        await sup.restart("bridge", "process exited 1")
        assert dead.job.closed
        assert "bridge" not in sup.children
        assert sup.generations["bridge"] == 1
        assert sup.children["task"] is task
        assert sup.children["spectator"] is spectator
        assert sup.restart_at["bridge"] > time.monotonic()
        assert not task.process.terminated

    asyncio.run(check())


def test_clean_console_exit_is_intentional_shutdown(config):
    async def check():
        sup = supervisor(config)
        add_child(sup, "bridge", exit_code=0)
        assert await sup.step() is False
        assert sup.events[-1]["event"] == "attendant_shutdown"

    asyncio.run(check())


def test_visible_conhost_exit_zero_is_not_assumed_clean(config):
    async def check():
        config.headless = False
        sup = supervisor(config)
        add_child(sup, "bridge", exit_code=0)
        sup.restart_at.update(task=float("inf"), spectator=float("inf"))
        assert await sup.step() is True
        assert sup.generations["bridge"] == 1

    asyncio.run(check())


def test_visible_explicit_quit_marker_is_clean(config):
    async def check():
        config.headless = False
        sup = supervisor(config)
        add_child(sup, "bridge", exit_code=0)
        launch.write_json(
            config.bridge_status,
            {
                "generation": 0,
                "shutdown_requested": True,
                "updated": time.time(),
            },
        )
        assert await sup.step() is False

    asyncio.run(check())


def test_old_quit_marker_cannot_stop_new_visible_launcher(config):
    async def check():
        config.headless = False
        sup = supervisor(config)
        add_child(sup, "bridge", exit_code=0)
        sup.restart_at.update(task=float("inf"), spectator=float("inf"))
        launch.write_json(
            config.bridge_status,
            {
                "generation": 0,
                "shutdown_requested": True,
                "updated": time.time() - 10,
            },
        )
        assert await sup.step() is True
        assert sup.generations["bridge"] == 1

    asyncio.run(check())


def test_bridge_health_rejects_stale_foreign_pid_and_generation(config):
    async def check():
        sup = supervisor(config)
        child = add_child(sup, "bridge")
        valid = dict(
            pid=child.process.pid, ready=True, generation=0, updated=time.time(), audio_frames=100
        )
        for update in ({"pid": 9999}, {"generation": 3}, {"updated": 0}):
            launch.write_json(config.bridge_status, valid | update)
            await sup._health(child, time.monotonic())
            assert not child.ready
        launch.write_json(config.bridge_status, valid)
        await sup._health(child, time.monotonic())
        assert child.ready

    asyncio.run(check())


def test_redirector_child_pid_is_accepted_only_in_owned_job(config):
    async def check():
        sup = supervisor(config)
        child = add_child(sup, "bridge")
        child.job.owned_pids.add(555)
        launch.write_json(
            config.bridge_status,
            dict(
                pid=555,
                ready=True,
                generation=0,
                updated=time.time(),
                audio_frames=100,
            ),
        )
        await sup._health(child, time.monotonic())
        assert child.ready
        snapshot = sup.snapshot()
        assert snapshot["children"]["bridge"]["pid"] == 555
        assert snapshot["children"]["bridge"]["wrapper_pid"] == child.process.pid

    asyncio.run(check())


def test_audio_stall_invalidates_even_fresh_bridge_status(config):
    async def check():
        sup = supervisor(config)
        child = add_child(sup, "bridge")
        child.last_audio_frames = 100
        child.audio_changed = time.monotonic() - 6
        launch.write_json(
            config.bridge_status,
            dict(
                pid=child.process.pid,
                ready=True,
                generation=0,
                updated=time.time(),
                audio_frames=100,
            ),
        )
        await sup._health(child, time.monotonic())
        assert not child.ready

    asyncio.run(check())


def test_unresponsive_bridge_is_restarted(config):
    async def check():
        sup = supervisor(config)
        child = add_child(sup, "bridge", launched=time.monotonic() - 20)
        sup.restart_at.update(task=float("inf"), spectator=float("inf"))
        assert await sup.step() is True
        assert child.job.closed
        assert "bridge" not in sup.children
        assert sup.generations["bridge"] == 1

    asyncio.run(check())


def test_browser_health_probe_only_reads_page(config, monkeypatch):
    async def check():
        sup = supervisor(config)
        child = add_child(sup, "task")

        async def probe(observed, settings):
            assert observed is child
            assert settings is config
            return True, True

        monkeypatch.setattr(launch, "browser_probe", probe)
        await sup._health(child, time.monotonic())
        assert child.ready and child.connected

    asyncio.run(check())


def test_browser_hung_probe_marks_unhealthy(config, monkeypatch):
    async def check():
        sup = supervisor(config)
        child = add_child(sup, "task")

        async def probe(*_):
            raise TimeoutError("renderer stalled")

        monkeypatch.setattr(launch, "browser_probe", probe)
        await sup._health(child, time.monotonic())
        assert not child.ready
        assert child.error == "renderer stalled"

    asyncio.run(check())


def test_snapshot_requires_all_three_healthy_connected_processes(config):
    sup = supervisor(config)
    for role in launch.ROLES:
        child = add_child(sup, role)
        child.ready = child.connected = True
    assert sup.snapshot()["ready"]
    sup.children["task"].connected = False
    assert not sup.snapshot()["ready"]
    assert json.loads(sup.status_path.read_text())["headless"] is True


def test_duplicate_launcher_directory_is_locked(config):
    first = launch.Supervisor(config)
    second = launch.Supervisor(config)
    first._acquire()
    try:
        with pytest.raises(RuntimeError, match="already owns"):
            second._acquire()
    finally:
        asyncio.run(first.close())


def test_status_reader_cannot_crash_supervision_with_windows_sharing_race(tmp_path, monkeypatch):
    path = tmp_path / "launcher.json"
    launch.write_json(path, {"generation": 0})
    original = Path.replace

    def temporarily_locked(*_):
        raise PermissionError("reader has DELETE sharing disabled")

    monkeypatch.setattr(Path, "replace", temporarily_locked)
    launch.write_json(path, {"generation": 1})
    assert launch.read_json(path) == {"generation": 0}
    monkeypatch.setattr(Path, "replace", original)
    launch.write_json(path, {"generation": 2})
    assert launch.read_json(path) == {"generation": 2}


def test_empty_initial_document_does_not_shorten_browser_startup_timeout(config, monkeypatch):
    async def check():
        sup = supervisor(config)
        child = add_child(sup, "task", launched=time.monotonic() - 7)
        sup.restart_at.update(bridge=float("inf"), spectator=float("inf"))

        async def probe(*_):
            return False, False

        monkeypatch.setattr(launch, "browser_probe", probe)
        assert await sup.step() is True
        assert not child.was_ready
        assert not child.job.closed
        assert sup.children["task"] is child

    asyncio.run(check())


def test_no_network_session_controls_in_launcher_source():
    source = Path(launch.__file__).read_text(encoding="utf-8")
    assert "session.start(" not in source
    assert "session.stop(" not in source
    assert '"Input.dispatch' not in source
    assert "--standalone" not in source
