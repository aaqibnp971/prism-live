"""In-process attendant UI (3.6), on LiveLoop's one asyncio thread.

Space/Enter is ONE button: arm -> cancel; running -> stop; reset -> wait.
No HTTP, WebSocket, callback from another thread, or session-control server exists here.
The terminal fills its assigned pane; a two-display booth tiles it beside the task on the laptop.
The pipe input is only for repeatable process-recovery tests, never a browser control path.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import queue
import shutil
import sys
import textwrap
import threading
import time
from pathlib import Path

from bridge.live import LiveLoop
from bridge.logging import SessionLog
from bridge.phase import SAMPLE_RATE

RESTART_NOTICE = "BRIDGE RESTARTED - VISITOR MUST START AGAIN. Nothing has resumed."


class ConsoleLog(SessionLog):
    """Keep only the gate's explicit decision for the local screen; not slope quality."""

    baseline_notice = ""

    def event(self, name, *, t_engine=None, **fields):
        super().event(name, t_engine=t_engine, **fields)
        if name == "segment" and fields.get("segment") == "baseline":
            self.baseline_notice = ""
        elif name == "baseline_end":
            self.baseline_notice = f"BASELINE: {fields['outcome'].upper()}"
            if fields.get("problems"):
                self.baseline_notice += " - " + "; ".join(fields["problems"])


class AttendantConsole:
    def __init__(self, bridge: LiveLoop, *, generation=0):
        self.bridge = bridge
        self.generation = generation
        self.restart_pending = bool(generation)
        self.notice = "Waiting for your press."
        self.start_task: asyncio.Task | None = None
        self.fault: Exception | None = None

    @property
    def armed(self):
        return self.start_task is not None and not self.start_task.done()

    async def press(self):
        """Always on the bridge loop; cancellation completes before another press can arm."""
        if self.armed:
            self.start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.start_task
            self.notice = "COUNTDOWN CANCELLED - disarmed."
        elif self.bridge.session.segment == "idle":
            self.notice = "Arming start..."
            self.start_task = asyncio.create_task(self._start(), name="attendant countdown")
            # Log/arm now, not on an unrelated later event-loop turn.
            await asyncio.sleep(0)
        elif self.bridge.session.segment == "reset":
            self.notice = "RESETTING - please wait. Start is disarmed."
        else:
            stopped = self.bridge.attendant_stop()
            self.notice = "STOPPED - 3 second reset." if stopped else "Nothing running."

    async def _start(self):
        try:
            refusal = await self.bridge.attendant_start()
            self.notice = "Session started." if refusal is None else f"START REFUSED: {refusal}"
            if refusal is None:
                self.restart_pending = False
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.notice = f"LOOP FAULT: {error}"
            self.fault = error  # render the fault once, then let the supervisor restart cleanly

    async def close(self):
        if self.armed:
            self.start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.start_task

    def snapshot(self):
        session = self.bridge.session
        now = self.bridge.clock()
        schedule = session.schedule
        alignment = self.bridge.start_alignment
        wait = None
        if self.armed and alignment is not None and self.bridge.phase is not None:
            wait = max(0, alignment.fire_frame - self.bridge.phase.frame()) / SAMPLE_RATE
        state = "COUNTDOWN" if self.armed else session.segment.upper()
        action = (
            "CANCEL COUNTDOWN"
            if self.armed
            else "ARM START"
            if session.segment == "idle"
            else "WAIT FOR RESET"
            if session.segment == "reset"
            else "STOP - 3 SECOND RESET"
        )
        # This is the host's live Schedule, not a browser guessing adaptive segment durations.
        ends = schedule.ends_at_latest_ms
        return {
            "state": state,
            "action": action,
            "session": session.session,
            "signal_lost": session.signal_lost,
            "countdown_s": wait,
            "remaining_s": None if ends is None else max(0, ends - now) / 1000,
            "notice": self.notice,
            "restart_notice": RESTART_NOTICE if self.restart_pending else "",
            "baseline_notice": getattr(self.bridge.log, "baseline_notice", ""),
            "source": "SYNTHETIC INPUT - NO ARMBAND",
            "psv_source": getattr(self.bridge.psv_feed, "source", "body"),
        }


_LETTER = {
    "L": ["#    ", "#    ", "#    ", "#    ", "#####"],
    "O": [" ### ", "#   #", "#   #", "#   #", " ### "],
    "S": [" ####", "#    ", " ### ", "    #", "#### "],
    "T": ["#####", "  #  ", "  #  ", "  #  ", "  #  "],
}


