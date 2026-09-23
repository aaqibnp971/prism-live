"""Transport self-checks: no real armband, LAN access or installed service."""

import asyncio
import importlib.util
import json
import socket

import pytest

from bridge.mqtt_source import (
    BURST_QUIET_MS,
    FRESH_MS,
    MAX_BURST_SAMPLES,
    MAX_DEDUP_KEYS,
    MAX_MAILBOX_MESSAGES,
    MqttConfig,
    MqttSource,
    PpiDecoder,
)
from tests.synthetic_ppi_capture import synthetic_records

BASE_TIME = 1893456000000  # Invented 2030 epoch; never a captured publication timestamp.
SESSION_ID = 42
TOPIC = "psl/prism-probe/ecg"


def payload(timestamp=BASE_TIME, session=SESSION_ID, samples=None, **extra):
    data = {
        "clientId": "verity-phone",
        "deviceId": "1967873D",
        "sessionId": session,
        "timeStamp": timestamp,
        "ppi": samples
        if samples is not None
        else [
            {
                "ppi": 800,
                "errorEstimate": 8,
                "blockerBit": False,
                "skinContactStatus": True,
                "hr": 75,
            },
        ],
    }
    data.update(extra)
    return json.dumps(data, allow_nan=True).encode()


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


def source_ready():
    clock = Clock()
    source = MqttSource(clock=clock)
    source.connected = True
    return source, clock


def deliver(source, clock, when, data=None, *, topic=TOPIC, retained=False):
    clock.value = when
    source._mailbox.append(
        ("message", topic, data or payload(BASE_TIME + int(when)), when, retained)
    )
    return source._drain()


def begin_live(source, clock):
    assert deliver(source, clock, 0) == []  # The unknown-age first burst is quarantined.
    assert deliver(source, clock, 5000) == []
    clock.value += BURST_QUIET_MS
    packets = source._drain()
    assert len(packets) == 1
    return packets[0]


def test_synthetic_capture_decoded_without_wear_inference():
    decoder = PpiDecoder(MqttConfig())
    messages = []
    expected_samples = 0
    for record in synthetic_records():
        if record.get("topic") != TOPIC:
            continue
        expected_samples += len(record["json"]["ppi"])
        result = decoder.decode(
            TOPIC, record["payload_utf8"].encode(), record["perf_counter_ns"] / 1e6
        )
        if result:
            messages.append(result)
    assert expected_samples > 100
    assert sum(len(message.samples) for message in messages) == expected_samples
    assert any(sample.blocked for message in messages for sample in message.samples)
    assert any(
        sample.reported_contact is False for message in messages for sample in message.samples
    )
    assert any(sample.reported_hr == 0 for message in messages for sample in message.samples)


def test_preserves_bad_quality_for_scheduler_instead_of_collapsing_timeline():
    samples = [
        {"ppi": 200, "errorEstimate": 800, "blockerBit": True, "hr": 0, "skinContactStatus": False},
        {"ppi": 900, "errorEstimate": 0, "blockerBit": False, "hr": 150, "skinContactStatus": True},
    ]
    message = PpiDecoder(MqttConfig()).decode(TOPIC, payload(samples=samples), 123.5)
    assert [sample.rr_ms for sample in message.samples] == [200, 900]
    assert message.samples[0].error_ms == 800
    assert message.samples[0].blocked is True
    assert message.arrived_ms == 123.5


@pytest.mark.parametrize(
    "field,value",
    [
        ("ppi", True),
        ("ppi", float("nan")),
        ("ppi", "800"),
        ("errorEstimate", float("inf")),
        ("errorEstimate", None),
        ("blockerBit", 1),
        ("blockerBit", "false"),
        ("skinContactStatus", 1),
        ("hr", False),
    ],
)
def test_malformed_sample_rejects_whole_message(field, value):
    sample = {"ppi": 800, "errorEstimate": 8, "blockerBit": False}
    sample[field] = value
    decoder = PpiDecoder(MqttConfig())
    assert decoder.decode(TOPIC, payload(samples=[sample]), 0) is None
    assert decoder.counters["malformed"] == 1


@pytest.mark.parametrize("field", ["ppi", "errorEstimate", "blockerBit"])
def test_missing_quality_is_not_invented(field):
    sample = {"ppi": 800, "errorEstimate": 8, "blockerBit": False}
    del sample[field]
    assert PpiDecoder(MqttConfig()).decode(TOPIC, payload(samples=[sample]), 0) is None


def test_optional_diagnostics_missing_are_none():
    sample = {"ppi": 800, "errorEstimate": 8, "blockerBit": False}
    message = PpiDecoder(MqttConfig()).decode(TOPIC, payload(samples=[sample]), 0)
    assert message.samples[0].reported_contact is None
    assert message.samples[0].reported_hr is None


