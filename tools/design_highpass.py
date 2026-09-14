"""Design the shim's 62 Hz high-pass and write native/src/highpass_coefficients.h.

36 to 62 Hz belongs to the heartbeat layer alone (CLAUDE.md hard rule 2), and the engine does not
keep that band clear, so the shim high-passes the engine buffer before it adds the heartbeat
layer. Decided 14 September: a 10th-order Chebyshev type II high-pass, at least 30 dB down from
62 Hz, and flat to within 1 dB above that as close to 62 Hz as a 10th-order filter allows. Type II
because its stopband is equiripple, so every frequency at or below 62 Hz gets the full 30 dB, and
its passband is monotone, so nothing above the edge is boosted.

Pure Python, math only, so the design is reproducible without scipy:

- Analog Chebyshev II low-pass prototype, stopband edge at 1 rad/s, 30 dB: the Chebyshev I poles
  for the same epsilon, inverted, and zeros at +-j / cos((2k - 1) pi / 2N).
- Low-pass to high-pass with s -> Ws / s, Ws prewarped so the digital stopband edge is exactly
  62 Hz at 48 kHz, then the bilinear transform.
- Each pole pair takes the nearest remaining zero pair, highest Q first; the sections are written
  from lowest Q to highest, each with unity gain at Nyquist, so the whole filter is exactly 1.0
  there.

The 1 dB passband point of this filter is 69.35 Hz, not 69.3 Hz: order 10 at 30 dB would need
N >= 10.03 to be within 1 dB at 69.3 Hz. 69.3 Hz is 1.03 dB down. The sub stem's D2 at 73.4 Hz is
well inside the passband. Checked below, printed, and re-checked by the tests from the header.

The prototype is designed for 30.001 dB, not 30: a design at exactly 30 dB touches -30 dB at 62 Hz
and at every stopband ripple peak, and evaluated in floating point it lands 1e-10 dB either side,
so "at least 30 dB" would hold only up to rounding. The 0.001 dB margin makes every check below
strict, and moves the 1 dB point by 0.0004 Hz.

    python tools/design_highpass.py            writes the header, prints the response
    python tools/design_highpass.py --check    exits 1 if the committed header differs
"""

from __future__ import annotations

import argparse
import cmath
import math
import re
import sys
from pathlib import Path

FS = 48_000.0
ORDER = 10
STOPBAND_EDGE_HZ = 62.0
STOPBAND_DB = 30.0  # the requirement: at least this far down at and below 62 Hz
DESIGN_MARGIN_DB = 0.001  # the prototype is designed this much deeper. See the docstring.
# The design's 1 dB point, rounded up to 0.01 Hz. See the docstring.
PASSBAND_1DB_HZ = 69.35
TABLE_HZ = (20, 30, 36, 44, 50, 55, 62, 65, 69.3, 69.35, 73.4, 100, 146.8, 1000)

HEADER = Path(__file__).resolve().parent.parent / "native" / "src" / "highpass_coefficients.h"


