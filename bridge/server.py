"""The laptop end of the link in docs/message-contract-v1.md: ws://<laptop>:8787/live.

The laptop sends ``beat`` and ``state`` to every connected client and answers ``clock``
pings. Clients send ``hello`` once on connect, ``clock`` pings, and ``task_event`` from the task
screen. Every message, both ways, is checked against the contract (bridge/contract.py) and
logged to the current session's file (bridge/logging.py).

Policy where the contract leaves the laptop to decide:

- The first message on a connection must be ``hello``, within HELLO_TIMEOUT_S. Until then the
  client receives nothing. A client on any ``v`` other than 1 is refused.
- Anything that breaks the contract is logged as received, then the connection is closed:
  1007 for a frame that is not JSON, 1003 for a binary frame, 1008 for everything else,
  including a second ``hello``. A broken client should fail loudly while it is being built.
- A beat that would leave with less than 300 ms before its t_play is not sent, and is logged
  as a ``beat_not_sent`` event. Rejected beats are exempt: they go out for logging and are
  never rendered.
- A client more than SLOW_CLIENT_BYTES behind is closed with 1013. It reconnects; until then
  it shows its disconnected marker, which is the contract's behaviour for a lost connection.

Run from the repo root as a module:  python -m bridge.server
"""

from __future__ import annotations

import sys

if __package__ in (None, ""):
    # Run by path, the bridge directory lands on sys.path and bridge/logging.py shadows the
    # standard library before any import below can fail with a clearer message.
    sys.exit("Run from the repo root as a module:  python -m bridge.server")

import argparse
import asyncio
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus

from websockets.asyncio.server import Server, ServerConnection, broadcast, serve
from websockets.exceptions import ConnectionClosed
from websockets.frames import CloseCode
from websockets.http11 import Request, Response

from bridge.clock import t_engine_ms
from bridge.contract import (
    MIN_LEAD_MS,
    PATH,
    PORT,
    VERSION,
    ContractError,
    DecodeError,
    decode,
    encode,
    validate,
)
from bridge.engine import EngineHost
from bridge.engine_feed import HeartbeatLevel, PsvFeed, SessionGain
from bridge.live import LiveLoop
from bridge.logging import SessionLog
from bridge.phase import PhaseTracker
from tools.synthetic_rr import DEFAULT_PROFILE_SPEC, Profile, SyntheticPacketSource

HELLO_TIMEOUT_S = 5.0
SLOW_CLIENT_BYTES = 64 * 1024  # about 40 s of backlog: far past any use to a client
MAX_FRAME_BYTES = 16 * 1024  # the largest contract message is under 1 KB
KEEPALIVE_S = 5.0  # a client that vanishes without closing is noticed within about 10 s


@dataclass
class Client:
    number: int
    connection: ServerConnection
    kind: str | None = None  # task-screen, spectator or quest, once hello arrives
    build: str | None = None

    @property
    def label(self) -> str:
        return f"c{self.number}/{self.kind}" if self.kind else f"c{self.number}"


@dataclass
class Stats:
    published: int = 0
    received: int = 0
    pongs: int = 0
    beats_not_sent: int = 0
    refused: int = 0
    too_slow: int = 0