@pytest.mark.parametrize("extra", [{"clientId": "other"}, {"deviceId": "other"}])
def test_identity_is_fixed_not_first_sender_wins(extra):
    decoder = PpiDecoder(MqttConfig())
    assert decoder.decode(TOPIC, payload(**extra), 0) is None
    assert decoder.counters["wrong_identity"] == 1


def test_hr_is_diagnostic_only_and_retained_never_counts():
    source, clock = source_ready()
    assert deliver(source, clock, 0, payload(hr=120), topic="psl/prism-probe/hr") == []
    assert source.last_message_ms == 0
    assert source.decoder.reported_hr == 120
    assert source.last_ppi_ms is None
    assert source.status == "waiting_for_ppi"
    assert deliver(source, clock, 5000, retained=True) == []
    assert source._probation is None
    assert not source.ready


def test_wrong_topic_not_treated_as_ppi_even_with_matching_json():
    decoder = PpiDecoder(MqttConfig())
    for topic in ("psl/prism-probe/ppi", "other/ecg", "psl/prism-probe/ecg/extra"):
        assert decoder.decode(topic, payload(), 0) is None
    assert decoder.counters["wrong_topic"] == 3


def test_duplicates_bounded_and_same_timestamp_fragments_preserved():
    decoder = PpiDecoder(MqttConfig())
    assert decoder.decode(TOPIC, payload(), 0)
    assert decoder.decode(TOPIC, payload(), 1) is None
    assert decoder.counters["duplicate"] == 1
    assert decoder.decode(
        TOPIC, payload(samples=[{"ppi": 790, "errorEstimate": 1, "blockerBit": False}]), 2
    )
    for index in range(MAX_DEDUP_KEYS + 20):
        assert decoder.decode(TOPIC, payload(BASE_TIME + index + 1), index + 10)
    assert len(decoder._seen) == MAX_DEDUP_KEYS
    assert (
        decoder.decode(TOPIC, payload(), 99999) is None
    )  # Old watermark, even after LRU eviction.


def test_old_session_and_phone_clock_regression_rejected_not_retimed():
    decoder = PpiDecoder(MqttConfig())
    assert decoder.decode(TOPIC, payload(session=1), 100)
    assert decoder.decode(TOPIC, payload(BASE_TIME + 1, session=2), 200)
    assert decoder.decode(TOPIC, payload(BASE_TIME + 2, session=1), 300) is None
    assert decoder.decode(TOPIC, payload(BASE_TIME - 100, session=2), 400) is None
    assert decoder.decode(TOPIC, payload(BASE_TIME + 2, session=2), 500)


def test_probation_burst_aggregation_and_callback_arrival_not_flush_time():
    source, clock = source_ready()
    deliver(source, clock, 0)
    assert not source.ready
    deliver(source, clock, 5244)
    deliver(source, clock, 5290)
    clock.value = 5493
    assert source._drain() == []
    clock.value = 5540
    packets = source._drain()
    assert len(packets) == 1
    assert len(packets[0].samples) == 2
    assert packets[0].arrived_ms == 5290  # Callback, not 5540 flush time.
    assert packets[0].discontinuity is True
    assert packets[0].source_id == f"verity-phone/1967873D/{SESSION_ID}"
    assert source.ready
    assert source.status == "flowing"
    deliver(source, clock, 10244)
    clock.value += BURST_QUIET_MS
    assert source._drain()[0].discontinuity is False


def test_hr_does_not_hide_stale_ppi_or_reestablish_ready():
    source, clock = source_ready()
    begin_live(source, clock)
    deliver(source, clock, 5000 + FRESH_MS + 1, payload(hr=100), topic="psl/prism-probe/hr")
    assert source.status == "stale"
    assert not source.ready
    assert source.last_ppi_ms == 5000


def test_initial_backlog_is_not_mistaken_for_live_progression():
    source, clock = source_ready()
    deliver(source, clock, 0)
    deliver(source, clock, 100, payload(BASE_TIME + 5000))
    deliver(source, clock, 200, payload(BASE_TIME + 10000))
    deliver(source, clock, 3000, payload(BASE_TIME + 15000))
    clock.value = 3500
    assert source._drain() == []
    assert not source.ready


def test_reconnect_keeps_watermark_and_quarantines_then_discontinues():
    source, clock = source_ready()
    begin_live(source, clock)
    source._mailbox.extend([("disconnected",), ("connected",)])
    source._drain()
    assert not source.ready
    deliver(source, clock, 7000, payload(BASE_TIME + 4000))  # Old queued publication.
    deliver(source, clock, 10000)
    deliver(source, clock, 15000)
    clock.value += BURST_QUIET_MS
    packet = source._drain()[0]
    assert packet.discontinuity
    assert packet.arrived_ms == 15000


def test_changed_app_session_discards_pending_old_burst():
    source, clock = source_ready()
    begin_live(source, clock)
    deliver(source, clock, 10000)
    deliver(source, clock, 10300, payload(BASE_TIME + 10300, session=2))
    clock.value = 10600
    assert source._drain() == []
    assert not source.ready
    deliver(source, clock, 15300, payload(BASE_TIME + 15300, session=2))
    clock.value += BURST_QUIET_MS
    packet = source._drain()[0]
    assert packet.source_id.endswith("/2")
    assert packet.discontinuity


