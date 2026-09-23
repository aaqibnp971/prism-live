"""Broker ownership/relay safety: no LAN listeners or real device used by these checks."""

import asyncio
from types import SimpleNamespace

import pytest

from bridge.mqtt_broker import MqttBroker


@pytest.mark.parametrize(
    "local,phone",
    [
        ("0.0.0.0", "192.168.1.135"),
        ("192.168.1.201", "8.8.8.8"),
        ("192.168.1.201", "192.168.1.201"),
    ],
)
def test_broker_refuses_unsafe_scope(local, phone):
    with pytest.raises(ValueError):
        MqttBroker(local, phone)


def test_lan_relay_cannot_open_before_local_broker():
    async def check():
        broker = MqttBroker("192.168.1.201", "192.168.1.135")
        with pytest.raises(RuntimeError, match="Start local"):
            await broker.open_lan()
        await broker.close()

    asyncio.run(check())


def test_relay_rejects_foreign_phone_before_opening_upstream(monkeypatch):
    async def check():
        broker = MqttBroker("192.168.1.201", "192.168.1.135")
        closed = []

        async def wait_closed():
            pass

        async def forbidden_connection(*args, **kwargs):
            raise AssertionError("Foreign peer must not reach the broker")

        monkeypatch.setattr(asyncio, "open_connection", forbidden_connection)
        writer = SimpleNamespace(
            get_extra_info=lambda _: ("192.168.1.136", 5000),
            close=lambda: closed.append(True),
            wait_closed=wait_closed,
        )
        await broker._relay(None, writer)
        assert closed and broker.rejected_connections == 1
        assert broker.phone_connections == 0 and not broker.tasks

    asyncio.run(check())


def test_close_stops_ingress_then_phone_connections_then_broker():
    async def check():
        broker = MqttBroker("192.168.1.201", "192.168.1.135")
        events = []

        async def wait_closed():
            events.append("listener closed")

        async def shutdown():
            events.append("broker closed")

        async def pending_connection():
            try:
                await asyncio.Event().wait()
            finally:
                events.append("phone closed")

        task = asyncio.create_task(pending_connection())
        await asyncio.sleep(0)
        broker.tasks.add(task)
        broker.server = SimpleNamespace(
            close=lambda: events.append("stop ingress"), wait_closed=wait_closed
        )
        broker.broker = SimpleNamespace(shutdown=shutdown)
        await broker.close()
        await broker.close()
        assert events == ["stop ingress", "listener closed", "phone closed", "broker closed"]

    asyncio.run(check())
