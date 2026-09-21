"""Real-browser regression check for the spectator; optional local screenshot capture.

Feeds contract-valid test messages through a localhost WebSocket. No test hook or alternate mode
is added to the screen. Uses an already-installed Chromium browser; installs nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import copy
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from bridge.contract import validate
from tools.fake_sender import DEFAULT_FIXTURE, load_outbound

ROOT = Path(__file__).resolve().parents[1]


def browser_path() -> str | None:
    for candidate in (
        shutil.which("msedge"),
        shutil.which("chromium"),
        shutil.which("google-chrome"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


class Cdp:
    def __init__(self, socket):
        self.socket = socket
        self.counter = 0
        self.errors = []
        self.requests = []

    async def call(self, method, **params):
        self.counter += 1
        await self.socket.send(json.dumps({"id": self.counter, "method": method, "params": params}))
        while True:
            reply = json.loads(await asyncio.wait_for(self.socket.recv(), 10))
            if reply.get("method") == "Runtime.exceptionThrown":
                self.errors.append(reply["params"])
            if reply.get("method") == "Network.requestWillBeSent":
                self.requests.append(reply["params"]["request"]["url"])
            if reply.get("id") == self.counter:
                assert "error" not in reply, reply
                return reply.get("result", {})

    async def evaluate(self, expression):
        response = await self.call(
            "Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True
        )
        assert "exceptionDetails" not in response, response
        return response["result"].get("value")

    async def until(self, expression, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.evaluate(expression):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"browser condition timed out: {expression}")

    async def screenshot(self, path):
        response = await self.call(
            "Page.captureScreenshot", format="png", captureBeyondViewport=False
        )
        path.write_bytes(base64.b64decode(response["data"]))


class FixtureFeed:
    def __init__(self):
        self.records = load_outbound(DEFAULT_FIXTURE)
        self.origin = time.perf_counter()
        self.clients = set()
        self.seq = 0
        self.beat_seq = 0
        self.current = None
        self.silent = False
        self.session = "S-20260921-0001"
        self.set_state("regulate", 30_000)

    def now(self):
        return round((time.perf_counter() - self.origin) * 1000)

    def set_state(self, segment, elapsed, *, new_session=False):
        if new_session:
            self.session = "S-20260921-0002"
            self.seq = self.beat_seq = 0
        matches = [m for _, m in self.records if m["type"] == "state" and m["segment"] == segment]
        self.current = copy.deepcopy(
            min(matches, key=lambda m: abs(m["segment_elapsed_ms"] - elapsed))
        )
        self.current["segment_elapsed_ms"] = elapsed
        if segment == "regulate":
            self.current["t_session"] = 150_000  # screenshot comparison point, supplied by host
        if segment == "idle":
            self.current["hr_bpm"] = self.current["hr_base"] = None
            self.current["confidence"] = dict.fromkeys(self.current["confidence"], 0)

    async def handler(self, socket):
        hello = json.loads(await socket.recv())
        validate(hello, "in")
        assert hello["client"] == "spectator"
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
            pass  # Reloading/closing a browser can close without a WebSocket close frame.
        finally:
            self.clients.discard(socket)

    async def publish(self, message):
        validate(message, "out")
        for socket in tuple(self.clients):
            with contextlib.suppress(ConnectionClosed):
                await socket.send(json.dumps(message))

    async def states(self):
        while True:
            if self.clients and not self.silent:
                self.seq += 1
                await self.publish(
                    dict(self.current, session=self.session, seq=self.seq, t_engine=self.now())
                )
            await asyncio.sleep(0.1)

    async def trace(self, bpms=None):
        if bpms is None:
            # Recorded values, compressed in time only for the browser integration check.
            bpms = [
                m["hr_bpm"] for _, m in self.records if m["type"] == "beat" and m["quality"] == "ok"
            ][:180]
        anchor = self.now() + 400
        for index, bpm in enumerate(bpms):
            self.beat_seq += 1
            await self.publish(
                {
                    "type": "beat",
                    "v": 1,
                    "session": self.session,
                    "seq": self.beat_seq,
                    "t_play": anchor + index * 8,
                    "rr_ms": 60_000 / bpm,
                    "hr_bpm": bpm,
                    "quality": "ok",
                }
            )
        await asyncio.sleep((400 + len(bpms) * 8) / 1000 + 0.15)


# Actual DOM geometry, beyond the pure numerical trace test. The SVG viewport, clip and path
# must all fit; this catches the former height:100%/overflow:visible card regression.
BOUNDS = r"""(() => {
  const get = id => document.getElementById(id);
  const rect = node => node.getBoundingClientRect();
  const inside = (a,b) => a.left >= b.left-.1 && a.top >= b.top-.1 &&
    a.right <= b.right+.1 && a.bottom <= b.bottom+.1;
  const svg = get('live-trace-svg'), card = get('live-trace-card'), frame = svg.parentElement;
  const line = get('live-trace-line'), end = get('live-trace-end');
  const bounds = line.getBBox(), box = svg.viewBox.baseVal;
  const header = document.querySelector('.session-heading');
  const title = rect(get('segment-title')), verb = rect(get('segment-verb'));
  const timer = rect(get('session-time'));
  const rail = document.querySelector('.dimensions');
  return {
    viewport: [innerWidth,innerHeight],
    plotInsideCard: inside(rect(svg),rect(card)) && inside(rect(frame),rect(card)),
    pathInsidePlot: bounds.x >= 0 && bounds.y >= 0 &&
      bounds.x+bounds.width <= box.width && bounds.y+bounds.height <= box.height,
    endpointInsidePlot: Number(end.getAttribute('cy')) >= 5 &&
      Number(end.getAttribute('cy')) <= box.height-5,
    clipped: getComputedStyle(frame).overflow === 'hidden' &&
      getComputedStyle(svg).overflow === 'hidden' && !!svg.querySelector('[clip-path]'),
    noFullscreenOverlap: rect(card).right <= rect(get('fullscreen-button')).left,
    clearHeader: title.right <= verb.left && verb.right <= timer.left &&
      inside(title,rect(header)) && inside(verb,rect(header)),
    railFits: inside(rect(document.querySelector('.rail-footer')),rect(rail)),
    monoLoaded: document.fonts.check('500 48px "IBM Plex Mono"'),
    sansLoaded: document.fonts.check('600 40px "IBM Plex Sans"'),
    valence: ['confidence','authority'].map(k=>get('valence-'+k).textContent),
    zeroBars: ['reading','authority'].every(k=>rect(get('valence-'+k+'-bar')).width === 0),
    traceMinimum: Number(get('live-trace-min').textContent),
    authority: get('arousal-authority').textContent
  };
})()"""


async def check(browser, output=None, reference=None):
    if output:
        output.mkdir(parents=True, exist_ok=True)
    feed = FixtureFeed()
    report = {}
    with tempfile.TemporaryDirectory(
        prefix="prism-spectator-", ignore_cleanup_errors=True
    ) as profile:
        async with serve(feed.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            process = subprocess.Popen(
                [
                    browser,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-first-run",
                    "--disable-background-networking",
                    "--disable-extensions",
                    "--remote-debugging-port=0",
                    f"--user-data-dir={profile}",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            publisher = asyncio.create_task(feed.states())
            try:
                port_file = Path(profile) / "DevToolsActivePort"
                deadline = time.monotonic() + 20
                while not port_file.exists():
                    if time.monotonic() > deadline:
                        raise RuntimeError("Browser failed to open its local debugging port")
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
                        width=1920,
                        height=1080,
                        deviceScaleFactor=1,
                        mobile=False,
                    )
                    url = (
                        ROOT / "web/spectator/index.html"
                    ).as_uri() + f"?ws=ws://127.0.0.1:{port}/live"
                    await cdp.call("Page.navigate", url=url)
                    await cdp.until("document.body?.dataset.view === 'active'")
                    await cdp.evaluate("document.fonts.ready.then(()=>true)")
                    await feed.trace()
                    report["running"] = await cdp.evaluate(BOUNDS)
                    if output:
                        await cdp.screenshot(output / "new-screen-1920x1080.png")
                    for key in (
                        "plotInsideCard",
                        "pathInsidePlot",
                        "endpointInsidePlot",
                        "clipped",
                        "noFullscreenOverlap",
                        "clearHeader",
                        "railFits",
                        "monoLoaded",
                        "sansLoaded",
                        "zeroBars",
                    ):
                        assert report["running"][key], (key, report["running"])
                    assert report["running"]["valence"] == ["0.00", "0.00"]
                    assert (
                        report["running"]["authority"]
                        == f"{feed.current['authority']['arousal']:.2f}"
                    )
                    await feed.trace([220, 34])
                    report["low_endpoint"] = await cdp.evaluate(BOUNDS)
                    assert report["low_endpoint"]["traceMinimum"] <= 34
                    assert (
                        report["low_endpoint"]["pathInsidePlot"]
                        and report["low_endpoint"]["endpointInsidePlot"]
                    )
                    # Baseline fill uses the host's 22.5 / 45 s, not the local clock.
                    feed.set_state("baseline", 22_500)
                    await cdp.until("!document.getElementById('baseline-learning').hidden")
                    assert (
                        await cdp.evaluate(
                            "document.getElementById('baseline-learning-fill').style.width"
                        )
                        == "50%"
                    )
                    if output:
                        await cdp.screenshot(output / "baseline-1920x1080.png")
                    await feed.trace([72, 34, 95])
                    feed.set_state("reset", 0)
                    await cdp.until("document.body.dataset.view === 'trace-hold'")
                    feed.set_state("idle", 0, new_session=True)
                    await cdp.until("document.body.dataset.view === 'idle-trace'")
                    assert await cdp.evaluate(
                        "document.getElementById('segment-strip').hidden && "
                        "document.getElementById('active-dashboard').hidden"
                    )
                    if output:
                        await cdp.screenshot(output / "idle-held-1920x1080.png")
                    # Reload with no history honestly gives cold idle.
                    await cdp.call("Page.reload")
                    await cdp.until("document.body.dataset.view === 'idle-cold'")
                    if output:
                        await cdp.screenshot(output / "idle-cold-1920x1080.png")
                    feed.set_state("regulate", 90_000)
                    await cdp.until(
                        "document.getElementById('progress-note').textContent.includes('HOLD +15')"
                    )
                    before = await cdp.evaluate(
                        "document.getElementById('session-time').textContent"
                    )
                    feed.silent = True  # Pongs still arrive: state freshness must drive the marker.
                    started = time.monotonic()
                    await cdp.until(
                        "document.getElementById('connection-banner').dataset.mode === 'lost'",
                        timeout=4,
                    )
                    report["silent_feed_marker_s"] = round(time.monotonic() - started, 3)
                    assert report["silent_feed_marker_s"] < 3
                    assert (
                        await cdp.evaluate("document.getElementById('session-time').textContent")
                        == before
                    )
                    if output:
                        await cdp.screenshot(output / "disconnected-1920x1080.png")
                    report["external_requests"] = [
                        u for u in cdp.requests if u.startswith(("http:", "https:"))
                    ]
                    assert not report["external_requests"]
                    assert not cdp.errors, cdp.errors
                    if reference:
                        await cdp.call("Page.navigate", url=reference.resolve().as_uri())
                        await cdp.until("!!document.getElementById('mirrorRef')")
                        await cdp.evaluate("document.fonts.ready.then(()=>true)")
                        if output:
                            await cdp.screenshot(output / "v3-reference-1920x1080.png")
                            comparison = output / "comparison.html"
                            comparison.write_text(
                                """<!doctype html><meta charset="utf-8">
