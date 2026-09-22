"""Shared field controller and both browser shells; no hardware or synthetic production path."""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from bridge.contract import validate
from tools.check_spectator_browser import Cdp, FixtureFeed, browser_path

ROOT = Path(__file__).parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node is needed for browser field integration tests")
def test_browser_field_integration():
    result = subprocess.run(
        [NODE, "--test", "tests/web_field_integration.test.cjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


class TaskFieldFeed(FixtureFeed):
    """Recorded contract states over the real task socket; test-only feed, no app hook."""

    async def handler(self, socket):
        hello = json.loads(await socket.recv())
        validate(hello, "in")
        assert hello["client"] == "task-screen"
        self.clients.add(socket)
        try:
            async for raw in socket:
                message = json.loads(raw)
                validate(message, "in")
                if message["type"] == "clock":
                    await socket.send(
                        json.dumps(
                            {
                                "type": "clock",
                                "v": 1,
                                "role": "pong",
                                "t_client_sent": message["t_client_sent"],
                                "t_engine": self.now(),
                            }
                        )
                    )
        except ConnectionClosed:
            pass
        finally:
            self.clients.discard(socket)


async def check_task_field(browser):
    feed = TaskFieldFeed()
    feed.set_state("baseline", 0)
    with tempfile.TemporaryDirectory(
        prefix="prism-task-field-", ignore_cleanup_errors=True
    ) as temp:
        async with serve(feed.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            process = subprocess.Popen(
                [
                    browser,
                    "--headless=new",
                    "--no-first-run",
                    "--disable-background-networking",
                    "--disable-extensions",
                    "--remote-debugging-port=0",
                    f"--user-data-dir={temp}",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            publisher = asyncio.create_task(feed.states())
            try:
                port_file = Path(temp) / "DevToolsActivePort"
                deadline = time.monotonic() + 20
                while not port_file.exists():
                    if time.monotonic() > deadline:
                        raise RuntimeError("Browser debugging port did not open")
                    await asyncio.sleep(0.1)
                debug_port = int(port_file.read_text().splitlines()[0])

                def targets():
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{debug_port}/json", timeout=5
                    ) as response:
                        return json.load(response)

                target = next(t for t in await asyncio.to_thread(targets) if t["type"] == "page")
                async with connect(target["webSocketDebuggerUrl"], max_size=None) as socket:
                    cdp = Cdp(socket)
                    await cdp.call("Runtime.enable")
                    await cdp.call("Network.enable")
                    await cdp.call(
                        "Emulation.setDeviceMetricsOverride",
                        width=1280,
                        height=720,
                        deviceScaleFactor=1,
                        mobile=False,
                    )
                    url = (
                        ROOT / "web/task/index.html"
                    ).as_uri() + f"?ws=ws://127.0.0.1:{port}/live"
                    await cdp.call("Page.navigate", url=url)
                    for segment in ("baseline", "load", "regulate", "resolve"):
                        feed.set_state(segment, 0)
                        await cdp.until(
                            "document.getElementById('segment-label')?.textContent === "
                            f"'{segment.upper()}'"
                        )
                        assert await cdp.evaluate(
                            "document.getElementById('connection-overlay').hidden && "
                            "document.getElementById('field-error').hidden && "
                            "!document.getElementById('ambient-field').hidden"
                        )
                        assert await cdp.evaluate(
                            "document.getElementById('instruction').hidden"
                        ) == (segment != "load")
                    await cdp.until("document.getElementById('ambient-field').width === 1280")
                    first = await cdp.evaluate(
                        "document.getElementById('ambient-field').toDataURL()"
                    )
                    await asyncio.sleep(0.4)
                    assert (
                        await cdp.evaluate("document.getElementById('ambient-field').toDataURL()")
                        != first
                    )  # Authored fog drift is actually drawn, not a static mock.
                    feed.silent = True
                    await cdp.until(
                        "document.getElementById('connection-overlay').dataset.lost === 'true'",
                        timeout=4,
                    )
                    frozen = await cdp.evaluate(
                        "document.getElementById('ambient-field').toDataURL()"
                    )
                    await asyncio.sleep(0.4)
                    assert (
                        await cdp.evaluate("document.getElementById('ambient-field').toDataURL()")
                        == frozen
                    )
                    feed.silent = False
                    feed.set_state("idle", 0, new_session=True)
                    await cdp.until(
                        "document.getElementById('segment-label').textContent === 'IDLE'"
                    )
                    assert await cdp.evaluate("document.getElementById('ambient-field').hidden")
                    assert not cdp.errors, cdp.errors
                    assert not [u for u in cdp.requests if u.startswith(("http:", "https:"))]
                    await cdp.call("Browser.close")
            finally:
                publisher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await publisher
                with contextlib.suppress(subprocess.TimeoutExpired):
                    await asyncio.to_thread(process.wait, 5)
                if process.poll() is None:
                    process.terminate()
                    await asyncio.to_thread(process.wait, 5)


@pytest.mark.skipif(browser_path() is None, reason="An installed Chromium browser is required")
def test_task_field_live_browser():
    asyncio.run(check_task_field(browser_path()))
