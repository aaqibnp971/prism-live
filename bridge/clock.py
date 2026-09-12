"""T_engine: the laptop's one clock (contract §1).

Milliseconds on a monotonic clock whose zero is the first import of this module, which the
bridge does at startup. Every laptop timestamp on the link is on this clock: beat t_play,
state t_engine, clock pong t_engine. Clients convert it to their own clock with the clock
exchange (bridge/clock_sync.py), never by assuming the two agree.
"""

from __future__ import annotations

import time

# perf_counter, not monotonic: both are monotonic, but on Windows before Python 3.13
# time.monotonic is GetTickCount64 and moves in 15.6 ms steps. perf_counter is
# QueryPerformanceCounter, well under a microsecond.
_ORIGIN_NS = time.perf_counter_ns()


def t_engine_ms() -> float:
    return (time.perf_counter_ns() - _ORIGIN_NS) / 1_000_000
