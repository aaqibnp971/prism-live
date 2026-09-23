"""Memory-only local MQTT broker with one explicitly allowlisted phone ingress.

The LAN listener is a byte relay; the broker itself never binds a LAN interface.
Start the local broker, subscribe the source, then open LAN ingress. It lives for
the bridge process, not a visitor session. No persistence, cloud, or TLS here:
use the project's trusted travel router, never a venue network.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket

PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def private_ipv4(value: str) -> str:
    address = ipaddress.ip_address(value)
    if address.version != 4 or not any(address in net for net in PRIVATE_NETWORKS):
        raise ValueError("MQTT requires an explicit RFC1918 IPv4 address on your own router")
    return str(address)


class MqttBroker:
    def __init__(self, bind_host: str, phone_ip: str, *, port=1883, broker_port=1884):
        self.bind_host = private_ipv4(bind_host)
        self.phone_ip = private_ipv4(phone_ip)
        if self.bind_host == self.phone_ip:
            raise ValueError("Laptop and phone must have different fixed addresses")
        if not all(1024 <= value <= 65535 for value in (port, broker_port)):
            raise ValueError("MQTT ports must be between 1024 and 65535")
        if port == broker_port:
            raise ValueError("MQTT LAN and loopback ports must differ")
        self.port, self.broker_port = port, broker_port
        self.broker = None
        self.server = None
        self.tasks: set[asyncio.Task] = set()
        self.phone_connections = 0
        self.rejected_connections = 0
        self.closing = False

    async def start_local(self):
        if self.broker is not None or self.closing:
            raise RuntimeError("MQTT broker cannot be started twice")
        try:
            from amqtt.broker import Broker
        except ImportError as error:
            raise RuntimeError(
                "MQTT dependencies missing; install the project's mqtt extra"
            ) from error
        self.broker = Broker(
            {
                "listeners": {
                    "default": {
                        "type": "tcp",
                        "bind": f"127.0.0.1:{self.broker_port}",
                        "max_connections": 8,
                    }
                },
                "plugins": {
                    "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}
                },
                "timeout_disconnect_delay": 0,
                "session_expiry_interval": 0,
            }
        )
        await self.broker.start()

    async def open_lan(self):
        if self.broker is None or self.server is not None or self.closing:
            raise RuntimeError("Start local MQTT and subscribe before opening LAN ingress")
        self.server = await asyncio.start_server(
            self._relay, self.bind_host, self.port, family=socket.AF_INET
        )

    async def _relay(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream_writer = None
        transfers = []
        peer = writer.get_extra_info("peername")
        accepted = False
        try:
            if self.closing or not peer or peer[0] != self.phone_ip or self.phone_connections >= 2:
                self.rejected_connections += 1
                return
            # Allow a reconnect while the old phone TCP connection is closing, but not
            # unbounded sockets from even the approved address.
            accepted = True
            self.phone_connections += 1
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.broker_port), 3
            )

            async def copy(source, destination):
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()

            transfers = [
                asyncio.create_task(copy(reader, upstream_writer)),
                asyncio.create_task(copy(upstream_reader, writer)),
            ]
            completed, _pending = await asyncio.wait(transfers, return_when=asyncio.FIRST_COMPLETED)
            for finished in completed:
                finished.result()
        except (ConnectionError, OSError, TimeoutError):
            # A phone reconnect must not crash the bridge or reset a visitor.
            pass
        finally:
            for transfer in transfers:
                transfer.cancel()
            await asyncio.gather(*transfers, return_exceptions=True)
            for stream in (upstream_writer, writer):
                if stream is not None:
                    stream.close()
                    with contextlib.suppress(ConnectionError, OSError, TimeoutError):
                        await asyncio.wait_for(stream.wait_closed(), 1)
            if accepted:
                self.phone_connections -= 1
            self.tasks.discard(task)

    async def close(self):
        self.closing = True
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        pending = tuple(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if self.broker is not None:
            broker, self.broker = self.broker, None
            await broker.shutdown()
