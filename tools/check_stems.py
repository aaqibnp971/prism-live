"""File-based acceptance checker for Prism Live stems.

The checker deliberately knows nothing about how a stem was authored. It reads standard IEEE
float or WAVE_FORMAT_EXTENSIBLE WAV files and applies the same acceptance rules to generated
placeholders and the composer's October delivery.

Accepted input forms:

    python -m tools.check_stems assets/placeholders
    python -m tools.check_stems delivery/bed.wav delivery/sub.wav
    python -m tools.check_stems bed=delivery/final-pad.wav sub=delivery/final-low.wav

A directory means all four conventional filenames must exist. Individual files may be any subset;
use ROLE=PATH when a filename is not exactly bed.wav, sub.wav, air.wav or pulse.wav.

"No energy" needs a finite digital acceptance floor. This checker defines it as less than
-90 dBFS RMS across 36--62 Hz. That is at least 72 dB below the quietest scripted heartbeat before
the shim's additional >=30 dB rejection. The bed's smooth reach is measured independently in four
spectral-density bands from 620 Hz through 6.5 kHz; the top band must remain audible above
-60 dBFS and the bands may neither jump upward nor fall off a cliff.
"""

from __future__ import annotations

import argparse
import math
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRECTORY = ROOT / "assets" / "placeholders"

RATE = 48_000
EXPECTED_FRAMES = {
    "bed": 912_000,
    "sub": 816_000,
    "air": 624_000,
    "pulse": 528_000,
}
ROLES = tuple(EXPECTED_FRAMES)
SEAM_TOLERANCE = 1e-6
RESERVED_BAND_HZ = (36.0, 62.0)
RESERVED_BAND_MAX_DBFS = -90.0
TRUE_PEAK_MAX_DBTP = -1.0
BED_REACH_HZ = (5_900.0, 6_500.0)
BED_REACH_MIN_DBFS = -60.0
BED_SLOPE_BANDS = (
    (620.0, 1_200.0),
    (1_200.0, 2_400.0),
    (2_400.0, 4_800.0),
    (4_800.0, 6_500.0),
)
BED_MAX_RISE_DB = 3.0
BED_MAX_DROP_DB = 18.0

COARSE = 4
FINE = 8  # 8 points in each 4x interval = a 32x grid at the original rate
FINE_HALF_TAPS = 32
FINE_BETA = 10.0
CANDIDATE_DB = 3.0
BATCH = 1 << 15

IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
IEEE_FLOAT_SUBFORMAT = bytes.fromhex("0300000000001000800000aa00389b71")


@dataclass(frozen=True)
class WaveStem:
    path: Path
    samples: np.ndarray
    channels: int
    sample_rate: int
    bits_per_sample: int
    format_name: str


@dataclass(frozen=True)
class StemReport:
    role: str
    path: Path
    metrics: tuple[str, ...]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


class StemFormatError(ValueError):
    """The file is not a supported, well-formed float WAV."""


def _db(amplitude: float) -> float:
    return 20.0 * math.log10(amplitude) if amplitude > 0.0 else -math.inf


def _chunk_payload(data: bytes, offset: int, size: int, path: Path) -> bytes:
    end = offset + size
    if end > len(data):
        raise StemFormatError(f"{path}: truncated WAV chunk")
    return data[offset:end]