def screen_lines(view, columns=80):
    """Plain, testable content. No regulate_result access, verdict, or inferred close."""
    width = max(20, columns - 2)
    lines = ["PRISM / ATTENDANT", view["source"], ""]
    if view["signal_lost"]:
        lines += ["  ".join(_LETTER[c][row] for c in "LOST") for row in range(5)]
        lines += ["SIGNAL LOST / NO ACCEPTED BEAT", "No automatic stop. Check the armband.", ""]
    else:
        lines += ["SIGNAL PRESENT", ""]
    if view["restart_notice"]:
        lines += [view["restart_notice"], ""]
    lines += [f"STATE: {view['state']}", f"NEXT PRESS: {view['action']}"]
    if view["countdown_s"] is not None:
        lines += [f"START IN {view['countdown_s']:.1f} s", "Outside the session and its cap."]
    if view["remaining_s"] is not None:
        lines += [f"Segment ends within {view['remaining_s']:.1f} s"]
    lines += [
        "",
        view["notice"],
        view["baseline_notice"],
        "",
        "SPACE / ENTER = button",
        "B = body | P = pose | Q = quit booth",
        f"PSV: {view['psv_source']} | {view['session']}",
        "Close: read peaked-at minus left-at from the spectator trace. No verdict here.",
    ]
    return [part for line in lines for part in (textwrap.wrap(line, width) or [""])]


def render(view, columns=80, rows=30):
    # Erase and pad every row with the chosen background: the WHOLE pane signals loss.
    colour = "\x1b[41;97m" if view["signal_lost"] else "\x1b[40;97m"
    lines = screen_lines(view, columns)
    if len(lines) > rows - 1:
        # Preserve action and restart text on short terminals; omit the large decorative letters.
        lines = [line for line in lines if "#" not in line]
    body = "\r\n".join(line[: columns - 1].ljust(columns - 1) for line in lines[: rows - 1])
    return "\x1b[H" + colour + "\x1b[2J" + body


class PipeKeys:
    """A daemon reads bytes only. All interpretation and controls stay on the asyncio thread."""

    def __init__(self):
        self.queue = queue.SimpleQueue()
        threading.Thread(target=self._read, daemon=True, name="diagnostic stdin").start()

    def _read(self):
        while char := sys.stdin.read(1):
            self.queue.put(char)
        self.queue.put(None)

    def poll(self):
        result = []
        while not self.queue.empty():
            char = self.queue.get_nowait()
            if char is None:
                raise RuntimeError("console input closed unexpectedly")
            result.append(char)
        return result

    def close(self):
        pass


