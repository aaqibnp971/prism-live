"""Generate Prism Live's four deterministic placeholder stems.

These are engineering assets for scene, gate and filter testing, not the final composition. They
are deliberately plain, but obey the delivery constraints that matter to the engine:

- mono 48 kHz IEEE-float WAV;
- exact 19 / 17 / 13 / 11 second sample counts;
- a C1-continuous loop join;
- no spectral content in the heartbeat layer's 36 to 62 Hz reservation;
- comfortable true-peak headroom;
- a D-minor-compatible bed with smoothly falling harmonics through at least 6 kHz.

The output is reproducible and gitignored. Validate it with the independent, file-based checker:

    python -m tools.make_placeholder_stems
    python -m tools.check_stems assets/placeholders
"""

from __future__ import annotations

import argparse
import math
import struct
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "assets" / "placeholders"

RATE = 48_000
FRAMES = {
    "bed": 912_000,
    "sub": 816_000,
    "air": 624_000,
    "pulse": 528_000,
}
STEMS = tuple(FRAMES)
TARGET_SAMPLE_PEAK_DBFS = -6.0
TARGET_SAMPLE_PEAK = 10.0 ** (TARGET_SAMPLE_PEAK_DBFS / 20.0)
HIGHPASS_HZ = 62.0
HIGHPASS_ORDER = 4  # 24 dB/octave below the corner


def _phase(frames: int) -> np.ndarray:
    return np.arange(frames, dtype=np.float64) * (2.0 * math.pi / frames)


def _cycle_for(hz: float, frames: int) -> int:
    """Nearest whole number of cycles in a loop, so every oscillator is exactly periodic."""
    return round(hz * frames / RATE)


def _seam_errors(samples: np.ndarray) -> tuple[float, float]:
    value = float(samples[-1] - samples[0])
    derivative = float((samples[-1] - samples[-2]) - (samples[1] - samples[0]))
    return value, derivative


def _make_seam_c1(samples: np.ndarray, correction_hz: float) -> np.ndarray:
    """Add one allowed-frequency sinusoid so endpoint value and derivative both match.

    The correction stays on one exact DFT bin. It therefore cannot leak into the 36--62 Hz
    reservation as a time-domain endpoint patch would.
    """
    frames = len(samples)
    cycle = _cycle_for(correction_hz, frames)
    angle = _phase(frames) * cycle
    sine, cosine = np.sin(angle), np.cos(angle)
    matrix = np.asarray([_seam_errors(sine), _seam_errors(cosine)], dtype=np.float64).T
    correction = np.linalg.solve(matrix, -np.asarray(_seam_errors(samples)))
    return samples + correction[0] * sine + correction[1] * cosine


def _normalise(samples: np.ndarray) -> np.ndarray:
    peak = float(np.max(np.abs(samples)))
    if not math.isfinite(peak) or peak == 0.0:
        raise ValueError("a placeholder stem must contain finite, non-zero audio")
    return np.asarray(samples * (TARGET_SAMPLE_PEAK / peak), dtype="<f4")


def _printed_highpass(samples: np.ndarray) -> np.ndarray:
    """Apply a circular fourth-order Butterworth 62 Hz high-pass to the file itself.

    Circular frequency-domain filtering is the right operation for a loop: there is no startup
    transient to hide at the join, and the magnitude is the same fourth-order (24 dB/octave)
    response a causal Butterworth implementation would print.
    """
    frequencies = np.fft.rfftfreq(len(samples), 1.0 / RATE)
    gain = np.zeros_like(frequencies)
    nonzero = frequencies > 0.0
    gain[nonzero] = 1.0 / np.sqrt(
        1.0 + (HIGHPASS_HZ / frequencies[nonzero]) ** (2 * HIGHPASS_ORDER)
    )
    return np.fft.irfft(np.fft.rfft(samples) * gain, len(samples))


def _finish(samples: np.ndarray, correction_hz: float) -> np.ndarray:
    filtered = _printed_highpass(samples)
    return _normalise(_make_seam_c1(filtered, correction_hz))


def _bed(frames: int) -> np.ndarray:
    """D3 pad: a smooth harmonic slope with useful content on both sides of the filter arc."""
    angle = _phase(frames)
    fundamental_cycle = _cycle_for(146.832384, frames)  # D3, adjusted below one cent to loop
    highest = int(6_500.0 * frames / (RATE * fundamental_cycle))
    rng = np.random.default_rng(0xBED)
    samples = np.zeros(frames, dtype=np.float64)
    for harmonic in range(1, highest + 1):
        # A gentle power-law plus exponential tilt: audible at 6 kHz, without a bright shelf.
        amplitude = harmonic**-1.08 * math.exp(-harmonic / 90.0)
        phase = rng.uniform(0.0, 2.0 * math.pi)
        samples += amplitude * np.sin(angle * (fundamental_cycle * harmonic) + phase)
    return _finish(samples, 5_432.0)


