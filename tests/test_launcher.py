"""Light self-checks for process ownership, restart semantics and booth display routing."""

import asyncio
import json
import sys
import threading
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
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--task-event-client") + 1] == "task-screen"
    assert "--console-fullscreen" not in command
    assert config.roles == ("bridge", "task", "spectator")
    browser = launch.browser_command(config, "task", Path("private-profile"))
    assert "--headless=new" in browser
    parsed = urllib.parse.urlsplit(browser[-1])
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.scheme == "file"
    assert query["ws"] == ["ws://127.0.0.1:8787/live"]
    assert "standalone" not in query
    assert "--one-session" not in command


def test_booth_switches_layout_network_and_producer_together(config):
    config.booth = True
    config.lan_ip = "192.168.8.20"
    config.headless = False
    config.console_input = "terminal"
    config.displays = launch.display_roles(displays(), 0, None, 1)
    assert config.roles == ("bridge", "spectator")
    assert not config.browser_task
    assert "task" not in config.displays
    assert launch.role_bounds(config, "console") == (0, 0, 1920, 1080)
    assert launch.role_bounds(config, "spectator") == (-1920, 0, 1920, 1080)
    for generation in (0, 1):
        command = launch.bridge_command(config, generation)
        assert command[command.index("--host") + 1] == "192.168.8.20"
        assert command[command.index("--task-event-client") + 1] == "quest"
        assert "--console-fullscreen" in command
    command = launch.browser_command(config, "spectator", Path("spectator-private"))
    assert "--kiosk" in command
    url = command[command.index("--kiosk") + 1]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["ws"] == ["ws://192.168.8.20:8787/live"]
    with pytest.raises(ValueError, match="not enabled"):
        launch.browser_command(config, "task", Path("must-not-open"))
    with pytest.raises(ValueError, match="not enabled"):
        launch.Supervisor(config).spawn("task")
    config.headless = True
    assert "--console-fullscreen" not in launch.bridge_command(config, 0)


def test_lan_discovery_keeps_only_local_private_ipv4(monkeypatch):
    addresses = [
        "127.0.0.1",
        "169.254.1.2",
        "8.8.8.8",
        "0.0.0.0",
        "192.168.8.20",
        "192.168.8.20",
        "10.1.2.3",
        "172.16.2.3",
        "172.32.2.3",
    ]

    def local_addresses(host, port, family, kind):
        assert family == launch.socket.AF_INET
        assert kind == launch.socket.SOCK_STREAM
        return [(family, kind, 6, "", (address, 0)) for address in addresses]

    monkeypatch.setattr(launch.socket, "getaddrinfo", local_addresses)
    assert launch.local_lan_addresses() == ["10.1.2.3", "172.16.2.3", "192.168.8.20"]


def test_lan_adapter_selection_requires_unambiguous_local_address(monkeypatch):
    monkeypatch.setattr(launch, "local_lan_addresses", lambda: ["192.168.8.20"])
    assert launch.select_lan_address(None) == "192.168.8.20"
    assert launch.select_lan_address("192.168.8.20") == "192.168.8.20"
    with pytest.raises(ValueError, match="local private"):
        launch.select_lan_address("0.0.0.0")
    monkeypatch.setattr(launch, "local_lan_addresses", lambda: [])
    with pytest.raises(ValueError, match="own booth router"):
        launch.select_lan_address(None)
    monkeypatch.setattr(launch, "local_lan_addresses", lambda: ["10.0.0.2", "192.168.8.20"])
    with pytest.raises(ValueError, match="--lan-ip"):
        launch.select_lan_address(None)
    assert launch.select_lan_address("192.168.8.20") == "192.168.8.20"


