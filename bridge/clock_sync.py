"""The client side of the clock exchange (contract §2, ``clock``).

Every client needs this arithmetic before it can render anything at t_play. The web screens
use web/shared/clock_sync.js, a line-for-line port; tests/test_clock_sync.py checks that both
give identical offsets from the same pongs. Unity will need a third copy.

From the contract:

    rtt     = t_client_now - t_client_sent
    offset  = t_engine + rtt/2 - t_client_now
    t_local = t_play - offset

Keep a rolling median of the last 9 offsets, discard any sample whose rtt is more than twice
the median rtt, and ping every 2 s.

An estimate belongs to one connection. The bridge's T_engine starts again at 0 when it
restarts, so build a new ClockSync on every connect; never carry one across a reconnect.

Contract v1.1 adds two rules, so the estimate can never get stuck:

- The median rtt is taken over the last 9 rtts *observed*, discarded samples included. If it
  were taken over kept samples only, a lasting rise in network delay would get every later
  sample discarded, forever.
- Twice the median rtt has a floor of twice ``RTT_FLOOR_MS``. On localhost the median rtt is
  a fraction of a millisecond, and twice nearly nothing would discard almost every sample.
"""

from __future__ import annotations

import math
from collections import deque
from statistics import median

WINDOW = 9
PING_INTERVAL_MS = 2000
RTT_FLOOR_MS = 2.0
MAX_RTT_MS = 5000.0  # a pong this late belongs to a connection that has since recovered


class ClockSync:
    def __init__(self) -> None:
        self._offsets: deque[float] = deque(maxlen=WINDOW)
        self._rtts: deque[float] = deque(maxlen=WINDOW)

    def ping(self, t_client_now: float) -> dict:
        return {"type": "clock", "v": 1, "role": "ping", "t_client_sent": t_client_now}

    def on_pong(self, pong: dict, t_client_now: float) -> bool:
        """Take one pong, received at t_client_now on this client's clock. True if it was used."""
        rtt = t_client_now - pong["t_client_sent"]
        if not 0 <= rtt <= MAX_RTT_MS:
            return False
        limit = 2 * max(median(self._rtts), RTT_FLOOR_MS) if self._rtts else math.inf
        self._rtts.append(rtt)
        if rtt > limit:
            return False
        self._offsets.append(pong["t_engine"] + rtt / 2 - t_client_now)
        return True

    @property
    def ready(self) -> bool:
        return bool(self._offsets)

    @property
    def samples(self) -> int:
        return len(self._offsets)

    @property
    def offset(self) -> float | None:
        """T_engine minus this client's clock, in ms. None until the first pong is used."""
        return median(self._offsets) if self._offsets else None

    def to_local(self, t_engine: float) -> float:
        """A laptop time, such as t_play, on this client's clock."""
        return t_engine - self._require_offset()

    def to_engine(self, t_local: float) -> float:
        return t_local + self._require_offset()

    def _require_offset(self) -> float:
        if not self._offsets:
            raise RuntimeError("no clock sample yet: ping, and wait for a pong")
        return median(self._offsets)