class _Refuse(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        # A close reason is at most 123 bytes of UTF-8. Cut on bytes, never mid-character.
        self.reason = reason.encode("utf-8", "backslashreplace")[:123].decode("utf-8", "ignore")


class LiveServer:
    """Serve the link. Use from the event loop's thread; other threads use publish_threadsafe.

    ``on_task_event(msg, client)`` is called on the loop thread for each valid task event.
    """

    def __init__(
        self,
        log: SessionLog,
        *,
        host: str | None = None,  # None: every interface, IPv4 and IPv6
        port: int = PORT,
        clock: Callable[[], float] = t_engine_ms,
        on_task_event: Callable[[dict, Client], None] | None = None,
    ) -> None:
        if host is None and port == 0:
            # Every interface binds IPv4 and IPv6 separately, and each would pick its own port.
            raise ValueError("port 0 needs an explicit host")
        self.log = log
        self.host = host
        self.clock = clock
        self.on_task_event = on_task_event
        self.stats = Stats()
        self._port = port
        self._server: Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: dict[ServerConnection, Client] = {}  # past hello: these get the stream
        self._connections = 0
        self._closing: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._server = await serve(
            self._handle,
            self.host,
            self._port,
            process_request=self._check_path,
            max_size=MAX_FRAME_BYTES,
            ping_interval=KEEPALIVE_S,
            ping_timeout=KEEPALIVE_S,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def __aenter__(self) -> LiveServer:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    @property
    def port(self) -> int:
        ports = {sock.getsockname()[1] for sock in self._server.sockets}
        if len(ports) != 1:
            raise RuntimeError(f"the listening sockets are on different ports: {sorted(ports)}")
        return ports.pop()

    @property
    def clients(self) -> list[Client]:
        return list(self._clients.values())

    async def wait_for_client(self, poll_s: float = 0.05) -> None:
        while not self._clients:
            await asyncio.sleep(poll_s)

    # --- laptop to client ---

    def publish(self, msg: dict) -> bool:
        """Send a beat or a state to every client past hello.

        Raises ContractError if msg breaks the contract, which is a bug in the caller.
        Returns False, having sent nothing, for a beat too close to its t_play to be of use.
        """
        validate(msg, "out")
        if msg["type"] == "clock":
            raise ContractError("a clock pong is only ever a reply to a ping")
        now = self.clock()
        if msg["type"] == "beat" and msg["quality"] != "rejected":
            headroom = msg["t_play"] - now
            if headroom < MIN_LEAD_MS:
                self.stats.beats_not_sent += 1
                self.log.event(
                    "beat_not_sent", t_engine=now, headroom_ms=round(headroom, 1), msg=msg
                )
                return False
        text = encode(msg)
        self.log.message("out", msg, t_engine=now)
        receivers = []
        for connection, client in list(self._clients.items()):
            if connection.transport.get_write_buffer_size() > SLOW_CLIENT_BYTES:
                self._close_slow(client)
            else:
                receivers.append(connection)
        broadcast(receivers, text)
        self.stats.published += 1
        return True

    def publish_threadsafe(self, msg: dict) -> None:
        """publish from another thread. Contract errors raise here, in the calling thread."""
        validate(msg, "out")
        self._loop.call_soon_threadsafe(self.publish, msg)

    def _close_slow(self, client: Client) -> None:
        self._clients.pop(client.connection, None)
        self.stats.too_slow += 1
        code, reason = CloseCode.TRY_AGAIN_LATER, "too far behind the stream"
        self.log.event("closed", client=client.label, code=int(code), reason=reason)
        task = asyncio.ensure_future(client.connection.close(code, reason))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    # --- client to laptop ---

    def _check_path(self, connection: ServerConnection, request: Request) -> Response | None:
        if request.path.split("?", 1)[0] != PATH:
            return connection.respond(HTTPStatus.NOT_FOUND, f"The link is at {PATH}\n")
        return None

    async def _handle(self, connection: ServerConnection) -> None:
        self._connections += 1
        client = Client(self._connections, connection)
        self.log.event("connected", client=client.label)
        try:
            await self._hello(client)
            self._clients[connection] = client
            async for data in connection:
                await self._on_message(client, data)
        except _Refuse as refusal:
            self.stats.refused += 1
            await connection.close(refusal.code, refusal.reason)
        except ConnectionClosed:
            pass
        except Exception as error:
            # websockets closes with 1011 and prints the traceback; the log keeps the cause.
            self.log.event("handler_failed", client=client.label, error=repr(error))
            raise
        finally:
            self._clients.pop(connection, None)
            # Both sides: websockets fails a connection itself (keepalive timeout 1011, an
            # oversized frame 1009) without waiting for the peer's close, which then reads 1006.
            sent, rcvd = connection.protocol.close_sent, connection.protocol.close_rcvd
            self.log.event(
                "disconnected",
                client=client.label,
                sent_code=sent and sent.code,
                sent_reason=sent and sent.reason,
                rcvd_code=rcvd and rcvd.code,
                rcvd_reason=rcvd and rcvd.reason,
            )

    async def _hello(self, client: Client) -> None:
        try:
            async with asyncio.timeout(HELLO_TIMEOUT_S):
                data = await client.connection.recv()
        except TimeoutError:
            raise _Refuse(CloseCode.POLICY_VIOLATION, "send hello first") from None
        t = self.clock()
        msg = self._parse(client, data, t)
        if msg["type"] != "hello":
            self.log.message("in", msg, client.label, t)
            raise _Refuse(CloseCode.POLICY_VIOLATION, "the first message must be hello")
        client.kind, client.build = msg["client"], msg["build"]
        self.log.message("in", msg, client.label, t)

    async def _on_message(self, client: Client, data: str | bytes) -> None:
        t = self.clock()
        msg = self._parse(client, data, t)
        if msg["type"] == "clock":
            # Stamp and reply before anything else. Time spent here before the reply is
            # half-counted as network delay by the client's offset arithmetic.
            pong = {
                "type": "clock",
                "v": VERSION,
                "role": "pong",
                "t_client_sent": msg["t_client_sent"],
                "t_engine": round(self.clock()),
            }
            try:
                await client.connection.send(encode(pong))
            finally:  # a client that closes right after pinging still has its ping logged
                self.log.message("in", msg, client.label, t)
            self.stats.pongs += 1
            self.log.message("out", pong, client.label, pong["t_engine"])
            return
        self.log.message("in", msg, client.label, t)
        if msg["type"] == "hello":
            raise _Refuse(CloseCode.POLICY_VIOLATION, "hello is sent once, on connect")
        if self.on_task_event is not None:
            try:
                self.on_task_event(msg, client)
            except Exception as error:  # a handler bug must not drop the task screen
                self.log.event("task_event_handler_failed", client=client.label, error=repr(error))

    def _parse(self, client: Client, data: str | bytes, t: float) -> dict:
        self.stats.received += 1
        if not isinstance(data, str):
            self.log.raw(f"<binary frame, {len(data)} bytes>", "binary frame", client.label, t)
            raise _Refuse(CloseCode.UNSUPPORTED_DATA, "text frames only")
        try:
            msg = decode(data)
            validate(msg, "in")
        except ContractError as error:
            self.log.raw(data, str(error), client.label, t)
            code = (
                CloseCode.INVALID_DATA
                if isinstance(error, DecodeError)
                else CloseCode.POLICY_VIOLATION
            )
            raise _Refuse(code, str(error)) from None
        return msg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the live bridge, synthetic armband, audio engine and WebSocket link."
    )
    parser.add_argument("--host", default=None, help="default: every interface")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument("--scene", default="assets/scenes.json")
    parser.add_argument("--profile", default=DEFAULT_PROFILE_SPEC)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--one-session",
        action="store_true",
        help="start when the synthetic signal is ready, print live timing metrics, then exit",
    )
    args = parser.parse_args(argv)

    async def run() -> None:
        log = SessionLog(args.log_dir)
        host = EngineHost()
        host.open(args.scene)
        assert host.engine is not None and host.shim is not None
        phase = PhaseTracker.for_shim(host.shim)
        feed = PsvFeed(host.engine, log, phase=phase)
        gain = SessionGain(host.shim, log)
        heartbeat = HeartbeatLevel(host.shim, log)

        # Prompt 2.8 changes this one construction line to BlePacketSource(...).  Both sources
        # asynchronously yield each raw HRM characteristic value once.
        packet_source = SyntheticPacketSource(Profile.from_spec(args.profile), seed=args.seed)

        def task_event(msg: dict, client: Client) -> None:
            print(f"{client.label}: task_event {msg['event']}")

        server = LiveServer(log, host=args.host, port=args.port, on_task_event=task_event)
        bridge = LiveLoop(
            packet_source,
            server.publish,
            log,
            psv_feed=feed,
            session_gain=gain,
            heartbeat=heartbeat,
            beat_sink=host.shim,
            phase=phase,
        )
        log.event("engine_scene_loaded", elapsed_ms=host.scene_load_ms, manifest=args.scene)
        started = False
        runtime: asyncio.Task | None = None
        control: asyncio.Task | None = None
        try:
            host.start(gain, heartbeat)
            started = True
            async with server:
                print(
                    f"ws://{args.host or 'localhost'}:{server.port}{PATH}  "
                    f"session {bridge.session.session}"
                )
                runtime = asyncio.create_task(bridge.run(), name="live bridge")
                await asyncio.sleep(0)  # let LiveLoop establish ownership before local controls
                control = asyncio.create_task(
                    _one_session(bridge) if args.one_session else _console(bridge),
                    name="local attendant",
                )
                done, _ = await asyncio.wait(
                    (runtime, control), return_when=asyncio.FIRST_COMPLETED
                )
                if runtime in done:
                    await runtime
                else:
                    await control
        finally:
            for task in (control, runtime):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (control, runtime) if task is not None),
                return_exceptions=True,
            )
            try:
                if started:
                    await host.stop(gain, heartbeat)
            finally:
                host.close()
                log.close()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