class TerminalKeys:
    """Windows console events, including key-up: held keys cannot toggle repeatedly."""

    def __init__(self):
        if os.name != "nt" or not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("Use a Windows terminal, or --console-input pipe for diagnostics")
        from ctypes import wintypes as w

        class Char(ctypes.Union):
            _fields_ = [("unicode", w.WCHAR), ("ascii", ctypes.c_char)]

        class Key(ctypes.Structure):
            _fields_ = [
                ("down", w.BOOL),
                ("repeat", w.WORD),
                ("vk", w.WORD),
                ("scan", w.WORD),
                ("char", Char),
                ("control", w.DWORD),
            ]

        class Event(ctypes.Union):
            _fields_ = [("key", Key), ("padding", ctypes.c_byte * 16)]

        class Record(ctypes.Structure):
            _fields_ = [("type", w.WORD), ("event", Event)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GetStdHandle.restype = w.HANDLE
        self.input = self.kernel.GetStdHandle(-10)
        self.output = self.kernel.GetStdHandle(-11)
        self.kernel.GetConsoleMode.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        self.kernel.SetConsoleMode.argtypes = [w.HANDLE, w.DWORD]
        self.kernel.GetNumberOfConsoleInputEvents.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        self.kernel.ReadConsoleInputW.argtypes = [
            w.HANDLE,
            ctypes.POINTER(Record),
            w.DWORD,
            ctypes.POINTER(w.DWORD),
        ]
        self.mode_in, self.mode_out = w.DWORD(), w.DWORD()
        if not (
            self.kernel.GetConsoleMode(self.input, ctypes.byref(self.mode_in))
            and self.kernel.GetConsoleMode(self.output, ctypes.byref(self.mode_out))
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        # Disable line/echo/quick-edit (selection must not suspend a live bridge).
        self.kernel.SetConsoleMode(self.input, (self.mode_in.value | 0x80) & ~(2 | 4 | 0x40))
        if not self.kernel.SetConsoleMode(self.output, self.mode_out.value | 4):
            self.close()
            raise RuntimeError("terminal must support virtual-terminal output")
        self.record_type, self.count_type = Record, w.DWORD
        self.held = set()

    def poll(self):
        count = self.count_type()
        if not self.kernel.GetNumberOfConsoleInputEvents(self.input, ctypes.byref(count)):
            raise ctypes.WinError(ctypes.get_last_error())
        result = []
        for _ in range(min(count.value, 64)):
            record, read = self.record_type(), self.count_type()
            if not self.kernel.ReadConsoleInputW(
                self.input, ctypes.byref(record), 1, ctypes.byref(read)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if record.type != 1:
                continue
            key = record.event.key
            if not key.down:
                self.held.discard(key.vk)
            elif key.vk not in self.held:
                self.held.add(key.vk)
                result.append(key.char.unicode)
        return result

    def close(self):
        self.kernel.SetConsoleMode(self.input, self.mode_in.value)
        self.kernel.SetConsoleMode(self.output, self.mode_out.value)


def write_status(path: Path, payload) -> bool:
    """Read-only supervisor telemetry on disk; NOT a source of session commands."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    # Windows readers may briefly deny delete-sharing. Keep the previous atomic snapshot;
    # the next 250 ms publication retries, rather than crashing a healthy bridge.
    try:
        temporary.replace(path)
    except PermissionError:
        return False
    return True


async def run_console(
    bridge, *, generation=0, input_mode="terminal", status_file=None, clients=lambda: []
):
    console = AttendantConsole(bridge, generation=generation)
    keys = PipeKeys() if input_mode == "pipe" else TerminalKeys()
    terminal = input_mode == "terminal"
    last_render = 0.0
    last_status = 0.0
    shutdown_requested = False
    previous_text = None
    previous_render = None
    last_frame, last_audio = -1, time.monotonic()
    if terminal:
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
    try:
        while True:
            for key in keys.poll():
                if key in (" ", "\r", "\n"):
                    await console.press()
                elif key.lower() in ("b", "p"):
                    bridge.set_psv_source("body" if key.lower() == "b" else "pose")
                elif key.lower() == "q":
                    shutdown_requested = True
                    return
            now = time.monotonic()
            if now - last_render >= 0.1:
                last_render = now
                view = console.snapshot()
                text = "\n".join(screen_lines(view))
                if terminal:
                    size = shutil.get_terminal_size((80, 35))
                    painted = render(view, size.columns, size.lines)
                    if painted != previous_render:
                        sys.stdout.write(painted)
                        sys.stdout.flush()
                        previous_render = painted
                elif text != previous_text:
                    print(text, flush=True)
                previous_text = text
                frames = bridge.phase.frame() if bridge.phase is not None else 0
                if frames != last_frame:
                    last_frame, last_audio = frames, now
                elif bridge.phase is not None and now - last_audio > 2:
                    raise RuntimeError("audio frame counter stalled; restart required")
                if status_file and now - last_status >= 0.25:
                    last_status = now
                    write_status(
                        Path(status_file),
                        {
                            "ready": frames > 0,
                            "pid": os.getpid(),
                            "generation": generation,
                            "session": bridge.session.session,
                            "segment": bridge.session.segment,
                            "signal_lost": bridge.session.signal_lost,
                            "updated": time.time(),
                            "console_state": view["state"],
                            "console_text": text,
                            "audio_frames": frames,
                            "source": "synthetic",
                            "clients": clients(),
                        },
                    )
                if console.fault is not None:
                    raise console.fault
            await asyncio.sleep(0.01)
    finally:
        await console.close()
        keys.close()
        if status_file:
            status = {
                "ready": False,
                "pid": os.getpid(),
                "generation": generation,
                "updated": time.time(),
                "shutdown_requested": shutdown_requested,
            }
            # Unlike an ordinary snapshot, intentional Q must reach the supervisor before
            # Console Host exits. Retry a transient reader collision without blocking ticks.
            deadline = time.monotonic() + 1.0
            while not write_status(Path(status_file), status):
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.025)
        if terminal:
            sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
