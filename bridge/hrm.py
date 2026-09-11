"""Bluetooth Heart Rate Measurement characteristic, 0x2A37.

The payload is variable length. Parse the flags byte first and index everything else from
it: the heart rate is 8 or 16 bits, energy expended may or may not be present, and zero or
more RR intervals follow. RR intervals are in units of 1/1024 s, not milliseconds. The
conversion happens here and nowhere else.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass

HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HRM_CHARACTERISTIC_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

# The flags byte.
FLAG_HR_UINT16 = 0x01
FLAG_CONTACT_DETECTED = 0x02
FLAG_CONTACT_SUPPORTED = 0x04
FLAG_ENERGY_PRESENT = 0x08
FLAG_RR_PRESENT = 0x10

RR_UNITS_PER_SECOND = 1024


def rr_raw_to_ms(raw: int) -> float:
    """Native 1/1024 s units to milliseconds."""
    return raw * 1000.0 / RR_UNITS_PER_SECOND


def rr_ms_to_raw(ms: float) -> int:
    """Milliseconds to the nearest native 1/1024 s unit."""
    return round(ms * RR_UNITS_PER_SECOND / 1000.0)


@dataclass(frozen=True, slots=True)
class HrmPacket:
    hr_bpm: int
    contact_supported: bool
    contact_detected: bool | None  # None when the sensor does not report contact at all
    energy_expended_kj: int | None
    rr_flag: bool  # the RR-present bit as received, whether or not intervals followed
    rr_raw: tuple[int, ...]  # native units, exactly as received

    @property
    def rr_ms(self) -> tuple[float, ...]:
        return tuple(rr_raw_to_ms(r) for r in self.rr_raw)


def parse_hrm(payload: bytes) -> HrmPacket:
    """Decode one notification. Raises ValueError on a malformed payload."""
    if len(payload) < 2:
        raise ValueError(f"payload too short: {payload.hex()!r}")
    flags = payload[0]
    pos = 1

    if flags & FLAG_HR_UINT16:
        hr_bpm = _uint16(payload, pos, "heart rate")
        pos += 2
    else:
        hr_bpm = payload[pos]
        pos += 1

    energy = None
    if flags & FLAG_ENERGY_PRESENT:
        energy = _uint16(payload, pos, "energy expended")
        pos += 2

    rest = len(payload) - pos
    rr_flag = bool(flags & FLAG_RR_PRESENT)
    if rr_flag:
        if rest % 2:
            raise ValueError(f"RR field has an odd byte count ({rest}): {payload.hex()}")
        rr_raw = struct.unpack_from(f"<{rest // 2}H", payload, pos)
    elif rest:
        raise ValueError(f"{rest} trailing bytes with the RR-present flag clear: {payload.hex()}")
    else:
        rr_raw = ()

    supported = bool(flags & FLAG_CONTACT_SUPPORTED)
    detected = bool(flags & FLAG_CONTACT_DETECTED) if supported else None
    return HrmPacket(hr_bpm, supported, detected, energy, rr_flag, tuple(rr_raw))


def encode_hrm(
    hr_bpm: int,
    rr_raw: Sequence[int] = (),
    *,
    contact_supported: bool = True,
    contact_detected: bool = True,
    energy_expended_kj: int | None = None,
    hr_uint16: bool | None = None,
) -> bytes:
    """Build one notification the way a device would. For the synthetic sender and tests."""
    if hr_uint16 is None:
        hr_uint16 = hr_bpm > 0xFF
    flags = 0
    if hr_uint16:
        flags |= FLAG_HR_UINT16
    if contact_supported:
        flags |= FLAG_CONTACT_SUPPORTED
        if contact_detected:
            flags |= FLAG_CONTACT_DETECTED
    if energy_expended_kj is not None:
        flags |= FLAG_ENERGY_PRESENT
    if rr_raw:
        flags |= FLAG_RR_PRESENT

    out = bytearray([flags])
    out += struct.pack("<H", hr_bpm) if hr_uint16 else bytes([hr_bpm])
    if energy_expended_kj is not None:
        out += struct.pack("<H", energy_expended_kj)
    for raw in rr_raw:
        out += struct.pack("<H", raw)
    return bytes(out)


def _uint16(payload: bytes, pos: int, what: str) -> int:
    if len(payload) < pos + 2:
        raise ValueError(f"payload ends inside the {what} field: {payload.hex()}")
    return struct.unpack_from("<H", payload, pos)[0]