async def _one_session(bridge: LiveLoop) -> None:
    await bridge.wait_for_signal()
    session = bridge.session.session
    refusal = await bridge.attendant_start()
    if refusal is not None:
        raise RuntimeError(f"automatic start was refused: {refusal}")
    metrics = await bridge.wait_for_completion(session)
    print(json.dumps(metrics.as_dict(), indent=2))


async def _console(bridge: LiveLoop) -> None:
    """Temporary local attendant control until prompt 3.6.  It is never a link client."""
    try:
        import msvcrt
    except ImportError:  # pragma: no cover - the booth and supported runtime are Windows
        print("Local keys need Windows; Ctrl+C stops the bridge.")
        await asyncio.Future()
        return

    print("Local keys: Space/Enter start, X stop, B body, P pose, Q quit")
    starts: set[asyncio.Task] = set()
    errors: list[Exception] = []

    async def start() -> None:
        refusal = await bridge.attendant_start()
        print("started" if refusal is None else f"start refused: {refusal}")

    def start_finished(task: asyncio.Task) -> None:
        starts.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as error:
            errors.append(error)

    try:
        while True:
            if errors:
                raise errors[0]
            if not msvcrt.kbhit():
                await asyncio.sleep(0.03)
                continue
            key = msvcrt.getwch().lower()
            if key in (" ", "\r"):
                task = asyncio.create_task(start(), name="armed attendant start")
                starts.add(task)
                task.add_done_callback(start_finished)
            elif key == "x":
                print("stopped" if bridge.attendant_stop() else "nothing running")
            elif key in ("b", "p"):
                source = "body" if key == "b" else "pose"
                bridge.set_psv_source(source)
                print(f"PSV source: {source}")
            elif key == "q":
                return
    finally:
        for task in starts:
            task.cancel()
        await asyncio.gather(*starts, return_exceptions=True)


if __name__ == "__main__":
    sys.exit(main())