def test_callback_queue_stall_is_not_new_arrival():
    source, clock = source_ready()
    begin_live(source, clock)
    source._mailbox.append(("message", TOPIC, payload(BASE_TIME + 10000), 10000, False))
    clock.value = 15000
    assert source._drain() == []
    assert source.decoder.counters["stale_callback"] == 1
    assert not source.ready


def test_pending_burst_not_emitted_after_bridge_stall():
    source, clock = source_ready()
    begin_live(source, clock)
    deliver(source, clock, 10000)
    clock.value = 15000
    assert source._drain() == []
    assert source.decoder.counters["stale_burst"] == 1
    assert not source.ready


def test_slow_phone_backlog_discontinues_instead_of_replaying():
    source, clock = source_ready()
    begin_live(source, clock)
    deliver(source, clock, 10500, payload(BASE_TIME + 8000))
    clock.value += BURST_QUIET_MS
    assert source._drain() == []
    assert source.decoder.counters["phone_backlog"] == 1
    assert not source.ready


def test_malformed_ppi_discontinues_but_duplicate_does_not():
    source, clock = source_ready()
    begin_live(source, clock)
    deliver(source, clock, 5200, payload(BASE_TIME + 5000))
    assert source.ready
    deliver(source, clock, 5300, payload(BASE_TIME + 5300, samples=[{"ppi": 800}]))
    assert not source.ready


def test_burst_overflow_is_bounded_discontinuity():
    source, clock = source_ready()
    begin_live(source, clock)
    samples = [{"ppi": 800, "errorEstimate": 0, "blockerBit": False}] * 64
    deliver(source, clock, 10000, payload(BASE_TIME + 10000, samples=samples))
    deliver(source, clock, 10001, payload(BASE_TIME + 10001, samples=samples))
    assert len(source._samples) == MAX_BURST_SAMPLES
    deliver(source, clock, 10002)
    assert len(source._samples) == 0
    assert source.decoder.counters["burst_overflow"] == 1


def test_thread_mailbox_bounded_with_only_one_pending_wakeup():
    async def run():
        source, _clock = source_ready()
        source._loop = asyncio.get_running_loop()
        for _ in range(MAX_MAILBOX_MESSAGES * 3):
            source._post(("oversized",))
        assert len(source._mailbox) <= MAX_MAILBOX_MESSAGES
        assert source._wake_pending
        source._drain()
        assert source.decoder.counters["mailbox_overflow"] == 1
        assert not source._wake_pending
        await source.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "config",
    [
        {"broker_host": "192.168.1.1"},
        {"broker_port": 0},
        {"topic_prefix": "#"},
        {"client_id": ""},
        {"device_id": ""},
    ],
)
def test_configuration_explicit_and_local(config):
    with pytest.raises(ValueError):
        MqttConfig(**config)


def test_local_broker_lifecycle_and_actual_pubsub():
    if importlib.util.find_spec("amqtt") is None or importlib.util.find_spec("paho") is None:
        pytest.skip("Optional MQTT dependencies are not installed in this environment")

    async def run():
        import paho.mqtt.client as mqtt

        from bridge.mqtt_broker import MqttBroker

        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        # Only start_local is called: the configured LAN addresses never bind.
        broker = MqttBroker("192.168.1.201", "192.168.1.135", broker_port=port)
        source, clock = source_ready()
        source.config = MqttConfig(broker_port=port)
        source.decoder = PpiDecoder(source.config)
        publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
        received = []

        async def consume():
            async for packet in source:
                received.append(packet)

        async def wait_for(predicate):
            for _ in range(200):
                if predicate():
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("Timed out waiting for loopback MQTT")

        consumer = None
        try:
            await broker.start_local()
            await source.start()  # Returns after SUBACK, before phone/LAN listener opens.
            consumer = asyncio.create_task(consume())
            await asyncio.to_thread(publisher.connect, "127.0.0.1", port, 10)
            publisher.loop_start()
            publisher.publish(TOPIC, payload(), qos=1)
            await wait_for(lambda: source._probation is not None)
            assert not received
            clock.value = 5000
            publisher.publish(TOPIC, payload(BASE_TIME + 5000), qos=1)
            await wait_for(lambda: source.last_ppi_ms == 5000)
            clock.value = 5300
            await wait_for(lambda: len(received) == 1)
            assert received[0].samples[0].rr_ms == 800
            assert received[0].discontinuity
            assert source.ready
        finally:
            await source.close()
            if consumer is not None:
                await asyncio.wait_for(consumer, 2)
            publisher.disconnect()
            await asyncio.to_thread(publisher.loop_stop)
            await broker.close()
        assert source.status == "closed"
        assert not source.ready

    asyncio.run(run())