<title>Spectator v3 / live port — 1920 × 1080 each</title>
<style>*{box-sizing:border-box}body{margin:0;background:#140e0b;color:#f7eadc;font:26px monospace}
main{display:flex;width:3840px}figure{margin:0;width:1920px}
figcaption{height:60px;padding:14px 28px}
img{display:block;width:1920px;height:1080px}</style><main>
<figure><figcaption>V3 — extracted, frozen design reference (not the bundle)</figcaption>
<img src="v3-reference-1920x1080.png" alt="Approved v3 design reference"></figure>
<figure><figcaption>LIVE PORT — recorded fixture via WebSocket; field deferred to 3.4</figcaption>
<img src="new-screen-1920x1080.png" alt="New plain-JavaScript spectator"></figure></main>""",
                                encoding="utf-8",
                            )
                            await cdp.call(
                                "Emulation.setDeviceMetricsOverride",
                                width=3840,
                                height=1140,
                                deviceScaleFactor=1,
                                mobile=False,
                            )
                            await cdp.call("Page.navigate", url=comparison.resolve().as_uri())
                            await cdp.until(
                                "document.images.length === 2 && "
                                "[...document.images].every(i=>i.complete&&i.naturalWidth===1920)"
                            )
                            await cdp.screenshot(output / "side-by-side.png")
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
    if output:
        (output / "checks.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--reference", type=Path, help="extracted frozen v3 reference, never the bundle"
    )
    args = parser.parse_args()
    browser = browser_path()
    if browser is None:
        raise SystemExit("An installed Edge/Chromium browser is required. Nothing was installed.")
    print(json.dumps(asyncio.run(check(browser, args.output, args.reference)), indent=2))


if __name__ == "__main__":
    main()