def analog_prototype() -> tuple[list[complex], list[complex]]:
    """Upper-half-plane poles and zeros of the Chebyshev II low-pass, stopband edge 1 rad/s."""
    eps = 1.0 / math.sqrt(10.0 ** ((STOPBAND_DB + DESIGN_MARGIN_DB) / 10.0) - 1.0)
    mu = math.asinh(1.0 / eps) / ORDER
    poles, zeros = [], []
    for k in range(1, ORDER // 2 + 1):
        theta = (2 * k - 1) * math.pi / (2 * ORDER)
        cheb1 = complex(-math.sinh(mu) * math.sin(theta), math.cosh(mu) * math.cos(theta))
        poles.append(1.0 / cheb1.conjugate())  # 1/p of the upper pole lies in the upper half
        zeros.append(complex(0.0, 1.0 / math.cos(theta)))
    return poles, zeros


def design() -> list[tuple[float, float, float, float, float, float]]:
    """Five sections (b0, b1, b2, a1, a2, q), lowest Q first, unity gain at Nyquist each."""
    ws = 2.0 * FS * math.tan(math.pi * STOPBAND_EDGE_HZ / FS)
    lp_poles, lp_zeros = analog_prototype()
    hp_poles = [ws / p for p in lp_poles]
    # Ws / z for z = j / cos(theta) is -j Ws cos(theta): keep the upper one of the pair.
    hp_zero_w = [ws / abs(z) for z in lp_zeros]

    def bilinear(s: complex) -> complex:
        return (2.0 * FS + s) / (2.0 * FS - s)

    poles = []
    for s in hp_poles:
        s = s if s.imag > 0 else s.conjugate()
        poles.append((bilinear(s), abs(s) / (-2.0 * s.real)))
    # A zero on the j axis lands on the unit circle at 2 atan(W / 2 fs): b2 is exactly 1.
    zero_angles = [2.0 * math.atan(w / (2.0 * FS)) for w in hp_zero_w]

    sections = []
    remaining = list(zero_angles)
    for z_pole, q in sorted(poles, key=lambda item: -item[1]):
        angle = min(remaining, key=lambda a: abs(cmath.exp(1j * a) - z_pole))
        remaining.remove(angle)
        a1 = -2.0 * z_pole.real
        a2 = abs(z_pole) ** 2
        b1 = -2.0 * math.cos(angle)
        # H(-1) = g (1 - b1 + 1) / (1 - a1 + a2) = 1.
        g = (1.0 - a1 + a2) / (2.0 - b1)
        sections.append((g, g * b1, g, a1, a2, q))
    sections.sort(key=lambda sec: sec[5])
    return sections


def magnitude_db(sections, f_hz: float, fs: float = FS) -> float:
    """|H| in dB at f_hz, from (b0, b1, b2, a1, a2, ...) sections."""
    z1 = cmath.exp(-2j * math.pi * f_hz / fs)
    z2 = z1 * z1
    h = complex(1.0)
    for b0, b1, b2, a1, a2, *_ in sections:
        h *= (b0 + b1 * z1 + b2 * z2) / (1.0 + a1 * z1 + a2 * z2)
    return 20.0 * math.log10(abs(h))


def render_header(sections) -> str:
    lines = [
        "/* GENERATED by tools/design_highpass.py. Do not edit: change the script and run it. */",
        "/*",
        " * 10th-order Chebyshev type II high-pass at 48 kHz: at least 30 dB down at and below",
        " * 62 Hz (designed for 30.001 dB), within 1 dB from 69.35 Hz up, 1.0 at Nyquist. Five",
        " * second-order sections, lowest Q first, run in this order. Each row is",
        " * {b0, b1, b2, a1, a2}, a0 = 1: y = b0 x + b1 x[-1] + b2 x[-2] - a1 y[-1] - a2 y[-2].",
        " */",
        "",
        "#ifndef PLS_HIGHPASS_COEFFICIENTS_H",
        "#define PLS_HIGHPASS_COEFFICIENTS_H",
        "",
        "namespace pls {",
        "",
        f"constexpr int kHighpassSections = {len(sections)};",
        f"constexpr double kHighpassSampleRate = {FS:.1f};",
        f"constexpr double kHighpassStopbandEdgeHz = {STOPBAND_EDGE_HZ:.1f};",
        f"constexpr double kHighpassStopbandDb = {STOPBAND_DB:.1f};",
        "",
        "constexpr double kHighpassCoefficients[kHighpassSections][5] = {",
    ]
    for b0, b1, b2, a1, a2, q in sections:
        values = ", ".join(f"{v:.17g}" for v in (b0, b1, b2, a1, a2))
        lines.append(f"    {{{values}}},  // Q {q:.4f}")
    lines += ["};", "", "}  // namespace pls", "", "#endif  // PLS_HIGHPASS_COEFFICIENTS_H", ""]
    return "\n".join(lines)


def parse_coefficients(text: str) -> list[tuple[float, ...]]:
    """The sections (b0, b1, b2, a1, a2) as written in a header's text."""
    body = text.split("kHighpassCoefficients[", 1)[1]
    rows = re.findall(r"\{([^{}]+)\}", body)
    return [tuple(float(v) for v in row.split(",")) for row in rows]


def read_coefficients(path: str | Path = HEADER) -> list[tuple[float, ...]]:
    """The sections as committed, for the tests."""
    return parse_coefficients(Path(path).read_text(encoding="utf-8"))


def check(sections) -> list[str]:
    """Every failure of the decided response, empty if it meets it."""
    failures = []
    grid = [1.0 + 0.01 * i for i in range(6100)] + [STOPBAND_EDGE_HZ]
    worst_stop = max(magnitude_db(sections, f) for f in grid)
    if worst_stop > -STOPBAND_DB:
        failures.append(f"stopband: {worst_stop:.6f} dB somewhere in 1..62 Hz")
    f, worst_pass, worst_f = PASSBAND_1DB_HZ, 0.0, PASSBAND_1DB_HZ
    while f <= 20_000.0:
        db = magnitude_db(sections, f)
        if db < worst_pass:
            worst_pass, worst_f = db, f
        f *= 1.0005
    if worst_pass < -1.0:
        failures.append(f"passband: {worst_pass:.4f} dB at {worst_f:.2f} Hz")
    # Log-spaced from 62 Hz to Nyquist, plus Nyquist itself.
    probes = [STOPBAND_EDGE_HZ * 1.001**i for i in range(8000)] + [FS / 2.0]
    peak = max(10.0 ** (magnitude_db(sections, f) / 20.0) for f in probes if f <= FS / 2.0)
    if peak > 1.0 + 1e-9:
        failures.append(f"gain above 1: {peak!r}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare with the committed header")
    args = parser.parse_args()

    sections = design()
    text = render_header(sections)
    written = parse_coefficients(text)

    print(f"{'Hz':>9}  {'dB':>10}")
    for f in TABLE_HZ:
        print(f"{f:>9}  {magnitude_db(written, f):>10.3f}")
    print(f"{'Nyquist':>9}  {magnitude_db(written, FS / 2.0):>10.3g}")
    for b0, b1, b2, a1, a2, q in sections:
        print(f"Q {q:7.4f}  b = {b0:.6g}, {b1:.6g}, {b2:.6g}  a = {a1:.9f}, {a2:.9f}")

    failures = check(written)
    for failure in failures:
        print("FAIL", failure, file=sys.stderr)
    if failures:
        return 1

    if args.check:
        committed = HEADER.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
        if committed != text:
            print(f"{HEADER} differs from the design: run this script", file=sys.stderr)
            return 1
        print(f"{HEADER} matches")
        return 0
    HEADER.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {HEADER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