def _sub(frames: int) -> np.ndarray:
    """D2 floor with only its fundamental and upper harmonics; nothing exists below 73 Hz."""
    angle = _phase(frames)
    fundamental_cycle = _cycle_for(73.416192, frames)  # D2
    amplitudes = (1.0, 0.30, 0.15, 0.08, 0.045, 0.025)
    phases = (0.1, 1.7, 2.6, 0.8, 2.1, 1.2)
    samples = sum(
        amplitude * np.sin(angle * (fundamental_cycle * harmonic) + phase)
        for harmonic, (amplitude, phase) in enumerate(zip(amplitudes, phases, strict=True), 1)
    )
    return _finish(samples, 367.0)


def _air(frames: int) -> np.ndarray:
    """A deterministic, high, non-pitched texture with a gentle downward spectral tilt."""
    rng = np.random.default_rng(0xA17)
    spectrum = np.zeros(frames // 2 + 1, dtype=np.complex128)
    frequencies = np.fft.rfftfreq(frames, 1.0 / RATE)
    selected = (frequencies >= 3_000.0) & (frequencies <= 14_000.0)
    selected_bins = np.flatnonzero(selected)
    phases = rng.uniform(0.0, 2.0 * math.pi, len(selected_bins))
    tilt = (frequencies[selected] / 3_000.0) ** -0.65
    # Sparse bins keep it light and textured rather than sounding like full-band hiss.
    keep = rng.random(len(selected_bins)) < 0.035
    spectrum[selected_bins[keep]] = tilt[keep] * np.exp(1j * phases[keep])
    samples = np.fft.irfft(spectrum, frames)
    return _finish(samples, 7_111.0)


def _pulse(frames: int) -> np.ndarray:
    """A jittered mid-range pressure texture: roughly 96 BPM, with no bar-one accent."""
    seconds = frames / RATE
    time = np.arange(frames, dtype=np.float64) / RATE
    rng = np.random.default_rng(0x965)
    samples = np.zeros(frames, dtype=np.float64)
    at = 0.31
    event = 0
    while at < seconds - 0.24:
        width = 0.115 + 0.025 * rng.random()
        relative = (time - at) / width
        inside = np.abs(relative) < 1.0
        envelope = np.zeros(frames, dtype=np.float64)
        envelope[inside] = np.cos(0.5 * math.pi * relative[inside]) ** 4
        carrier = (790.0, 1_170.0, 1_730.0)[event % 3]
        phase = rng.uniform(0.0, 2.0 * math.pi)
        samples += (0.72 + 0.18 * rng.random()) * envelope * np.sin(
            2.0 * math.pi * carrier * (time - at) + phase
        )
        # 625 ms implies 96 BPM; deterministic jitter removes a downbeat and bar grid.
        at += 0.625 + rng.uniform(-0.075, 0.075)
        event += 1

    # Windowed bursts are already far above the reserved band. Removing all bins below 180 Hz
    # makes the reservation exact rather than dependent on finite-window sidelobes.
    spectrum = np.fft.rfft(samples)
    spectrum[np.fft.rfftfreq(frames, 1.0 / RATE) < 180.0] = 0.0
    samples = np.fft.irfft(spectrum, frames)
    return _finish(samples, 1_730.0)


GENERATORS = {"bed": _bed, "sub": _sub, "air": _air, "pulse": _pulse}


def make_stem(stem: str) -> np.ndarray:
    """Return one complete placeholder stem as little-endian float32 samples."""
    try:
        frames = FRAMES[stem]
    except KeyError as error:
        raise ValueError(f"unknown stem {stem!r}; expected one of {STEMS}") from error
    return GENERATORS[stem](frames)


def _chunk(name: bytes, payload: bytes) -> bytes:
    return name + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) & 1 else b"")


def write_float32_wav(path: Path, samples: np.ndarray, rate: int = RATE) -> None:
    """Write a mono WAVE_FORMAT_IEEE_FLOAT file without requiring an audio package."""
    samples = np.ascontiguousarray(samples, dtype="<f4")
    fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    fact = struct.pack("<I", len(samples))
    body = _chunk(b"fmt ", fmt) + _chunk(b"fact", fact) + _chunk(b"data", samples.tobytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<4sI4s", b"RIFF", 4 + len(body), b"WAVE") + body)


def generate(output: Path = DEFAULT_OUTPUT) -> tuple[Path, ...]:
    """Generate all four stems into `output` and return their paths."""
    paths = []
    for stem in STEMS:
        path = output / f"{stem}.wav"
        samples = make_stem(stem)
        write_float32_wav(path, samples)
        value_gap, derivative_gap = _seam_errors(samples)
        print(
            f"{stem:>5}: {len(samples):,} frames -> {path} "
            f"(seam {abs(value_gap):.3g}, derivative {abs(derivative_gap):.3g})"
        )
        paths.append(path)
    return tuple(paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"destination directory (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()
    generate(args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
