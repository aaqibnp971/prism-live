"""Measured optical intervals, without pretending they are standard BLE RR packets.

Quality and reported contact/HR travel intact to the scheduler. Neither contact nor
the reported HR proves that a Verity Sense is being worn. ``arrived_ms`` is the
laptop's T_engine callback time, never the phone's unsynchronised wall clock.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PpiSample:
    rr_ms: float
    error_ms: float
    blocked: bool
    reported_contact: bool | None = None
    reported_hr: float | None = None


@dataclass(frozen=True)
class PpiPacket:
    samples: tuple[PpiSample, ...]
    source_id: str
    arrived_ms: float
    discontinuity: bool = False
