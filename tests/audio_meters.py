"""Reference meters for the shim and engine tests: true peak, band energy, tone amplitude.

Independent of the shim on purpose. Nothing here shares code, kernels or constants with
native/src, so a mistake in the shim's detector cannot hide behind the same mistake in the meter.

True peak. The peak of the band-limited reconstruction of the samples, the signal taken as zero
before its first sample, in two stages:

- 4x over the whole signal by FFT: the spectrum zero-padded, the Nyquist bin split between its
  two images, so every fourth output sample is an input sample. That is the ideal sinc
  interpolator, not a windowed approximation of one, truncated only at the edges of a chunk. Each
  chunk carries CONTEXT samples of real signal either side that are used and not searched; the
  sinc tails beyond them add about 0.0035 of the signal's RMS to a reading, a few thousandths of
  a dB at the levels tested.
- 32x in every 4x interval with an endpoint within 3 dB of the largest 4x sample so far, by a
  Kaiser-windowed sinc on the 4x stream. The signal fills a quarter of that stream's band, so a
  short kernel is accurate there: tests/test_shim.py holds the whole meter against a direct
  256x FFT reconstruction.

A reading can only be low by how far a 32x grid misses a crest, under 0.01 dB below 20 kHz. The
3 dB rule loses nothing: 4x misses a crest by at most 0.69 dB.
"""

from __future__ import annotations

import math

import numpy as np

RATE = 48_000

CHUNK = 1 << 20  # samples per FFT, context included
CONTEXT = 1 << 14  # real samples either side of a chunk's searched span
COARSE = 4
FINE = 8  # points per 4x interval: 32x of the signal's rate
FINE_HALF_TAPS = 32
FINE_BETA = 10.0
CANDIDATE_DB = 3.0
BATCH = 1 << 15


def db(ratio: float) -> float:
    """An amplitude ratio in dB."""
    return 20.0 * math.log10(ratio) if ratio > 0.0 else -math.inf


def power_db(ratio: float) -> float:
    """A power or energy ratio in dB."""
    return 10.0 * math.log10(ratio) if ratio > 0.0 else -math.inf


def oversample(segment: np.ndarray, factor: int) -> np.ndarray:
    """Band-limited interpolation of an even-length segment, taken as one period, by `factor`."""
    n = len(segment)
    if n % 2:
        raise ValueError("segment length must be even")
    spectrum = np.fft.rfft(segment.astype(np.float64))
    padded = np.zeros(factor * n // 2 + 1, dtype=np.complex128)
    padded[: n // 2] = spectrum[: n // 2]
    padded[n // 2] = spectrum[n // 2] / 2.0  # Nyquist: half to each image
    return np.fft.irfft(padded, factor * n) * factor


def _fine_kernel() -> np.ndarray:
    """(FINE, 2 H) taps: the 4x stream at i + p / FINE is sum_k up[i + k] K[p, k + H - 1]."""
    offsets = np.arange(-FINE_HALF_TAPS + 1, FINE_HALF_TAPS + 1)
    tau = np.arange(FINE)[:, None] / FINE - offsets[None, :]
    inside = np.clip(1.0 - (tau / FINE_HALF_TAPS) ** 2, 0.0, None)
    return np.sinc(tau) * np.i0(FINE_BETA * np.sqrt(inside)) / np.i0(FINE_BETA)


_KERNEL = _fine_kernel()
_OFFSETS = np.arange(-FINE_HALF_TAPS + 1, FINE_HALF_TAPS + 1)


def true_peak(samples: np.ndarray, start: int = 0, stop: int | None = None) -> float:
    """The linear true peak of samples[start:stop], with every sample of `samples` as context.

    Zero is assumed before sample 0 and after the last sample. A render that stops while its
    material is still sounding has no such silence: pass a `stop` at least CONTEXT before its end.
    """
    x = np.asarray(samples, dtype=np.float64)
    stop = len(x) if stop is None else stop
    core = CHUNK - 2 * CONTEXT
    peak = 0.0
    largest_coarse = 0.0
    for begin in range(start, stop, core):
        end = min(begin + core, stop)
        segment = np.zeros(CHUNK, dtype=np.float64)
        lo, hi = max(0, begin - CONTEXT), min(len(x), begin - CONTEXT + CHUNK)
        segment[lo - (begin - CONTEXT) : hi - (begin - CONTEXT)] = x[lo:hi]
        up = oversample(segment, COARSE)
        first, last = CONTEXT * COARSE, (CONTEXT + end - begin) * COARSE  # searched, 4x indices
        magnitude = np.abs(up[first : last + 1])
        largest_coarse = max(largest_coarse, float(magnitude.max()))
        threshold = largest_coarse * 10.0 ** (-CANDIDATE_DB / 20.0)
        edges = np.maximum(magnitude[:-1], magnitude[1:])
        intervals = first + np.flatnonzero(edges >= threshold)
        peak = max(peak, largest_coarse, _refine(up, intervals))
    return peak


def _refine(up: np.ndarray, intervals: np.ndarray) -> float:
    """The largest magnitude at FINE points in each 4x interval [i, i + 1]."""
    peak = 0.0
    for b in range(0, len(intervals), BATCH):
        index = intervals[b : b + BATCH]
        windows = up[index[:, None] + _OFFSETS[None, :]]
        values = windows @ _KERNEL.T
        peak = max(peak, float(np.abs(values).max(initial=0.0)))
    return peak


def true_peak_dbtp(samples: np.ndarray, start: int = 0, stop: int | None = None) -> float:
    return db(true_peak(samples, start, stop))


def sample_peak_dbfs(samples: np.ndarray) -> float:
    return db(float(np.max(np.abs(samples))))


def band_energy(samples: np.ndarray, lo_hz: float, hi_hz: float, rate: int = RATE) -> float:
    """Energy between lo_hz and hi_hz, both included, under a 4-term Blackman-Harris window.

    The window keeps a strong tone just outside the band (73.4 Hz next to 62 Hz) from leaking
    into it: its sidelobes are 92 dB down. Compare only signals of the same length.
    """
    x = np.asarray(samples, dtype=np.float64)
    n = len(x)
    k = np.arange(n) * (2.0 * math.pi / (n - 1))
    window = 0.35875 - 0.48829 * np.cos(k) + 0.14128 * np.cos(2 * k) - 0.01168 * np.cos(3 * k)
    power = np.abs(np.fft.rfft(x * window)) ** 2
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    return float(power[(freqs >= lo_hz) & (freqs <= hi_hz)].sum())


def tone_amplitude(samples: np.ndarray, hz: float, rate: int = RATE) -> float:
    """The amplitude of the sinusoid at hz that best fits the samples (least squares)."""
    x = np.asarray(samples, dtype=np.float64)
    phase = 2.0 * math.pi * hz * np.arange(len(x)) / rate
    basis = np.column_stack([np.cos(phase), np.sin(phase)])
    (a, b), *_ = np.linalg.lstsq(basis, x, rcond=None)
    return math.hypot(a, b)
