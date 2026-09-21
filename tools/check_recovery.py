"""Kill each supervised role mid-session, measuring real engine + live browser recovery.

Runs installed Edge headlessly because a second display may not be attached. No fake audio,
accelerated session clock, WebSocket control messages, or spectator simulation is used. The
synthetic armband is the bridge's normal source until 2.8. Local stdin presses exercise the same
AttendantConsole.press path as terminal keys. This does NOT verify physical display placement.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from tools.launch import ROOT, read_json


async def wait_status(process, path, predicate, *, timeout=30):
    deadline = time.perf_counter() + timeout
    latest = {}
    while time.perf_counter() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"launcher exited {process.returncode}; inspect {path.parent}")
        latest = read_json(path)
        if predicate(latest):
            return latest
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Recovery condition timed out: {json.dumps(latest)}")


def full_ready(status):
    bridge = status.get("bridge", {})
    return (
        status.get("ready") is True
        and not bridge.get("signal_lost", True)
        and set(bridge.get("clients", [])) >= {"task-screen", "spectator"}
        and time.time() - bridge.get("updated", 0) < 2
    )


async def check(output: Path):
    output.mkdir(parents=True, exist_ok=False)
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    command = [
        sys.executable,
        "-m",
        "tools.launch",
        "--headless",
        "--console-input",
        "pipe",
        "--port",
        str(port),
        "--status-dir",
        str(output / "status"),
        "--log-dir",
        str(output / "sessions"),
    ]
    records = []
    status_path = output / "status" / "launcher.json"
    with (output / "process.log").open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=stream,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            await wait_status(process, status_path, full_ready)
            for role in ("bridge", "task", "spectator"):
                state = read_json(status_path)
                if state["bridge"]["segment"] == "idle":
                    process.stdin.write(b" ")
                    process.stdin.flush()
                    await wait_status(
                        process,
                        status_path,
                        lambda s: s.get("bridge", {}).get("segment") == "baseline",
                        timeout=15,
                    )
                before = await wait_status(process, status_path, full_ready)
                assert before["bridge"]["segment"] in ("baseline", "load", "regulate", "resolve")
                child = before["children"][role]
                old_session = before["bridge"]["session"]
                # Exact PID supplied by this launcher's current owned-child record, never names
                # or a system-wide browser lookup. Exit is deliberately nonzero on Windows.
                began = time.perf_counter()
                os.kill(child["pid"], signal.SIGTERM)
                recovered = await wait_status(
                    process,
                    status_path,
                    lambda s, role=role, child=child: (
                        full_ready(s)
                        and s.get("children", {}).get(role, {}).get("generation", -1)
                        > child["generation"]
                    ),
                )
                elapsed = time.perf_counter() - began
                current = recovered["bridge"]
                if role == "bridge":
                    assert current["session"] != old_session
                    assert current["segment"] == "idle"
                    assert "VISITOR MUST START AGAIN" in current["console_text"]
                else:
                    assert current["session"] == old_session
                    assert current["segment"] != "idle"
                result = {
                    "killed": role,
                    "recovery_s": round(elapsed, 3),
                    "old_pid": child["pid"],
                    "new_pid": recovered["children"][role]["pid"],
                    "session_before": old_session,
                    "session_after": current["session"],
                    "segment_after": current["segment"],
                    "audio_frames": current["audio_frames"],
                    "both_browsers_connected": True,
                }
                records.append(result)
                print(json.dumps(result), flush=True)
                assert elapsed < 30, result
            process.stdin.write(b"q")
            process.stdin.flush()
            await asyncio.to_thread(process.wait, 15)
            assert process.returncode == 0
        finally:
            if process.poll() is None:
                with contextlib.suppress(OSError):
                    process.stdin.write(b"q")
                    process.stdin.flush()
                try:
                    await asyncio.to_thread(process.wait, 12)
                except subprocess.TimeoutExpired:
                    process.kill()  # owns Windows Jobs: closing handles reaps only this run
                    await asyncio.to_thread(process.wait, 5)
            process.stdin.close()
    report = {
        "measured_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "mode": "real audio, real clock, synthetic RR, two headless Edge pages",
        "recovery_end": "audio advancing; fresh console; trusted signal; both live pages connected",
        "physical_two_display_layout_verified": False,
        "results": records,
    }
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "logs" / f"recovery-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(check(args.output.resolve())), indent=2))


if __name__ == "__main__":
    main()