@pytest.mark.parametrize("booth", [False, True])
def test_cli_mode_is_explicit_and_prints_the_headset_address(booth, monkeypatch, capsys):
    settings = []

    class FakeSupervisor:
        def __init__(self, config):
            settings.append(config)

        async def run(self):
            pass

    def local_addresses():
        assert booth, "Default mode must not discover or bind any LAN adapter"
        return ["192.168.8.20"]

    monkeypatch.setattr(launch, "Supervisor", FakeSupervisor)
    monkeypatch.setattr(launch, "local_lan_addresses", local_addresses)
    monkeypatch.setattr(launch, "find_browser", lambda _: Path("edge.exe"))
    monkeypatch.setattr(launch, "list_displays", displays)
    launch.main(
        ["--python", sys.executable]
        + (["--booth", "--packet-source", "synthetic"] if booth else [])
    )
    config = settings[0]
    assert config.booth is booth
    assert config.browser_task is not booth
    output = capsys.readouterr().out
    if booth:
        assert config.bind_host == "192.168.8.20"
        assert "ws://192.168.8.20:8787/live" in output
        assert "NEVER venue WiFi" in output
        assert set(config.displays) == {"console", "spectator"}
    else:
        assert config.bind_host == "127.0.0.1"
        assert "no LAN listener" in output
        assert set(config.displays) == {"console", "task", "spectator"}


@pytest.mark.parametrize("args", [["--lan-ip", "192.168.8.20"], ["--booth", "--task-display", "0"]])
def test_cli_rejects_mixed_mode_flags(args):
    with pytest.raises(SystemExit) as error:
        launch.main(args)
    assert error.value.code == 2


def test_booth_mqtt_command_keeps_phone_stream_independent_of_session(config):
    config.booth = True
    config.packet_source = "mqtt"
    config.lan_ip = "192.168.1.201"
    config.mqtt_phone_ip = "192.168.1.135"
    command = launch.bridge_command(config, 2)
    for flag, value in {
        "--packet-source": "mqtt",
        "--mqtt-bind": "192.168.1.201",
        "--mqtt-phone-ip": "192.168.1.135",
        "--mqtt-port": "1883",
        "--mqtt-broker-port": "1884",
        "--mqtt-topic-prefix": "psl/prism-probe",
        "--mqtt-client-id": "verity-phone",
        "--mqtt-device-id": "1967873D",
    }.items():
        assert command[command.index(flag) + 1] == value
    assert "--one-session" not in command


@pytest.mark.parametrize(
    "extra", [[], ["--lan-ip", "192.168.1.201"], ["--mqtt-phone-ip", "192.168.1.135"]]
)
def test_booth_mqtt_requires_both_explicit_fixed_addresses(extra):
    with pytest.raises(SystemExit) as error:
        launch.main(["--booth", *extra])
    assert error.value.code == 2


def test_booth_defaults_mqtt_with_explicit_addresses(monkeypatch):
    settings = []

    class FakeSupervisor:
        def __init__(self, config):
            settings.append(config)

        async def run(self):
            pass

    monkeypatch.setattr(launch, "Supervisor", FakeSupervisor)
    monkeypatch.setattr(launch, "local_lan_addresses", lambda: ["192.168.1.201"])
    monkeypatch.setattr(launch, "find_browser", lambda _: Path("edge.exe"))
    launch.main(
        [
            "--python",
            sys.executable,
            "--headless",
            "--booth",
            "--lan-ip",
            "192.168.1.201",
            "--mqtt-phone-ip",
            "192.168.1.135",
        ]
    )
    assert settings[0].packet_source == "mqtt"


@pytest.mark.parametrize("fail_open", [False, True])
def test_supervisor_owns_firewall_and_cleans_up_on_startup_failure(config, monkeypatch, fail_open):
    events = []

    class Firewall:
        name = "owned-test-rule"
        scope = None

        def __init__(self, local_ip, phone_ip, python, *, port):
            assert (local_ip, phone_ip, port) == ("192.168.1.201", "192.168.1.135", 1883)

        def open(self):
            events.append("open")
            if fail_open:
                raise RuntimeError("firewall unavailable")

        def close(self):
            events.append("close")

    async def check():
        config.booth = True
        config.packet_source = "mqtt"
        config.lan_ip = "192.168.1.201"
        config.mqtt_phone_ip = "192.168.1.135"
        sup = launch.Supervisor(config)

        async def fail_step():
            events.append("step")
            raise RuntimeError("startup failed")

        monkeypatch.setattr(sup, "step", fail_step)
        with pytest.raises(RuntimeError):
            await sup.run()
        assert sup._lock_file is None

    monkeypatch.setattr(launch, "MqttFirewall", Firewall)
    asyncio.run(check())
    assert events == (["open", "close"] if fail_open else ["open", "step", "close"])


