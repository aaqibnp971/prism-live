"""A generated scene for the engine tests: four short WAV loops and a one-scene manifest.

Not the placeholder stems, which prompt 2.7 builds. These exist so the tests can hear each stem
on its own frequencies: 16-bit PCM mono through Python's wave module, loops of a few seconds with
integer sample counts, every tone a whole number of cycles per loop so each loop is seamless.

- bed: 200 Hz and its harmonics to 6 kHz at 1/k, always on, for the engine's low-pass to work on.
  No harmonic lands near the pulse tone (800 and 1,000 Hz are its neighbours).
- sub: 73.4 Hz (D2) and, deliberately, 45 Hz inside 36 to 62 Hz, for the high-pass test.
- pulse: 880 Hz in 125 ms flat-topped bursts, four a second: the gated stem whose tone the gate
  test listens for.
- air: white noise of random full-scale signs, as loud as noise gets for its peak, so the
  loudest PSV drives the engine into its own -3 dBFS limiter.

The manifest has the shape of vendor/prism-core/assets/scenes.json: one scene, the default, and
no lead.
"""

from __future__ import annotations

import json
import math
import wave
from pathlib import Path

import numpy as np

from bridge.phase import STEM_FRAMES

RATE = 48_000
LOOP_S = {"bed": 3.8, "sub": 5.0, "pulse": 2.25, "air": 2.6}
STEMS = ("bed", "sub", "pulse", "air")
PEAK = 0.9  # each stem's sample peak

BED_HZ = 200.0
BED_HARMONICS = 30  # to 6 kHz
SUB_HZ = 73.4
SUB_IN_BAND_HZ = 45.0
PULSE_HZ = 880.0
PULSE_BURST_S = 0.125
PULSE_EDGE_S = 0.01  # raised-cosine rise and fall inside each burst
PULSE_PERIOD_S = 0.25

# Exact production loop lengths with spectrally separate signals.  Prompt 2.7's phase tests use
# these temporary test stems, not assets/placeholders and not the placeholder generator.
PHASE_TONES = {"sub": 100.0, "pulse": 880.0, "air": 4_000.0}


def loop_frames(stem: str, rate: int = RATE) -> int:
    return round(LOOP_S[stem] * rate)


def stem_samples(stem: str, rate: int = RATE) -> np.ndarray:
    """One loop of `stem` as float64, peak PEAK."""
    n = loop_frames(stem, rate)
    t = np.arange(n) / rate
    rng = np.random.default_rng(sum(map(ord, stem)))
    if stem == "bed":
        phases = rng.uniform(0.0, 2.0 * math.pi, BED_HARMONICS)
        x = sum(
            np.sin(2.0 * math.pi * BED_HZ * k * t + phases[k - 1]) / k
            for k in range(1, BED_HARMONICS + 1)
        )
    elif stem == "sub":
        x = np.sin(2.0 * math.pi * SUB_HZ * t) + np.sin(2.0 * math.pi * SUB_IN_BAND_HZ * t)
    elif stem == "pulse":
        within = np.mod(t, PULSE_PERIOD_S)
        edge = np.minimum(within, PULSE_BURST_S - within) / PULSE_EDGE_S
        envelope = np.where(
            within < PULSE_BURST_S, np.sin(0.5 * math.pi * np.clip(edge, 0, 1)) ** 2, 0.0
        )
        x = np.sin(2.0 * math.pi * PULSE_HZ * t) * envelope
    elif stem == "air":
        x = rng.choice([-1.0, 1.0], n)
    else:
        raise ValueError(stem)
    return x * (PEAK / np.max(np.abs(x)))


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    pcm = np.round(np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm.tobytes())


def make_scene(directory: Path, rate: int = RATE, rates: dict[str, int] | None = None) -> Path:
    """Write the four stems and scenes.json into `directory`; return the manifest's path.

    `rates` overrides the rate of single stems, for a scene the engine must reject.
    """
    directory.mkdir(parents=True, exist_ok=True)
    stems = []
    for stem in STEMS:
        stem_rate = (rates or {}).get(stem, rate)
        write_wav(directory / f"{stem}.wav", stem_samples(stem, stem_rate), stem_rate)
        stems.append({"role": stem, "file": f"{stem}.wav"})
    manifest = {
        "schema_version": "1.0.0",
        "default_scene": "test",
        "scenes": [{"id": "test", "key": "D minor", "stems": stems}],
    }
    path = directory / "scenes.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def phase_stem_samples(stem: str) -> np.ndarray:
    """One exact-length phase-test loop, with each gated stem isolated at one frequency."""
    frames = STEM_FRAMES[stem]
    if stem == "bed":
        # A deterministic, unique 19 s texture. Every selected DFT bin is exactly periodic, and
        # none overlaps the three role-identifying tones below.
        rng = np.random.default_rng(0x27)
        spectrum = np.zeros(frames // 2 + 1, np.complex128)
        frequencies = np.fft.rfftfreq(frames, 1.0 / RATE)
        bins = np.flatnonzero((frequencies >= 200.0) & (frequencies <= 700.0))[::29]
        spectrum[bins] = np.exp(1j * rng.uniform(0.0, 2.0 * math.pi, len(bins)))
        samples = np.fft.irfft(spectrum, frames)
    else:
        frequency = PHASE_TONES[stem]
        samples = np.sin(2.0 * math.pi * frequency * np.arange(frames) / RATE)
    return samples * (PEAK / np.max(np.abs(samples)))


def make_phase_scene(directory: Path) -> Path:
    """Write four exact production-length loops for sample-accurate phase/gate tests."""
    directory.mkdir(parents=True, exist_ok=True)
    stems = []
    for stem in STEMS:
        write_wav(directory / f"{stem}.wav", phase_stem_samples(stem), RATE)
        stems.append({"role": stem, "file": f"{stem}.wav"})
    manifest = {
        "schema_version": "1.0.0",
        "default_scene": "phase-test",
        "scenes": [{"id": "phase-test", "key": "D minor", "stems": stems}],
    }
    path = directory / "scenes.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path