def read_float32_wav(path: str | Path) -> WaveStem:
    """Read a mono or multichannel float WAV; validation later requires mono.

    Both classic WAVE_FORMAT_IEEE_FLOAT and the float32 WAVE_FORMAT_EXTENSIBLE form written by
    current DAWs are supported. PCM files are intentionally rejected rather than converted.
    """
    path = Path(path)
    data = path.read_bytes()
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise StemFormatError(f"{path}: not a little-endian RIFF/WAVE file")

    fmt: bytes | None = None
    audio: bytes | None = None
    offset = 12
    while offset + 8 <= len(data):
        name, size = struct.unpack_from("<4sI", data, offset)
        offset += 8
        payload = _chunk_payload(data, offset, size, path)
        if name == b"fmt " and fmt is None:
            fmt = payload
        elif name == b"data" and audio is None:
            audio = payload
        offset += size + (size & 1)

    if fmt is None or audio is None:
        raise StemFormatError(f"{path}: WAV needs one fmt chunk and one data chunk")
    if len(fmt) < 16:
        raise StemFormatError(f"{path}: truncated fmt chunk")
    code, channels, rate, byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", fmt)
    format_name = "IEEE float"
    if code == WAVE_FORMAT_EXTENSIBLE:
        if len(fmt) < 40 or struct.unpack_from("<H", fmt, 16)[0] < 22:
            raise StemFormatError(f"{path}: truncated WAVE_FORMAT_EXTENSIBLE fmt chunk")
        valid_bits = struct.unpack_from("<H", fmt, 18)[0]
        subformat = fmt[24:40]
        if subformat != IEEE_FLOAT_SUBFORMAT or valid_bits != 32:
            raise StemFormatError(f"{path}: extensible WAV is not 32-bit IEEE float")
        format_name = "extensible IEEE float"
    elif code != IEEE_FLOAT:
        raise StemFormatError(f"{path}: format code {code} is not IEEE float")

    expected_align = channels * 4
    if (
        channels == 0
        or rate == 0
        or bits != 32
        or block_align != expected_align
        or byte_rate != rate * block_align
    ):
        raise StemFormatError(f"{path}: inconsistent 32-bit-float WAV format fields")
    if len(audio) % block_align:
        raise StemFormatError(f"{path}: data chunk ends partway through a frame")
    samples = np.frombuffer(audio, dtype="<f4").copy()
    if channels > 1:
        samples = samples.reshape(-1, channels)
    return WaveStem(path, samples, channels, rate, bits, format_name)


def seam_errors(samples: np.ndarray) -> tuple[float, float]:
    """Endpoint value gap and one-sided endpoint-derivative gap."""
    x = np.asarray(samples, dtype=np.float64)
    if len(x) < 3:
        return math.inf, math.inf
    value = abs(float(x[-1] - x[0]))
    derivative = abs(float((x[-1] - x[-2]) - (x[1] - x[0])))
    return value, derivative


def band_rms(
    samples: np.ndarray, low_hz: float, high_hz: float, sample_rate: int = RATE
) -> float:
    """RMS of inclusive DFT bins in a loop's periodic, band-limited reconstruction."""
    x = np.asarray(samples, dtype=np.float64)
    spectrum = np.fft.rfft(x)
    frequencies = np.fft.rfftfreq(len(x), 1.0 / sample_rate)
    selected = (frequencies >= low_hz) & (frequencies <= high_hz)
    # Every selected band here excludes DC and Nyquist, whose real-only bins have half the weight.
    return math.sqrt(2.0 * float(np.sum(np.abs(spectrum[selected]) ** 2))) / len(x)


