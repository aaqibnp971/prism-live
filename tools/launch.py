"""One-command Windows booth launcher; never a network client of the attendant controls.

The bridge owns a dedicated Console Host terminal and its UI and buttons. The browser processes
have separate, launcher-owned profiles and are supervised independently.  Chromium's local
debug endpoint is used only to check/position its windows, never to start or stop a session.
See docs/launcher.md.  --headless is a recovery diagnostic, not a booth/simulation mode.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
POLL_SECONDS = 0.25
STARTUP_TIMEOUT = 15.0
HEALTH_TIMEOUT = 5.0
PROBE_INTERVAL = 1.0
ROLES = ("bridge", "task", "spectator")


@dataclass(frozen=True)
class Display:
    index: int
    name: str
    x: int
    y: int
    width: int
    height: int
    primary: bool = False


def list_displays() -> list[Display]:
    """Physical pixel bounds, including negative coordinates on extended desktops."""
    if os.name != "nt":
        raise RuntimeError("Booth display routing requires Windows; headless diagnostics do not.")
    user = ctypes.WinDLL("user32", use_last_error=True)
    # Per-monitor DPI awareness must precede display enumeration/window positioning.
    with contextlib.suppress(AttributeError):
        user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

    class MonitorInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    values = []
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )
    user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    user.GetMonitorInfoW.restype = wintypes.BOOL

    @callback_type
    def collect(monitor, _dc, _rect, _data):
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return False
        rect = info.rcMonitor
        values.append(
            (
                info.szDevice,
                rect.left,
                rect.top,
                rect.right - rect.left,
                rect.bottom - rect.top,
                bool(info.dwFlags & 1),
            )
        )
        return True

    if not user.EnumDisplayMonitors(None, None, collect, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    # Primary first, then a stable device name; --list-displays prints the actual mapping.
    values.sort(key=lambda item: (not item[5], item[0]))
    return [Display(index, *value) for index, value in enumerate(values)]


def display_roles(
    displays: list[Display], console: int, task: int, spectator: int
) -> dict[str, Display]:
    requested = {"console": console, "task": task, "spectator": spectator}
    if spectator in (console, task):
        raise ValueError("The spectator needs its own display; do not hide the console or task.")
    available = {display.index: display for display in displays}
    if any(index not in available for index in requested.values()):
        raise ValueError(
            f"A requested display is unavailable ({len(displays)} connected). "
            "Run --list-displays and connect the booth displays. --headless is diagnostics only."
        )
    return {role: available[index] for role, index in requested.items()}


def role_bounds(config: Config, role: str) -> tuple[int, int, int, int]:
    """Two-screen booth: task left 65%, console right 35%; spectator owns its screen."""
    display = config.displays[role]
    shared = config.displays["task"].index == config.displays["console"].index
    if shared and role in ("task", "console"):
        task_width = round(display.width * 0.65)
        if role == "task":
            return display.x, display.y, task_width, display.height
        return display.x + task_width, display.y, display.width - task_width, display.height
    return display.x, display.y, display.width, display.height


def find_browser(explicit: Path | None = None) -> Path:
    candidates = (
        explicit,
        shutil.which("msedge"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    )
    if explicit is not None and not explicit.is_file():
        raise ValueError(f"Browser does not exist: {explicit}")
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise ValueError("Microsoft Edge was not found. Nothing has been installed.")


@dataclass
class Config:
    browser: Path
    python: Path = Path(sys.executable)
    root: Path = ROOT
    port: int = 8787
    log_dir: Path = ROOT / "logs"
    status_dir: Path = ROOT / "logs" / "launcher"
    headless: bool = False
    console_input: str = "terminal"
    displays: dict[str, Display] = field(default_factory=dict)
    distance_cm: float = 60.0
    screen_width_cm: float = 59.77
    startup_timeout: float = STARTUP_TIMEOUT
    health_timeout: float = HEALTH_TIMEOUT

    @property
    def bridge_status(self) -> Path:
        return self.status_dir / "bridge.json"


def bridge_command(config: Config, generation: int) -> list[str]:
    return [
        str(config.python),
        "-m",
        "bridge.server",
        "--host",
        "127.0.0.1",
        "--port",
        str(config.port),
        "--log-dir",
        str(config.log_dir),
        "--status-file",
        str(config.bridge_status),
        "--restart-generation",
        str(generation),
        "--console-input",
        config.console_input,
    ]


def browser_command(config: Config, role: str, profile: Path) -> list[str]:
    query: dict[str, str | float] = {"ws": f"ws://127.0.0.1:{config.port}/live"}
    if role == "task":
        fraction = (
            1.0 if config.headless else (role_bounds(config, role)[2] / config.displays[role].width)
        )
        query.update(
            distance_cm=config.distance_cm, screen_width_cm=config.screen_width_cm * fraction
        )
        if not config.headless:
            query.update(
                monitor_width_cm=config.screen_width_cm,
                monitor_width_px=config.displays[role].width,
            )
            if config.displays["task"].index == config.displays["console"].index:
                query["tiled"] = "1"
    url = (config.root / "web" / role / "index.html").resolve().as_uri()
    url += "?" + urllib.parse.urlencode(query)
    command = [
        str(config.browser),
        f"--user-data-dir={profile}",
        "--remote-debugging-port=0",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--disable-session-crashed-bubble",
        "--disable-background-networking",
        "--disable-extensions",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-features=msEdgeSidebarV2",
        "--noerrdialogs",
    ]
    if config.headless:
        return command + ["--headless=new", "--disable-gpu", "--window-size=1920,1080", url]
    x, y, width, height = role_bounds(config, role)
    command += [f"--window-position={x},{y}", f"--window-size={width},{height}"]
    if role == "task" and config.displays["task"].index == config.displays["console"].index:
        return command + [f"--app={url}"]
    return command + ["--kiosk", url, "--edge-kiosk-type=fullscreen"]


class WindowsJob:
    """Retain ownership of descendant browser processes even if their parent exits.

    Closing the supervisor closes these non-inheritable handles: Windows kills only the
    processes assigned to them, never a user's unrelated Edge instance/profile.
    """

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process: subprocess.Popen) -> None:
        if self.handle and not self.kernel.AssignProcessToJobObject(
            self.handle, int(process._handle)
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def pids(self) -> set[int]:
        if not self.handle:
            return set()

        class ProcessList(ctypes.Structure):
            _fields_ = [
                ("assigned", wintypes.DWORD),
                ("count", wintypes.DWORD),
                ("ids", ctypes.c_size_t * 256),
            ]

        processes = ProcessList()
        if not self.kernel.QueryInformationJobObject(
            self.handle, 3, ctypes.byref(processes), ctypes.sizeof(processes), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return set(processes.ids[: processes.count])


@dataclass
class ManagedChild:
    role: str
    process: subprocess.Popen
    job: WindowsJob
    generation: int
    launched: float
    profile: Path | None = None
    ready: bool = False
    connected: bool = False
    last_healthy: float = 0.0
    last_probe: float = 0.0
    page_url: str | None = None
    last_audio_frames: int | None = None
    audio_changed: float = 0.0
    error: str | None = None
    positioned: bool = False
    was_ready: bool = False
    launched_epoch: float = field(default_factory=time.time)


def position_window(child: ManagedChild, config: Config) -> bool:
    """Position only a visible top-level window belonging to a launcher-owned process."""
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.MoveWindow.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.BOOL,
    ]
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = []
    owned_pids = child.job.pids() if child.role == "bridge" else {child.process.pid}

    @callback_type
    def collect(window, _data):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if pid.value in owned_pids and user.IsWindowVisible(window):
            found.append(window)
        return True

    user.EnumWindows(collect, 0)
    if not found:
        return False
    role = "console" if child.role == "bridge" else child.role
    x, y, width, height = role_bounds(config, role)
    positioned = True
    for window in found:
        rect = wintypes.RECT()
        if not user.GetWindowRect(window, ctypes.byref(rect)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Console Host quantizes its size to character cells and the available desktop.
        height_tolerance = 64 if role == "console" else 2
        correct = (
            abs(rect.left - x) <= 2
            and abs(rect.top - y) <= 2
            and abs(rect.right - rect.left - width) <= 16
            and 0 <= height - (rect.bottom - rect.top) <= height_tolerance
        )
        if correct:
            continue
        user.ShowWindow(window, 9)
        if not user.MoveWindow(window, x, y, width, height, True):
            raise ctypes.WinError(ctypes.get_last_error())
        # Console Host's resize handler can retain the old position while quantizing its
        # character-cell size. Apply position separately, without another resize message.
        if role == "console" and not user.SetWindowPos(window, None, x, y, 0, 0, 0x0415):
            raise ctypes.WinError(ctypes.get_last_error())
        # A just-created conhost can overwrite the first placement after MoveWindow returns.
        # Verify on a later poll instead of accepting that first request as proof of placement.
        positioned = False
    return positioned


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    # Windows readers can briefly deny DELETE sharing while opening the old status file.
    # Telemetry is expendable for one poll; it must never crash supervision/session control.
    with contextlib.suppress(PermissionError):
        temporary.replace(path)


async def browser_probe(child: ManagedChild, config: Config) -> tuple[bool, bool]:
    """Check real page responsiveness and feed indicator, without injecting events/data."""
    assert child.profile
    port = int((child.profile / "DevToolsActivePort").read_text().splitlines()[0])

    def get_targets():
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=0.5) as response:
            return json.load(response)

    targets = await asyncio.to_thread(get_targets)
    expected = (config.root / "web" / child.role / "index.html").resolve().as_uri()
    target = next(
        item
        for item in targets
        if item.get("type") == "page" and item.get("url", "").split("?")[0] == expected
    )
    child.page_url = target["url"]
    expression = (
        "({ready:document.readyState==='complete'&&!!document.getElementById('fullscreen-button'),"
        "connected:document.getElementById('connection-banner')?.hidden===true})"
        if child.role == "spectator"
        else "({ready:document.readyState==='complete'&&"
        "!!document.getElementById('geometry-label'),"
        "connected:document.getElementById('link-dot')?.dataset.state==='connected',"
        "width:innerWidth,scale:devicePixelRatio})"
    )
    async with connect(
        target["webSocketDebuggerUrl"], open_timeout=0.5, close_timeout=0.1
    ) as socket:
        await socket.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {"expression": expression, "returnByValue": True},
                }
            )
        )
        while True:
            reply = json.loads(await asyncio.wait_for(socket.recv(), 0.5))
            if reply.get("id") == 1:
                if "error" in reply or "exceptionDetails" in reply.get("result", {}):
                    raise RuntimeError("Browser page is not responding normally")
                value = reply["result"]["result"]["value"]
                return bool(value["ready"]), bool(value["connected"])


class Supervisor:
    def __init__(self, config: Config):
        self.config = config
        self.children: dict[str, ManagedChild] = {}
        self.generations = dict.fromkeys(ROLES, 0)
        self.restart_at: dict[str, float] = {}
        self.events: list[dict[str, Any]] = []
        self.stopping = False
        self.status_path = config.status_dir / "launcher.json"
        self._lock_file = None

    def event(self, kind: str, role: str, **detail: Any) -> None:
        record = {"event": kind, "role": role, "at": time.time(), **detail}
        self.events.append(record)
        self.events = self.events[-50:]
        with (self.config.status_dir / "supervisor.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")

    def _acquire(self) -> None:
        self.config.status_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = (self.config.status_dir / "launcher.lock").open("a+b")
        self._lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            self._lock_file = None
            raise RuntimeError("A launcher already owns this status directory.") from None

    def spawn(self, role: str) -> ManagedChild:
        generation = self.generations[role]
        profile = None
        if role == "bridge":
            command = bridge_command(self.config, generation)
            if not self.config.headless:
                # Explicit Console Host bypasses Windows Terminal's invisible pseudo-window.
                # It belongs to this launcher's Job; no existing user terminal is moved.
                conhost = Path(os.environ["SYSTEMROOT"]) / "System32" / "conhost.exe"
                command = [str(conhost), "--", *command]
        else:
            # Fresh on restart: no restoration of stale tabs, frames, or crashed profile locks.
            # Browser startup creates thousands of small cache/profile files. Keep this off
            # the project/audio/log drive; a slower external D: disk can stall the bridge.
            profile = Path(tempfile.mkdtemp(prefix=f"prism-live-{role}-"))
            command = browser_command(self.config, role, profile)
        job = WindowsJob()
        process = None
        try:
            # Diagnostics inherit stdin. Visible mode gives bridge its own Console Host.
            process = subprocess.Popen(
                command,
                cwd=self.config.root,
                stdin=None if role == "bridge" else subprocess.DEVNULL,
                stdout=None if role == "bridge" else subprocess.DEVNULL,
                stderr=None if role == "bridge" else subprocess.DEVNULL,
                creationflags=(subprocess.CREATE_NO_WINDOW if role != "bridge" else 0)
                if os.name == "nt"
                else 0,
                start_new_session=os.name != "nt" and role != "bridge",
            )
            job.assign(process)
        except BaseException:
            job.close()
            if process is not None:
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=3)
            raise
        now = time.monotonic()
        child = ManagedChild(
            role,
            process,
            job,
            generation,
            now,
            profile=profile,
            last_healthy=now,
            audio_changed=now,
        )
        self.children[role] = child
        self.event("started", role, pid=process.pid, generation=generation)
        return child

    async def stop_child(self, role: str) -> None:
        child = self.children.pop(role, None)
        if child is None:
            return
        child.job.close()  # Includes renderer/GPU descendants, even after root browser exit.
        if os.name != "nt" and role != "bridge":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.process.pid, signal.SIGTERM)
        if child.process.poll() is None:
            child.process.terminate()
        try:
            await asyncio.to_thread(child.process.wait, 2)
        except subprocess.TimeoutExpired:
            child.process.kill()
            await asyncio.to_thread(child.process.wait, 2)

    async def restart(self, role: str, reason: str) -> None:
        child = self.children[role]
        self.event("restart", role, pid=child.process.pid, reason=reason)
        if role == "bridge":
            print("BRIDGE RESTARTING — the visitor must start again. " + reason, flush=True)
        await self.stop_child(role)
        self.generations[role] += 1
        # Avoid spinning on missing device/startup failures, while retaining <30s recovery budget.
        self.restart_at[role] = time.monotonic() + 1.0

    async def _health(self, child: ManagedChild, now: float) -> None:
        if not self.config.headless:
            child.positioned = position_window(child, self.config)
            if not child.positioned:
                child.ready = child.connected = False
                return
        if child.role == "bridge":
            status = read_json(self.config.bridge_status)
            owned = child.job.pids() | {child.process.pid}
            current = status.get("pid") in owned
            current = current and status.get("generation") == child.generation
            fresh = time.time() - status.get("updated", 0) < self.config.health_timeout
            frames = status.get("audio_frames")
            if current and isinstance(frames, int) and frames != child.last_audio_frames:
                child.last_audio_frames = frames
                child.audio_changed = now
            audio_live = now - child.audio_changed < self.config.health_timeout
            child.ready = bool(current and fresh and status.get("ready") and audio_live)
            child.connected = child.ready
            if child.ready:
                child.last_healthy = now
                child.was_ready = True
            return
        if now - child.last_probe < PROBE_INTERVAL:
            return
        child.last_probe = now
        try:
            child.ready, child.connected = await asyncio.wait_for(
                browser_probe(child, self.config), 1.5
            )
            if child.ready:
                child.error = None
                bridge = self.children.get("bridge")
                # A bridge restart is not a browser fault. A live bridge with a permanently
                # disconnected page is: refresh that process, not the visitor's session.
                if child.connected or bridge is None or not bridge.ready:
                    child.last_healthy = now
                if child.connected:
                    child.was_ready = True
                else:
                    child.error = "page is responsive but its live feed has not reconnected"
        except (OSError, ValueError, StopIteration, KeyError, RuntimeError, TimeoutError) as error:
            child.ready = child.connected = False
            child.error = str(error)
        except Exception as error:
            # WebSocket handshake/disconnection and renderer termination are health failures.
            child.ready = child.connected = False
            child.error = f"{type(error).__name__}: {error}"

    async def step(self) -> bool:
        """One bounded supervisor poll. False is an explicit clean console shutdown."""
        now = time.monotonic()
        for role in ROLES:
            if role not in self.children:
                if now >= self.restart_at.get(role, 0):
                    try:
                        self.spawn(role)
                    except OSError as error:
                        self.event("spawn_failed", role, error=str(error))
                        print(f"{role.upper()} RESTARTING — {error}", flush=True)
                        self.generations[role] += 1
                        self.restart_at[role] = time.monotonic() + 1.0
                continue
            child = self.children[role]
            exit_code = child.process.poll()
            if exit_code is not None:
                status = read_json(self.config.bridge_status) if role == "bridge" else {}
                clean_exit = (
                    exit_code == 0
                    if self.config.headless
                    else (
                        status.get("shutdown_requested") is True
                        and status.get("generation") == child.generation
                        and status.get("updated", 0) >= child.launched_epoch
                    )
                )
                if role == "bridge" and clean_exit:
                    self.event("attendant_shutdown", role, pid=child.process.pid)
                    return False
                await self.restart(role, f"process exited {exit_code}")
        await asyncio.gather(*(self._health(child, now) for child in self.children.values()))
        for role, child in tuple(self.children.items()):
            timeout = (
                self.config.startup_timeout if not child.was_ready else self.config.health_timeout
            )
            if now - child.last_healthy > timeout:
                await self.restart(role, f"unresponsive for {timeout:g}s: {child.error or role}")
        self.snapshot()
        return True

    def snapshot(self) -> dict[str, Any]:
        bridge = read_json(self.config.bridge_status)
        children = {
            role: {
                "pid": child.process.pid,
                "generation": child.generation,
                "wrapper_pid": child.process.pid,
                "ready": child.ready,
                "connected": child.connected,
                "profile": str(child.profile) if child.profile else None,
                "error": child.error,
            }
            for role, child in self.children.items()
        }
        if "bridge" in children:
            child = self.children["bridge"]
            if bridge.get("pid") in (child.job.pids() | {child.process.pid}):
                children["bridge"]["pid"] = bridge["pid"]
        value = {
            "pid": os.getpid(),
            "updated": time.time(),
            "headless": self.config.headless,
            "stopping": self.stopping,
            "children": children,
            "bridge": bridge,
            "ready": len(children) == len(ROLES)
            and all(child["ready"] and child["connected"] for child in children.values()),
            "events": self.events,
        }
        write_json(self.status_path, value)
        return value

    async def close(self) -> None:
        self.stopping = True
        for role in tuple(self.children):
            await self.stop_child(role)
        if self._lock_file is not None:
            self.snapshot()
            self._lock_file.close()
            self._lock_file = None

    async def run(self) -> None:
        self._acquire()
        try:
            while await self.step():
                await asyncio.sleep(POLL_SECONDS)
        finally:
            await self.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-displays", action="store_true")
    parser.add_argument("--console-display", type=int, default=0)
    parser.add_argument("--task-display", type=int, default=0)
    parser.add_argument("--spectator-display", type=int, default=1)
    parser.add_argument(
        "--headless", action="store_true", help="Recovery diagnostic, not booth mode"
    )
    parser.add_argument("--console-input", choices=("terminal", "pipe"), default="terminal")
    parser.add_argument("--browser", type=Path)
    parser.add_argument("--python", type=Path, default=ROOT / ".venv" / "Scripts" / "python.exe")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--log-dir", type=Path, default=ROOT / "logs")
    parser.add_argument("--status-dir", type=Path, default=ROOT / "logs" / "launcher")
    parser.add_argument("--distance-cm", type=float, default=60.0)
    parser.add_argument("--screen-width-cm", type=float, default=59.77)
    args = parser.parse_args()
    try:
        if args.list_displays:
            print(json.dumps([asdict(display) for display in list_displays()], indent=2))
            return
        if not args.python.is_file():
            raise ValueError(f"Python environment missing: {args.python}; nothing was installed.")
        if not 1 <= args.port <= 65535:
            raise ValueError("Port must be between 1 and 65535.")
        if args.distance_cm <= 0 or args.screen_width_cm <= 0:
            raise ValueError("Viewing geometry must be positive.")
        config = Config(
            browser=find_browser(args.browser),
            python=args.python.resolve(),
            port=args.port,
            log_dir=args.log_dir.resolve(),
            status_dir=args.status_dir.resolve(),
            headless=args.headless,
            console_input=args.console_input,
            distance_cm=args.distance_cm,
            screen_width_cm=args.screen_width_cm,
        )
        if not config.headless:
            config.displays = display_roles(
                list_displays(), args.console_display, args.task_display, args.spectator_display
            )
        else:
            print("HEADLESS RECOVERY DIAGNOSTIC — display placement is not tested.", flush=True)
        asyncio.run(Supervisor(config).run())
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(2, f"Launcher: {error}\n")


if __name__ == "__main__":
    main()
