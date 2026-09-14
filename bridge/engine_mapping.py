"""The engine's PSV mapping, mirrored in Python for logging and tests only.

The Prism Engine turns three effective PSV values into a low-pass cutoff, per-stem gains and
density gates through fixed curves the host cannot change: `pgae/src/mapping.cpp` at acbfd50,
tabled in docs/engine-findings.md, "The mapping". The engine never reports what it made of a PSV,
so the feed logs this mirror's reading of every PSV it sends, and the tests pin the findings'
numbers against it. Nothing here drives the engine. The engine computes its own; this only has
to agree with it.

What the engine reads is the effective value, 0.5 + (value - 0.5) x confidence
(`psv/include/psv/rt.h`), on arousal, cognitive_load and readiness. Valence and mode_hint are
never read. The bridge sends every value already blended with its authority, and confidence 1.0
(CLAUDE.md, "Architecture"), so the value it sends is the value the engine reads.

Every expression keeps mapping.cpp's order of operations, so brightness, density and the gains
are the same doubles the engine computes (the DLL is plain -O2 x86-64: no FMA, no fast-math). The
cutoff goes through pow, whose last bits can differ between C runtimes; it is logged to 0.1 Hz.

A gate here is the engine's target, not what is audible. The engine applies a gate change at
that stem's next loop boundary and then fades it over 1.5 s (findings, "Gate timing").
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# The three dimensions the mapping reads, in the order the feed sends them.
DIMENSIONS = ("arousal", "cognitive_load", "readiness")
NEUTRAL = 0.5
# What the feed sends besides those three (prompt 2.5): the engine never reads valence, and
# confidence 1.0 makes each value its own effective value. mode_hint is NULL.
VALENCE_SENT = 0.5
CONFIDENCE_SENT = 1.0

CUTOFF_MIN_HZ = 300.0
CUTOFF_RATIO = 40.0  # kCutoffMaxHz / kCutoffMinHz: the cutoff runs 300 Hz to 12 kHz
CUTOFF_MAX_HZ = CUTOFF_MIN_HZ * CUTOFF_RATIO
AMP_TRIM = 0.4  # PgaeOptions::amp_trim: level amplitude = 0.4 x gain^1.5 (engine.cpp:18-21)

STEMS = ("bed", "sub", "pulse", "lead", "air")
# Density at or above which a gated stem is active (mapping.cpp:71-73). Bed and sub are always
# on. prism-live's scene has no lead, so its gate changes nothing audible.
GATE_THRESHOLDS = {"pulse": 0.35, "air": 0.55, "lead": 0.72}


def effective(value: float, confidence: float) -> float:
    """What the engine reads from a value and its confidence (psv/include/psv/rt.h:36)."""
    return 0.5 + (value - 0.5) * confidence


def clamp01(x: float) -> float:
    """std::max(0.0, std::min(1.0, x)), NaN included."""
    m = x if x < 1.0 else 1.0
    return m if m > 0.0 else 0.0


def brightness_to_hz(brightness: float) -> float:
    return CUTOFF_MIN_HZ * (CUTOFF_MAX_HZ / CUTOFF_MIN_HZ) ** clamp01(brightness)


def hz_to_brightness(hz: float) -> float:
    """The inverse, for reading the findings' filter points. Not in the engine."""
    return math.log(hz / CUTOFF_MIN_HZ) / math.log(CUTOFF_MAX_HZ / CUTOFF_MIN_HZ)


def density(arousal: float, cognitive_load: float) -> float:
    """The one number that gates every optional stem. Readiness plays no part."""
    a = arousal - 0.5
    l = cognitive_load - 0.5  # noqa: E741 (mapping.cpp's name)
    return clamp01(0.5 + 0.9 * a - 1.0 * l)


@dataclass(frozen=True)
class EngineParams:
    """What the engine makes of one PSV, before its own smoothing and loop boundaries."""

    brightness: float
    cutoff_hz: float
    density: float
    gains: dict[str, float]  # perceptual 0 to 1, per stem
    gates: dict[str, bool]  # active per stem

    def to_log(self) -> dict:
        return {
            "cutoff_hz": round(self.cutoff_hz, 1),
            "brightness": round(self.brightness, 4),
            "density": round(self.density, 4),
            "gates": dict(self.gates),
            "gains": {stem: round(g, 4) for stem, g in self.gains.items()},
        }


def map_effective(arousal: float, cognitive_load: float, readiness: float) -> EngineParams:
    """mapping.cpp's map_from_effectives, on effective values."""
    a = arousal - 0.5
    l = cognitive_load - 0.5  # noqa: E741
    r = readiness - 0.5
    brightness = clamp01(0.55 + 0.9 * a - 0.8 * l)
    dens = clamp01(0.5 + 0.9 * a - 1.0 * l)
    gains = {
        "bed": clamp01(0.6 + 0.6 * l),
        "sub": clamp01(0.5 - 0.6 * r),
        "pulse": clamp01(0.5 + 0.7 * a - 0.6 * l),
        "lead": clamp01(0.5 + 0.4 * a - 0.9 * l),
        "air": clamp01(0.5 + 0.5 * a - 0.6 * l),
    }
    gates = {"bed": True, "sub": True}
    gates.update({stem: dens >= threshold for stem, threshold in GATE_THRESHOLDS.items()})
    return EngineParams(
        brightness=brightness,
        cutoff_hz=brightness_to_hz(brightness),
        density=dens,
        gains=gains,
        gates={stem: gates[stem] for stem in STEMS},
    )


def level_amplitude(gain: float) -> float:
    """A stem's level amplitude at a perceptual gain (engine.cpp:18-21)."""
    return AMP_TRIM * gain**1.5


def level_db(gain: float) -> float:
    """That amplitude in dBFS; -inf at gain 0."""
    amplitude = level_amplitude(gain)
    return 20.0 * math.log10(amplitude) if amplitude > 0.0 else -math.inf


def gain_change_db(before: float, after: float) -> float:
    """A gain move in dB: 30 log10(after / before), since amplitude goes as gain^1.5."""
    return level_db(after) - level_db(before)