def _oversample_periodic(samples: np.ndarray, factor: int) -> np.ndarray:
    """Ideal FFT interpolation, treating the verified loop as one period."""
    x = np.asarray(samples, dtype=np.float64)
    frames = len(x)
    spectrum = np.fft.rfft(x)
    padded = np.zeros(factor * frames // 2 + 1, dtype=np.complex128)
    if frames % 2:
        padded[: len(spectrum)] = spectrum
    else:
        padded[: frames // 2] = spectrum[: frames // 2]
        padded[frames // 2] = spectrum[frames // 2] / 2.0
    return np.fft.irfft(padded, factor * frames) * factor


def _fine_kernel() -> tuple[np.ndarray, np.ndarray]:
    offsets = np.arange(-FINE_HALF_TAPS + 1, FINE_HALF_TAPS + 1)
    tau = np.arange(FINE)[:, None] / FINE - offsets[None, :]
    inside = np.clip(1.0 - (tau / FINE_HALF_TAPS) ** 2, 0.0, None)
    kernel = np.sinc(tau) * np.i0(FINE_BETA * np.sqrt(inside)) / np.i0(FINE_BETA)
    return offsets, kernel


_FINE_OFFSETS, _FINE_KERNEL = _fine_kernel()


def true_peak(samples: np.ndarray) -> float:
    """Peak of a periodic band-limited reconstruction, searched on an accurate 32x grid."""
    up = _oversample_periodic(samples, COARSE)
    magnitude = np.abs(up)
    coarse_peak = float(magnitude.max(initial=0.0))
    if coarse_peak == 0.0:
        return 0.0
    threshold = coarse_peak * 10.0 ** (-CANDIDATE_DB / 20.0)
    intervals = np.flatnonzero(np.maximum(magnitude, np.roll(magnitude, -1)) >= threshold)
    peak = coarse_peak
    length = len(up)
    for begin in range(0, len(intervals), BATCH):
        index = intervals[begin : begin + BATCH]
        windows = up[(index[:, None] + _FINE_OFFSETS[None, :]) % length]
        peak = max(peak, float(np.max(np.abs(windows @ _FINE_KERNEL.T), initial=0.0)))
    return peak


def _spectral_density_db(
    samples: np.ndarray, low_hz: float, high_hz: float, sample_rate: int
) -> float:
    spectrum = np.fft.rfft(np.asarray(samples, dtype=np.float64))
    frequencies = np.fft.rfftfreq(len(samples), 1.0 / sample_rate)
    selected = (frequencies >= low_hz) & (frequencies < high_hz)
    if not np.any(selected):
        return -math.inf
    power = float(np.mean(2.0 * np.abs(spectrum[selected]) ** 2 / len(samples) ** 2))
    return 10.0 * math.log10(power) if power > 0.0 else -math.inf


def _bed_checks(samples: np.ndarray, sample_rate: int) -> tuple[list[str], list[str]]:
    metrics: list[str] = []
    failures: list[str] = []
    reach_dbfs = _db(band_rms(samples, *BED_REACH_HZ, sample_rate))
    metrics.append(f"bed {BED_REACH_HZ[0]:.0f}-{BED_REACH_HZ[1]:.0f} Hz {reach_dbfs:.2f} dBFS RMS")
    if reach_dbfs < BED_REACH_MIN_DBFS:
        failures.append(
            f"bed does not reach 6 kHz: {reach_dbfs:.2f} dBFS in "
            f"{BED_REACH_HZ[0]:.0f}-{BED_REACH_HZ[1]:.0f} Hz, need >= {BED_REACH_MIN_DBFS:.1f}"
        )

    density = [
        _spectral_density_db(samples, *band, sample_rate) for band in BED_SLOPE_BANDS
    ]
    metrics.append(
        "bed spectral-density slope "
        + " -> ".join(f"{value:.1f}" for value in density)
        + " dBFS/bin"
    )
    if not all(math.isfinite(value) for value in density):
        failures.append("bed has an empty spectral-density band before 6.5 kHz")
        return metrics, failures
    changes = [
        after - before for before, after in zip(density[:-1], density[1:], strict=True)
    ]
    for band, change in zip(BED_SLOPE_BANDS[1:], changes, strict=True):
        if change > BED_MAX_RISE_DB:
            failures.append(
                f"bed energy rises {change:.2f} dB into {band[0]:.0f}-{band[1]:.0f} Hz; "
                f"maximum smooth rise is {BED_MAX_RISE_DB:.1f} dB"
            )
        if change < -BED_MAX_DROP_DB:
            failures.append(
                f"bed energy falls {abs(change):.2f} dB into {band[0]:.0f}-{band[1]:.0f} Hz; "
                f"maximum smooth drop is {BED_MAX_DROP_DB:.1f} dB"
            )
    if density[-1] >= density[0]:
        failures.append("bed energy does not fall overall from 620 Hz to 6.5 kHz")
    return metrics, failures


def check_stem(role: str, path: str | Path) -> StemReport:
    """Read and check one stem, returning every failure rather than stopping at the first."""
    path = Path(path)
    metrics: list[str] = []
    failures: list[str] = []
    try:
        wave = read_float32_wav(path)
    except (OSError, StemFormatError) as error:
        return StemReport(role, path, (), (str(error),))

    frames = len(wave.samples)
    metrics.append(
        f"{wave.format_name}, {wave.channels} ch, {wave.sample_rate} Hz, "
        f"{wave.bits_per_sample}-bit, {frames:,} frames"
    )
    if wave.channels != 1:
        failures.append(f"expected mono, found {wave.channels} channels")
        return StemReport(role, path, tuple(metrics), tuple(failures))
    if wave.sample_rate != RATE:
        failures.append(f"expected {RATE} Hz, found {wave.sample_rate} Hz")
    if frames != EXPECTED_FRAMES[role]:
        failures.append(f"expected {EXPECTED_FRAMES[role]:,} frames, found {frames:,}")

    samples = wave.samples
    if not np.all(np.isfinite(samples)):
        failures.append("samples contain NaN or infinity")
        return StemReport(role, path, tuple(metrics), tuple(failures))
    if frames < 3:
        failures.append("at least three frames are required to check a loop")
        return StemReport(role, path, tuple(metrics), tuple(failures))

    value_gap, derivative_gap = seam_errors(samples)
    metrics.append(f"loop seam value {value_gap:.3g}, derivative {derivative_gap:.3g}")
    if value_gap > SEAM_TOLERANCE:
        failures.append(f"loop endpoint gap {value_gap:.9g} exceeds {SEAM_TOLERANCE:g}")
    if derivative_gap > SEAM_TOLERANCE:
        failures.append(f"loop derivative gap {derivative_gap:.9g} exceeds {SEAM_TOLERANCE:g}")

    reserved_dbfs = _db(band_rms(samples, *RESERVED_BAND_HZ, wave.sample_rate))
    metrics.append(
        f"reserved {RESERVED_BAND_HZ[0]:.0f}-{RESERVED_BAND_HZ[1]:.0f} Hz "
        f"{reserved_dbfs:.2f} dBFS RMS"
    )
    if reserved_dbfs > RESERVED_BAND_MAX_DBFS:
        failures.append(
            f"reserved-band energy is {reserved_dbfs:.2f} dBFS; "
            f"must be <= {RESERVED_BAND_MAX_DBFS:.1f} dBFS"
        )

    peak_dbtp = _db(true_peak(samples))
    metrics.append(f"true peak {peak_dbtp:.3f} dBTP")
    if peak_dbtp >= TRUE_PEAK_MAX_DBTP:
        failures.append(
            f"true peak {peak_dbtp:.3f} dBTP must be below {TRUE_PEAK_MAX_DBTP:.1f} dBTP"
        )

    if role == "bed":
        bed_metrics, bed_failures = _bed_checks(samples, wave.sample_rate)
        metrics.extend(bed_metrics)
        failures.extend(bed_failures)
    return StemReport(role, path, tuple(metrics), tuple(failures))


def _inputs(arguments: Iterable[str]) -> tuple[tuple[str, Path], ...]:
    values = list(arguments)
    if not values:
        values = [str(DEFAULT_DIRECTORY)]
    found: list[tuple[str, Path]] = []
    for value in values:
        if "=" in value:
            role, raw_path = value.split("=", 1)
            role = role.lower()
            if role not in ROLES:
                raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
            found.append((role, Path(raw_path)))
            continue
        path = Path(value)
        if path.is_dir() or (not path.exists() and not path.suffix):
            found.extend((role, path / f"{role}.wav") for role in ROLES)
            continue
        role = path.stem.lower()
        if role not in ROLES:
            raise ValueError(f"cannot infer a role from {path.name!r}; use ROLE=PATH")
        found.append((role, path))
    roles = [role for role, _ in found]
    duplicates = sorted({role for role in roles if roles.count(role) > 1})
    if duplicates:
        raise ValueError(f"duplicate stem roles: {', '.join(duplicates)}")
    return tuple(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "stems",
        nargs="*",
        metavar="STEM",
        help="directory, conventional WAV path, or ROLE=PATH (default: assets/placeholders)",
    )
    args = parser.parse_args(argv)
    try:
        inputs = _inputs(args.stems)
    except ValueError as error:
        parser.error(str(error))

    passed = True
    for role, path in inputs:
        report = check_stem(role, path)
        print(f"{role}: {path}")
        for metric in report.metrics:
            print(f"  {metric}")
        if report.passed:
            print("  PASS")
        else:
            passed = False
            for failure in report.failures:
                print(f"  FAIL: {failure}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