def test_cancelled_firewall_setup_waits_for_os_command_before_cleanup(config, monkeypatch):
    began, finish = threading.Event(), threading.Event()
    events = []

    class Firewall:
        name = "owned-test-rule"
        scope = None

        def __init__(self, *args, **kwargs):
            pass

        def open(self):
            began.set()
            assert finish.wait(3), "test did not release the simulated OS operation"
            events.append("created")

        def close(self):
            events.append("removed")

    async def check():
        config.booth, config.packet_source = True, "mqtt"
        config.lan_ip, config.mqtt_phone_ip = "192.168.1.201", "192.168.1.135"
        sup = launch.Supervisor(config)
        run = asyncio.create_task(sup.run())
        try:
            while not began.is_set():
                await asyncio.sleep(0.001)
            run.cancel()
            await asyncio.sleep(0.01)
            assert not events
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert events == ["created", "removed"]

    monkeypatch.setattr(launch, "MqttFirewall", Firewall)
    asyncio.run(check())


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


def test_booth_supervises_only_bridge_and_spectator_including_after_restart(config, monkeypatch):
    async def check():
        config.booth = True
        config.lan_ip = "192.168.8.20"
        sup = supervisor(config)
        started = []

        def spawn(role):
            started.append(role)
            return add_child(sup, role)

        async def healthy(child, now):
            child.ready = child.connected = True
            child.last_healthy = now

        monkeypatch.setattr(sup, "spawn", spawn)
        monkeypatch.setattr(sup, "_health", healthy)
        assert await sup.step()
        assert started == ["bridge", "spectator"]
        snapshot = sup.snapshot()
        assert snapshot["booth"] and not snapshot["browser_task"]
        assert snapshot["ready"]  # No task page or connected Quest required for process readiness.
        spectator = sup.children["spectator"]
        sup.children["bridge"].process.exit_code = 1
        assert await sup.step()
        assert not sup.snapshot()["ready"]
        sup.restart_at["bridge"] = 0
        assert await sup.step()
        assert started == ["bridge", "spectator", "bridge"]
        assert sup.children["spectator"] is spectator
        assert sup.generations == {"bridge": 1, "spectator": 0}
        assert sup.snapshot()["ready"]
        spectator.connected = False
        assert not sup.snapshot()["ready"]

    asyncio.run(check())


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


@pytest.mark.parametrize("stall_seconds", [2, 5, 10])
def test_bridge_stall_watchdog_includes_cached_status_freshness(config, monkeypatch, stall_seconds):
    """Freeze status, not the supervisor. Its existing policy has two successive grace windows."""

    async def check():
        sup = supervisor(config)
        child = add_child(sup, "bridge")
        now = 1000.0
        child.last_healthy = now
        child.audio_changed = now
        child.last_audio_frames = 100
        child.was_ready = True
        launch.write_json(
            config.bridge_status,
            dict(
                pid=child.process.pid,
                generation=0,
                ready=True,
                updated=now,
                audio_frames=100,
            ),
        )
        would_restart_at = None
        for quarter in range(1, stall_seconds * 4 + 1):
            now = 1000.0 + quarter / 4
            monkeypatch.setattr(launch.time, "time", lambda current=now: current)
            await sup._health(child, now)
            # Exactly the restart predicate in Supervisor.step after _health returns.
            if now - child.last_healthy > config.health_timeout:
                would_restart_at = now - 1000.0
                break
        if stall_seconds == 10:
            assert would_restart_at == 10.0
        else:
            assert would_restart_at is None
            launch.write_json(
                config.bridge_status,
                dict(
                    pid=child.process.pid,
                    generation=0,
                    ready=True,
                    updated=now,
                    audio_frames=200,
                ),
            )
            await sup._health(child, now)
            assert child.ready

    asyncio.run(check())
