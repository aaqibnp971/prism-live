"""Polar Sensor Logger's measured optical PPI, continuously between visitors.

The measured app publishes PPI on ``<prefix>/ecg`` despite that misleading name.
It is a JSON ``ppi`` array, NOT an ECG waveform and NOT standard BLE HRM bytes.
``errorEstimate`` and ``blockerBit`` remain available to the scheduler's quality
filter. Contact and HR are diagnostic only: both stayed positive off-arm in the
23 September capture. HR-only messages can never keep the PPI stream alive.

The phone's ``timeStamp`` was Unix milliseconds in that capture, not the app
FAQ's nanoseconds since 2000. It is used only for ordering and relative backlog
checks. It is never subtracted from laptop UTC to claim physical beat age.
Without acquisition timestamps, even these checks cannot prove freshness of an
otherwise steadily paced replay. A newly connected source first observes one
burst, then requires advancing phone/host clocks before becoming ready.

Paho owns one bounded callback mailbox. JSON parsing, aggregation and iteration
run on the bridge asyncio loop, not the audio callback or MQTT network thread.
The optional dependency is imported only by ``start``; synthetic mode needs none.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import math
import threading
import uuid
from collections import Counter, OrderedDict, deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from bridge.clock import t_engine_ms
from bridge.packets import PpiPacket, PpiSample

BURST_QUIET_MS = 250.0
BURST_MAX_MS = 750.0
FRESH_MS = 6200.0
MAX_CALLBACK_AGE_MS = 1500.0
MAX_PHONE_LAG_MS = 1500.0
PROBATION_MIN_MS = 2500.0
PROBATION_CLOCK_TOLERANCE_MS = 1000.0
MAX_PAYLOAD_BYTES = 65536
MAX_MESSAGE_SAMPLES = 64
MAX_BURST_SAMPLES = 128
MAX_MAILBOX_MESSAGES = 128
MAX_DEDUP_KEYS = 512


@dataclass(frozen=True)
class MqttConfig:
    broker_host: str = "127.0.0.1"
    broker_port: int = 1884
    topic_prefix: str = "psl/prism-probe"
    client_id: str = "verity-phone"  # Expected phone publisher, not our subscriber ID.
    device_id: str = "1967873D"

    def __post_init__(self) -> None:
        if not ipaddress.ip_address(self.broker_host).is_loopback:
            raise ValueError("The PPI subscriber connects only to the laptop's loopback broker")
        if not 1 <= self.broker_port <= 65535:
            raise ValueError("Invalid local broker port")
        if (
            not self.topic_prefix
            or len(self.topic_prefix) > 256
            or any(char in self.topic_prefix for char in "+#\x00")
            or self.topic_prefix.endswith("/")
        ):
            raise ValueError("Use an exact topic prefix, without MQTT wildcards")
        if any(not value or len(value) > 128 for value in (self.client_id, self.device_id)):
            raise ValueError("Expected phone client and sensor device identities are required")


@dataclass(frozen=True)
class PpiMessage:
    samples: tuple[PpiSample, ...]
    session_id: str
    timestamp_ms: int
    arrived_ms: float
    source_id: str


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-JSON number {value}")


class PpiDecoder:
    """Strict wire-shape validation and bounded duplicate/replay protection.

    No RR range, error threshold or wear inference belongs here. Invalid *quality*
    still reaches the scheduler, preserving every sample's place in the timeline.
    A malformed sample invalidates the message rather than silently shortening it.
    """

    def __init__(self, config: MqttConfig):
        self.config = config
        self.counters: Counter[str] = Counter()
        self.last_message_ms: float | None = None
        self.reported_hr: float | None = None
        self._seen: OrderedDict[tuple[str, int, bytes], None] = OrderedDict()
        self._session: str | None = None
        self._retired: deque[str] = deque(maxlen=32)
        self._timestamp: int | None = None

    def decode(
        self, topic: str, payload: bytes, arrived_ms: float, *, retained: bool = False
    ) -> PpiMessage | None:
        if retained:
            self.counters["retained"] += 1
            return None
        if len(payload) > MAX_PAYLOAD_BYTES:
            self.counters["oversized"] += 1
            return None
        kind = topic.removeprefix(self.config.topic_prefix + "/")
        if topic != self.config.topic_prefix + "/" + kind or kind not in ("hr", "ecg"):
            self.counters["wrong_topic"] += 1
            return None
        try:
            data = json.loads(payload, parse_constant=_reject_constant)
            if not isinstance(data, dict):
                raise ValueError("Payload must be an object")
            if (
                data.get("clientId") != self.config.client_id
                or data.get("deviceId") != self.config.device_id
            ):
                self.counters["wrong_identity"] += 1
                return None
            timestamp = data["timeStamp"]
            session = data["sessionId"]
            if type(timestamp) is not int or not 0 < timestamp < 2**63:
                raise ValueError("timeStamp must be a positive millisecond integer")
            if type(session) not in (str, int) or not str(session) or len(str(session)) > 128:
                raise ValueError("sessionId must be a bounded identifier")
            session = str(session)
            if kind == "hr":
                self.reported_hr = _number(data["hr"], "hr")
                self.last_message_ms = arrived_ms
                self.counters["hr"] += 1
                return None
            raw_samples = data["ppi"]
            if (
                not isinstance(raw_samples, list)
                or not 1 <= len(raw_samples) <= MAX_MESSAGE_SAMPLES
            ):
                raise ValueError("ppi must be a bounded, nonempty array")
            samples = []
            for raw in raw_samples:
                if not isinstance(raw, dict) or type(raw.get("blockerBit")) is not bool:
                    raise ValueError("Every sample needs a boolean blockerBit")
                contact = raw.get("skinContactStatus")
                if contact is not None and type(contact) is not bool:
                    raise ValueError("skinContactStatus, when present, must be boolean")
                reported_hr = None if "hr" not in raw else _number(raw["hr"], "hr")
                interval = _number(raw["ppi"], "ppi")
                error = _number(raw["errorEstimate"], "errorEstimate")
                if not 1 <= interval <= 60_000 or error < 0:
                    raise ValueError("PPI must be 1..60000 ms; errorEstimate must be nonnegative")
                samples.append(
                    PpiSample(
                        interval,
                        error,
                        raw["blockerBit"],
                        contact,
                        reported_hr,
                    )
                )
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
            self.counters["malformed"] += 1
            return None
        key = (session, timestamp, hashlib.sha256(payload).digest())
        if key in self._seen:
            self.counters["duplicate"] += 1
            return None
        if session in self._retired or (
            self._timestamp is not None and timestamp < self._timestamp
        ):
            self.counters["old_message"] += 1
            return None
        if self._session is not None and session != self._session:
            # A new app run must advance the publication clock. Same-timestamp
            # fragments remain legal only within the same app session.
            if timestamp == self._timestamp:
                self.counters["old_message"] += 1
                return None
            self._retired.append(self._session)
        self._session = session
        self._timestamp = timestamp
        self._seen[key] = None
        while len(self._seen) > MAX_DEDUP_KEYS:
            self._seen.popitem(last=False)
        self.last_message_ms = arrived_ms
        self.counters["ppi_messages"] += 1
        return PpiMessage(
            tuple(samples),
            session,
            timestamp,
            arrived_ms,
            f"{self.config.client_id}/{self.config.device_id}/{session}",
        )


class MqttSource:
    """One async iterable for the lifetime of a bridge process, not a session.

    ``ready`` means fresh, non-retained PPI transport, NOT accepted intervals or
    confirmed wear. Session/scheduler quality gates remain authoritative.
    ``connected`` means subscribed to the LOCAL broker, not connected to a phone.
    """

    ppi_bursts = True

    def __init__(
        self, config: MqttConfig | None = None, *, clock: Callable[[], float] = t_engine_ms
    ):
        self.config = config or MqttConfig()
        self.clock = clock
        self.decoder = PpiDecoder(self.config)
        self.connected = False
        self.last_ppi_ms: float | None = None
        self._closed = False
        self._started = False
        self._iterating = False
        self._client = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._subscription_ready = asyncio.Event()
        self._lock = threading.Lock()
        self._mailbox: deque[tuple] = deque()
        self._wake_pending = False
        self._overflowed = False
        self._samples: list[PpiSample] = []
        self._burst_first: float | None = None
        self._burst_last: float | None = None
        self._burst_source: str | None = None
        self._discontinuous = True
        self._probation: PpiMessage | None = None
        self._live = False
        self._min_offset: float | None = None
        self._last_received: float | None = None

    @property
    def last_message_ms(self) -> float | None:
        return self.decoder.last_message_ms

    @property
    def ready(self) -> bool:
        return (
            self.connected
            and self._live
            and self.last_ppi_ms is not None
            and 0 <= self.clock() - self.last_ppi_ms <= FRESH_MS
        )

    @property
    def status(self) -> str:
        if self._closed:
            return "closed"
        if not self.connected:
            return "connecting" if not self._started else "disconnected"
        if self.ready:
            return "flowing"
        if self.last_ppi_ms is not None and self.clock() - self.last_ppi_ms > FRESH_MS:
            return "stale"
        return "waiting_for_ppi"

    def _post(self, item: tuple) -> None:
        """The only network-thread crossing, including bounded wakeup scheduling."""
        if self._closed or self._loop is None:
            return
        with self._lock:
            if len(self._mailbox) >= MAX_MAILBOX_MESSAGES:
                self._mailbox.clear()
                self._overflowed = True
            self._mailbox.append(item)
            if not self._wake_pending:
                self._wake_pending = True
                self._loop.call_soon_threadsafe(self._wake.set)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            self._post(("disconnected",))
            return
        result, _mid = client.subscribe(
            [(self.config.topic_prefix + "/ecg", 2), (self.config.topic_prefix + "/hr", 2)]
        )
        if result != 0:
            self._post(("disconnected",))

    def _on_subscribe(self, client, userdata, mid, reasons, properties) -> None:
        connected = len(reasons) == 2 and not any(reason.is_failure for reason in reasons)
        self._post(("connected" if connected else "disconnected",))
        if connected and self._loop is not None and not self._closed:
            self._loop.call_soon_threadsafe(self._subscription_ready.set)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self._post(("disconnected",))

    def _on_message(self, client, userdata, message) -> None:
        # Preserve callback arrival even if the asyncio loop stalls. Do not turn
        # stale queued data into fresh data merely by draining it after a freeze.
        now = self.clock()
        if len(message.payload) > MAX_PAYLOAD_BYTES:
            self._post(("oversized",))
            return
        self._post(("message", message.topic, bytes(message.payload), now, bool(message.retain)))

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Cannot restart a closed MQTT source")
        if self._started:
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError as error:
            raise RuntimeError(
                "MQTT mode requires the project's optional [mqtt] dependencies"
            ) from error
        self._loop = asyncio.get_running_loop()
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"prism-ppi-{uuid.uuid4().hex}",
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )
        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=5)
        self._client.max_queued_messages_set(MAX_MAILBOX_MESSAGES)
        self._client.connect_async(self.config.broker_host, self.config.broker_port, keepalive=10)
        self._client.loop_start()
        self._started = True
        try:
            # The launcher opens the LAN relay only after this subscription is
            # installed, so startup cannot race the phone's first publication.
            await asyncio.wait_for(self._subscription_ready.wait(), timeout=10)
        except BaseException:
            await self.close()
            raise

    def _reset(self) -> None:
        self._samples.clear()
        self._burst_first = self._burst_last = None
        self._burst_source = None
        self._discontinuous = True
        self._probation = None
        self._live = False
        # Keep the best observed phone-to-host relative offset and decoder
        # watermark across broker reconnects. Replaying backlog cannot reset it.

    def _accept(self, message: PpiMessage) -> None:
        now = message.arrived_ms
        if self._burst_source not in (None, message.source_id):
            self._reset()
            self._min_offset = None  # New app session may have reset its wall clock.
        if self._last_received is not None and now - self._last_received > FRESH_MS:
            self._reset()
        self._last_received = now
        offset = now - message.timestamp_ms
        if self._min_offset is not None and offset > self._min_offset + MAX_PHONE_LAG_MS:
            self.decoder.counters["phone_backlog"] += 1
            self._reset()
            return
        self._min_offset = offset if self._min_offset is None else min(self._min_offset, offset)
        if not self._live:
            if self._probation is None:
                self._probation = message
                self._burst_source = message.source_id
                self.decoder.counters["probation"] += len(message.samples)
                return
            host_delta = now - self._probation.arrived_ms
            phone_delta = message.timestamp_ms - self._probation.timestamp_ms
            if (
                host_delta < PROBATION_MIN_MS
                or abs(phone_delta - host_delta) > PROBATION_CLOCK_TOLERANCE_MS
            ):
                self.decoder.counters["probation"] += len(message.samples)
                if host_delta >= PROBATION_MIN_MS:
                    # A burst of old phone-side queued messages may have just
                    # caught up. Start a new observation window, never replay it.
                    self._probation = message
                return
            self._live = True
        if len(self._samples) + len(message.samples) > MAX_BURST_SAMPLES:
            self.decoder.counters["burst_overflow"] += 1
            self._reset()
            return
        self.last_ppi_ms = now
        self._burst_source = message.source_id
        if self._burst_first is None:
            self._burst_first = now
        self._burst_last = now
        self._samples.extend(message.samples)

    def _take_packet(self) -> PpiPacket | None:
        if not self._samples:
            return None
        assert self._burst_source is not None and self._burst_last is not None
        if self.clock() - self._burst_last > MAX_CALLBACK_AGE_MS:
            self.decoder.counters["stale_burst"] += 1
            self._reset()
            return None
        packet = PpiPacket(
            tuple(self._samples), self._burst_source, self._burst_last, self._discontinuous
        )
        self._samples.clear()
        self._burst_first = self._burst_last = None
        self._discontinuous = False
        return packet

    def _drain(self) -> list[PpiPacket]:
        with self._lock:
            items = tuple(self._mailbox)
            self._mailbox.clear()
            overflowed = self._overflowed
            self._overflowed = False
            self._wake_pending = False
            self._wake.clear()
        if overflowed:
            self.decoder.counters["mailbox_overflow"] += 1
            self._reset()
        packets = []
        for item in items:
            kind = item[0]
            if kind in ("connected", "disconnected"):
                self.connected = kind == "connected"
                self._reset()
            elif kind == "oversized":
                self.decoder.counters["oversized"] += 1
                self._reset()
            elif kind == "message":
                _, topic, payload, arrived, retained = item
                if not self.connected:
                    continue
                if self.clock() - arrived > MAX_CALLBACK_AGE_MS:
                    self.decoder.counters["stale_callback"] += 1
                    self._reset()
                    continue
                malformed_before = self.decoder.counters["malformed"]
                message = self.decoder.decode(topic, payload, arrived, retained=retained)
                if message is None:
                    # Losing unknown intervals is a discontinuity. Duplicates
                    # and retained messages are ignored without interrupting it.
                    if (
                        topic == self.config.topic_prefix + "/ecg"
                        and self.decoder.counters["malformed"] > malformed_before
                    ):
                        self._reset()
                    continue
                if self._burst_source not in (None, message.source_id):
                    self._reset()
                    self._min_offset = None
                if self._burst_last is not None and (
                    arrived - self._burst_last >= BURST_QUIET_MS
                    or arrived - self._burst_first >= BURST_MAX_MS
                ):
                    packet = self._take_packet()
                    if packet is not None:
                        packets.append(packet)
                self._accept(message)
        if self._burst_last is not None and (
            self.clock() - self._burst_last >= BURST_QUIET_MS
            or self.clock() - self._burst_first >= BURST_MAX_MS
        ):
            packet = self._take_packet()
            if packet is not None:
                packets.append(packet)
        return packets

    async def __aiter__(self) -> AsyncIterator[PpiPacket]:
        if self._iterating:
            raise RuntimeError("An MQTT source has exactly one consumer")
        self._iterating = True
        try:
            await self.start()
            while not self._closed:
                for packet in self._drain():
                    yield packet
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=0.05)
        finally:
            self._iterating = False
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.connected = False
        self._wake.set()
        self._reset()
        if self._client is not None:
            self._client.disconnect()
            await asyncio.to_thread(self._client.loop_stop)
        with self._lock:
            self._mailbox.clear()
