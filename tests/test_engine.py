"""bridge/engine.py: the real engine through the shim, offline, with a generated scene (prompt 2.5).

The scene is tests/scene_fixture.py's: bed 200 Hz harmonics, sub 73.4 Hz and 45 Hz, pulse 880 Hz
bursts, air noise. Every render runs on the test's thread through pls_render_offline; no test
opens a device. Levels are read with tests/audio_meters.py.

The question prompt 2.5 asks, answered by measurement: the engine renders, and honours the mood
override, with prism_start never called. The low-pass lands where the Python mirror of the mapping
puts it, and the pulse stem opens and closes on its density gate at its loop boundary. If either
stopped being true these tests say so in as many words.

Also here: a scene that is not 48 kHz is refused; a four-minute render at the loudest PSV with the
heartbeat at its loudest scripted level stays under -1.0 dBTP; the engine's 36 to 62 Hz band is at
least 24 dB down after the high-pass; a whole fixture session through the PSV feed and the session
gain; EngineHost's start, stop and close; and nothing in prism-live calls prism_start or the
engine's built-in device.
"""

import ast
import asyncio
import ctypes
import hashlib
import io
import json
import math
import re
import sys
import tokenize
import wave
from pathlib import Path
from types import SimpleNamespace

import audio_meters as meters
import numpy as np
import pytest
import scene_fixture
from conftest import c_code

from bridge import clock
from bridge import engine as engine_module
from bridge.engine import (
    ENGINE_DLL,
    RENDER_FN,
    STOP_DEADLINE_MS,
    STOP_POLL_S,
    STOP_RAMP_MS,
    Engine,
    EngineError,
    EngineHost,
    PlsConfig,
    PlsHeartbeatOnset,
    PlsStats,
    PrismConfig,
    PrismMoodOverride,
    Shim,
    ShimError,
    engine_library,
)
from bridge.engine_feed import FADE_FRAMES, HeartbeatLevel, PsvFeed, SessionGain
from bridge.engine_mapping import map_effective
from bridge.phase import PULSE_ALIGNMENT_PHASE, PhaseTracker

if sys.platform != "win32":
    pytest.skip("the engine and the shim are Windows DLLs", allow_module_level=True)

ROOT = Path(__file__).resolve().parent.parent
RATE = 48_000
LATENCY = Shim.latency_frames()
CEILING_DBTP = -1.0
FIXTURE = ROOT / "tools" / "fixtures" / "synthetic-clean.jsonl"

LOUDEST = (1.0, 0.0, 0.0)  # brightness 1 (12 kHz), density 1: every gate open, sub gain 0.8
DARK = (0.0, 1.0, 0.5)  # brightness 0 (300 Hz), density 0: every gate closed
BASELINE_POSE = (0.486, 0.65, 0.5)  # 1,400 Hz, pulse and air closed (bridge/poses.py)


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    return scene_fixture.make_scene(tmp_path_factory.mktemp("scene"))


@pytest.fixture(scope="module")
def phase_scene(tmp_path_factory):
    return scene_fixture.make_phase_scene(tmp_path_factory.mktemp("phase-scene"))


@pytest.fixture
def host(scene):
    host = EngineHost()
    host.open(scene)
    yield host
    host.close()


def render(shim, frames, chunk=RATE):
    """(out, tap) for the next `frames` frames."""
    out = np.zeros(frames, np.float32)
    tap = np.zeros(frames, np.float32)
    for start in range(0, frames, chunk):
        shim.render_offline(out[start : start + chunk], tap[start : start + chunk])
    return out, tap


# --- the library and the scene -------------------------------------------------------------------


def test_the_engine_library_is_the_acbfd50_build():
    assert engine_library().prism_version().decode() == "0.3.0"
    readme = (ROOT / "vendor" / "lib" / "README.md").read_text(encoding="utf-8")
    assert f"`{hashlib.sha256(ENGINE_DLL.read_bytes()).hexdigest()}`" in readme


def test_engine_host_records_its_one_blocking_scene_load(host, scene):
    assert host.engine.scene == scene.resolve()
    assert host.scene_load_ms is not None and host.scene_load_ms > 0.0


def test_the_ctypes_structs_have_the_c_layouts():
    """x64: prism_config int32, int32, int64, int64, double; prism_mood_override a pointer and five
    doubles; pls_config four uint32 and a double; pls_stats ABI 3's timing and loss fields."""
    sizes = [
        ctypes.sizeof(t)
        for t in (PrismConfig, PrismMoodOverride, PlsConfig, PlsStats, PlsHeartbeatOnset)
    ]
    assert sizes == [32, 48, 24, 128, 16]
    assert (PrismConfig.cadence_ms.offset, PrismConfig.significant_delta.offset) == (8, 24)
    assert (PlsConfig.engine_trim_db.offset, PlsStats.limiter_min_gain.offset) == (16, 48)
    assert PlsStats.device_unrequested_stops.offset == 56
    assert PlsStats.device_clock_samples.offset == 64
    assert PlsStats.heartbeat_onset_measurements.offset == 80
    assert PlsStats.heartbeat_onset_error_abs_max_ms.offset == 112
    assert PlsStats.heartbeat_onset_telemetry_dropped.offset == 120
    assert PlsHeartbeatOnset.error_ms.offset == 8
    assert Shim.abi_version() == engine_module.SHIM_ABI_VERSION == 3


def test_a_44_1_khz_scene_is_refused_and_the_engine_destroyed(tmp_path):
    manifest = scene_fixture.make_scene(tmp_path, rate=44_100)
    engine = Engine()
    with pytest.raises(EngineError, match="44100 Hz"):
        engine.load_scene(manifest)
    with pytest.raises(EngineError, match="destroyed"):
        engine.handle  # noqa: B018
    host = EngineHost()
    with pytest.raises(EngineError, match="44100 Hz"):
        host.open(manifest)
    assert (host.engine, host.shim) == (None, None)


@pytest.mark.parametrize(
    ("manifest", "error"),
    [(None, TypeError), ("missing-scene.json", FileNotFoundError)],
    ids=["not a path", "missing"],
)
def test_open_destroys_the_engine_when_the_scene_raises_before_the_engine_sees_it(
    monkeypatch, manifest, error
):
    """A path the scene layer cannot load raises before the engine sees it, and open still frees
    the new handle."""
    destroyed = []
    destroy = Engine.destroy

    def counted(self):
        destroyed.append(self._handle)
        destroy(self)

    monkeypatch.setattr(Engine, "destroy", counted)
    host = EngineHost()
    with pytest.raises(error):
        host.open(manifest)
    assert len(destroyed) == 1 and destroyed[0] is not None
    assert (host.engine, host.shim) == (None, None)


def test_a_second_scene_is_refused_and_the_engine_kept(host, scene):
    """The shim holds the handle, so refusing must not destroy it."""
    with pytest.raises(EngineError, match="already loaded"):
        host.engine.load_scene(scene)
    render(host.shim, 4800)
    assert host.shim.stats().render_errors == 0


def test_a_scene_with_mixed_rates_is_refused_by_the_engine(tmp_path):
    manifest = scene_fixture.make_scene(tmp_path, rates={"air": 44_100})
    with pytest.raises(EngineError) as refused:
        Engine().load_scene(manifest)
    assert refused.value.name == "PRISM_ERROR_IO"


def test_the_generated_scene_is_48_khz_with_exact_loops(scene):
    for stem in scene_fixture.STEMS:
        with wave.open(str(scene.parent / f"{stem}.wav"), "rb") as stem_file:
            assert stem_file.getframerate() == RATE
            assert stem_file.getnframes() == scene_fixture.loop_frames(stem)
    manifest = json.loads(scene.read_text(encoding="utf-8"))
    assert manifest["default_scene"] == manifest["scenes"][0]["id"]


# --- without prism_start -------------------------------------------------------------------------


def test_the_engine_renders_without_prism_start(host):
    """A loaded scene and prism_render are all it needs. Before any override: bed and sub."""
    out, tap = render(host.shim, 3 * RATE)
    stats = host.shim.stats()
    rms = meters.db(float(np.sqrt(np.mean(tap[RATE:].astype(np.float64) ** 2))))
    print(f"no prism_start: engine RMS {rms:.1f} dBFS, render errors {stats.render_errors}")
    assert stats.render_errors == 0, "prism_render refused to render without prism_start"
    assert rms > -40.0, "the engine rendered silence without prism_start"
    assert stats.frames_rendered == 3 * RATE


def settled_after(host, psv, seconds=6.0, keep=2.0):
    """The tap's last `keep` seconds after holding `psv` for `seconds`."""
    host.engine.set_mood_override(*psv)
    _, tap = render(host.shim, round(seconds * RATE))
    return tap[-round(keep * RATE) :]


def test_the_mood_override_is_honoured_without_prism_start(host):
    """Dark (300 Hz, every gate closed) against loudest (12 kHz, every gate open), after the
    cutoff's 0.6 s smoothing and a loop boundary for pulse and air."""
    host.shim.set_session_gain(1.0, 0.0)
    dark = meters.band_energy(settled_after(host, DARK), 2000, 10_000)
    bright = meters.band_energy(settled_after(host, LOUDEST), 2000, 10_000)
    difference = meters.power_db(bright / dark)
    print(f"override without prism_start: 2-10 kHz energy {difference:.1f} dB brighter")
    assert difference >= 30.0, (
        f"the mood override is NOT honoured without prism_start: dark and bright differ by "
        f"only {difference:.1f} dB above 2 kHz"
    )


def rbj_lowpass_gain(hz, cutoff_hz, q=0.7):
    """The engine's master low-pass (pgae/include/pgae/detail/dsp.h, BiquadLowpass) at hz."""
    w0 = 2 * math.pi * cutoff_hz / RATE
    alpha = math.sin(w0) / (2 * q)
    a0 = 1 + alpha
    b0 = b2 = (1 - math.cos(w0)) / 2 / a0
    b1 = (1 - math.cos(w0)) / a0
    a1, a2 = -2 * math.cos(w0) / a0, (1 - alpha) / a0
    z1 = np.exp(-2j * np.pi * np.asarray(hz) / RATE)
    return np.abs((b0 + b1 * z1 + b2 * z1**2) / (1 + a1 * z1 + a2 * z1**2))


@pytest.mark.parametrize("psv", [DARK, (0.45, 0.8, 0.5), (0.33, 0.5, 0.5)])
def test_the_low_pass_lands_where_the_mirror_maps_the_override(host, psv):
    """Every gate closed, so only bed and sub sound. The bed's harmonics 1/k through a low-pass at
    the mirror's cutoff: each harmonic against the fundamental, within 0.5 dB, wherever the
    prediction is within 60 dB of it."""
    params = map_effective(*psv)
    assert not params.gates["pulse"]
    tap = settled_after(host, psv, seconds=8.0, keep=3.0)
    harmonics = np.arange(1, scene_fixture.BED_HARMONICS + 1)
    hz = scene_fixture.BED_HZ * harmonics
    measured = np.array([meters.tone_amplitude(tap, f) for f in hz])
    predicted = rbj_lowpass_gain(hz, params.cutoff_hz) / harmonics
    measured_db = 20 * np.log10(measured / measured[0])
    predicted_db = 20 * np.log10(predicted / predicted[0])
    used = predicted_db >= -60.0
    worst = float(np.max(np.abs(measured_db - predicted_db)[used]))
    print(f"{psv}: cutoff {params.cutoff_hz:.0f} Hz, {used.sum()} harmonics, worst {worst:.3f} dB")
    assert used.sum() >= 5
    assert worst <= 0.5, f"the override {psv} did not put the low-pass at {params.cutoff_hz:.0f} Hz"


def pulse_envelope(tap, per=RATE // 4):
    """The pulse tone's level per 250 ms burst period: the tap moved down by 880 Hz, through a
    Gaussian low-pass (8 Hz, so 60 ms of smear and bed harmonics 80 Hz away gone by 200 dB), its
    largest magnitude in each period."""
    n = len(tap)
    baseband = tap.astype(np.float64) * np.exp(
        -2j * np.pi * scene_fixture.PULSE_HZ * np.arange(n) / RATE
    )
    freqs = np.fft.fftfreq(n, 1.0 / RATE)
    envelope = np.abs(np.fft.ifft(np.fft.fft(baseband) * np.exp(-0.5 * (freqs / 8.0) ** 2)))
    periods = n // per
    return np.arange(periods) * per, envelope[: periods * per].reshape(periods, per).max(axis=1)


def test_the_pulse_stem_follows_its_gate_at_its_loop_boundary(host):
    """Closed at density 0.337 across a boundary; opened mid-loop at 0.368, silent until the next
    boundary, full 1.5 s after it; closed mid-loop again, sounding until the next boundary, gone
    1.5 s after it."""
    loop = scene_fixture.loop_frames("pulse")  # 108000
    opened = (0.52, 0.65, 0.5)
    assert map_effective(*opened).gates["pulse"] and not map_effective(*opened).gates["air"]
    assert not map_effective(*BASELINE_POSE).gates["pulse"]
    fade = round(1.5 * RATE)
    host.engine.set_mood_override(*BASELINE_POSE)
    closed = render(host.shim, 140_400)[1]  # across the boundary at 108000
    host.engine.set_mood_override(*opened)  # consumed at 140400: the next boundary is 216000
    open_ = render(host.shim, 330_000 - 140_400)[1]
    host.engine.set_mood_override(*BASELINE_POSE)  # consumed at 330000: the next is 432000
    closed_again = render(host.shim, 600_000 - 330_000)[1]
    tap = np.concatenate([closed, open_, closed_again])
    starts, peaks = pulse_envelope(tap)
    ends = starts + RATE // 4
    steady = (starts >= 2 * loop + fade) & (ends <= 330_000)
    level = 20 * np.log10(peaks / np.median(peaks[steady]))
    # Clear of the envelope's 60 ms smear before a fade starts, and of the first and last second,
    # where the FFT joins the render's end to its start.
    smear = RATE // 10
    before = (starts >= RATE) & (ends <= 2 * loop - smear)
    after = (starts >= 4 * loop + fade) & (ends <= len(tap) - RATE)
    report = {
        "closed, up to the open boundary": level[before].max(),
        "the period from the open boundary": level[starts == 2 * loop][0],
        "open, spread": np.ptp(level[steady]),
        "the period up to the close boundary": level[ends == 4 * loop][0],
        "closed, from 1.5 s after the close boundary": level[after].max(),
    }
    print("pulse gate: " + ", ".join(f"{k} {v:.1f} dB" for k, v in report.items()))
    assert report["closed, up to the open boundary"] <= -60.0, "pulse sounded below its gate"
    assert report["the period from the open boundary"] >= -40.0, "pulse did not open"
    assert report["open, spread"] <= 0.5
    # Closing lowers pulse's gain by 0.8 dB at once; the gate itself waits for the boundary.
    assert report["the period up to the close boundary"] >= -3.0, "pulse closed early"
    assert report["closed, from 1.5 s after the close boundary"] <= -60.0, "pulse did not close"


# --- the high-pass on the real engine ------------------------------------------------------------


class Capture:
    """The render function handed to the shim: prism_render, and a copy of what it rendered before
    the high-pass. It runs where prism_render always runs, on the shim's audio thread."""

    def __init__(self, frames):
        self.lib = engine_library()
        self.raw = np.zeros(frames, np.float32)
        self.position = 0
        self.fn = RENDER_FN(self._render)

    def _render(self, core, out, frames):
        result = self.lib.prism_render(core, out, frames)
        address = ctypes.cast(out, ctypes.c_void_p).value
        ctypes.memmove(self.raw.ctypes.data + 4 * self.position, address, 4 * frames)
        self.position += frames
        return result


@pytest.mark.parametrize("psv", [LOUDEST, BASELINE_POSE])
def test_the_engines_36_to_62_hz_band_is_24_db_down_after_the_high_pass(scene, psv):
    """The sub stem carries 45 Hz on purpose. The same render before and after the high-pass."""
    frames = 20 * RATE
    engine = Engine()
    engine.load_scene(scene)
    capture = Capture(frames)
    shim = Shim(capture.fn, engine.handle)
    try:
        engine.set_mood_override(*psv)
        _, tap = render(shim, frames)
    finally:
        shim.close()
        engine.destroy()
    raw, tap = capture.raw[4 * RATE :], tap[4 * RATE :]
    band = meters.power_db(meters.band_energy(tap, 36, 62) / meters.band_energy(raw, 36, 62))
    in_band = meters.db(meters.tone_amplitude(tap, 45.0) / meters.tone_amplitude(raw, 45.0))
    d2 = meters.db(meters.tone_amplitude(tap, 73.4) / meters.tone_amplitude(raw, 73.4))
    print(f"engine at {psv}: 36-62 Hz {band:.2f} dB, 45 Hz {in_band:.2f} dB, 73.4 Hz {d2:+.3f} dB")
    assert band <= -24.0
    assert in_band <= -30.0
    assert abs(d2) <= 1.0


# --- the ceiling ---------------------------------------------------------------------------------


@pytest.mark.parametrize("trim_db", [-6.0, 0.0])
def test_four_minutes_at_the_loudest_psv_with_the_loudest_heartbeat_stay_under_the_ceiling(
    scene, trim_db
):
    """Every gate open, 12 kHz, session gain 1, the heartbeat layer at -9 dBFS (2.6's loudest) with
    a beat every 400 ms. The reference meter over all four minutes. At the -6 dB trim prompt 2.5
    starts from, the engine's own -3 dBFS limiter and the trim keep the sum under the ceiling
    before the shim's limiter has anything to do; at 0 dB, the most the ABI allows and where
    Week B's tuning could go, the shim's limiter holds it."""
    seconds = 240
    total = (seconds + 1) * RATE  # the last second is context for the meter
    config = Shim.config_default()
    config.engine_trim_db = trim_db
    host = EngineHost(config)
    host.open(scene)
    shim = host.shim
    host.engine.set_mood_override(*LOUDEST)
    shim.set_session_gain(1.0, 0.0)
    shim.set_heartbeat_level(-9.0, 0.0)
    shim.set_clock_anchor(0.0, 0)
    out = np.zeros(total, np.float32)
    tap = np.zeros(total, np.float32)
    beat, pushed = 1, 0
    try:
        for start in range(0, total, RATE):
            end = min(start + RATE, total)
            while beat * 400 * 48 - LATENCY < end:  # every beat whose voice starts in this second
                shim.push_beat(beat * 400.0, 400.0)
                beat, pushed = beat + 1, pushed + 1
            shim.render_offline(out[start:end], tap[start:end])
        stats = shim.stats()
    finally:
        host.close()
    peak = meters.true_peak_dbtp(out, 0, seconds * RATE)
    print(
        f"four minutes at the loudest PSV, trim {trim_db} dB: {peak:.3f} dBTP, sample peak "
        f"{meters.sample_peak_dbfs(out):.2f} dBFS, engine sample peak "
        f"{meters.sample_peak_dbfs(tap):.2f} dBFS, beats {stats.beats_played}, limiter active "
        f"{stats.limiter_active_frames} frames, min gain {stats.limiter_min_gain:.4f}"
    )
    assert (stats.beats_played, stats.beats_dropped_late, stats.beats_dropped_full) == (
        pushed,
        0,
        0,
    )
    assert stats.render_errors == 0 and stats.frames_rendered == total
    assert meters.sample_peak_dbfs(tap) >= -3.5  # the engine was at its own -3 dBFS limiter
    assert peak <= CEILING_DBTP


# --- a fixture session through the feed ----------------------------------------------------------


class Events:
    def __init__(self):
        self.events = []

    def event(self, name, *, t_engine=None, **fields):
        self.events.append((name, t_engine, fields))


def phase_state(t_engine, segment, elapsed, arousal=0.9):
    return {
        "type": "state",
        "t_engine": t_engine,
        "segment": segment,
        "segment_elapsed_ms": elapsed,
        "segment_nominal_ms": 45_000 if segment == "baseline" else 75_000,
        "psv": {
            "arousal": arousal,
            "valence": 0.5,
            "cognitive_load": 0.5,
            "readiness": 0.5,
        },
        "confidence": {
            "arousal": 1.0,
            "valence": 0.0,
            "cognitive_load": 1.0,
            "readiness": 1.0,
        },
        "authority": {
            "arousal": 1.0,
            "valence": 0.0,
            "cognitive_load": 1.0,
            "readiness": 1.0,
        },
    }


def tone_blocks(samples, hz, frames=2_400):
    """Complex-demodulated amplitude in 50 ms blocks (integer cycles at 880 and 4 kHz)."""
    count = len(samples) // frames
    blocks = np.asarray(samples[: count * frames], np.float64).reshape(count, frames)
    window = np.hanning(frames)
    oscillator = window * np.exp(-2j * np.pi * hz * np.arange(frames) / RATE)
    return np.abs(blocks @ oscillator) * (2.0 / np.sum(window))


def windowed_tone(samples, hz):
    window = np.hanning(len(samples))
    oscillator = window * np.exp(-2j * np.pi * hz * np.arange(len(samples)) / RATE)
    return float(abs(np.asarray(samples, np.float64) @ oscillator) * (2.0 / np.sum(window)))


def circular_samples(samples, start, count):
    indices = (start + np.arange(count)) % len(samples)
    return samples[indices]


def correlation_peak(stem_fft, capture, loop_frames):
    padded = np.zeros(loop_frames, np.float64)
    padded[: len(capture)] = np.asarray(capture, np.float64) * np.hanning(len(capture))
    correlation = np.fft.irfft(np.conj(stem_fft) * np.fft.rfft(padded), loop_frames)
    return int(np.argmax(np.abs(correlation)))


def circular_distance(a, b, modulus):
    return abs((a - b + modulus // 2) % modulus - modulus // 2)


def test_aligned_fixture_audio_has_ordered_gates_and_continuous_bed_phase(phase_scene):
    """Prompt 2.7 through the real engine and shim, on exact production-length test loops.

    The temporary stems isolate pulse at 880 Hz and air at 4 kHz. The bed is a unique 19 s
    texture, so its circular cross-correlation against its own file detects any phase restart.
    """
    host = EngineHost()
    host.open(phase_scene)
    shim, engine = host.shim, host.engine
    phase, log = PhaseTracker.for_shim(shim), Events()
    feed = PsvFeed(engine, log, "pose", phase=phase)
    shim.set_session_gain(1.0, 0.0)

    baseline_frame = PULSE_ALIGNMENT_PHASE
    expected_plan = phase.session_gate_plan(baseline_frame)
    audio_end = expected_plan.pulse_close.boundary_frame + FADE_FRAMES + 2 * RATE
    position = 0
    tap = np.zeros(audio_end, np.float32)

    def render_into(target):
        nonlocal position
        while position < target:
            end = min(target, position + RATE)
            out = np.empty(end - position, np.float32)
            shim.render_offline(out, tap[position:end] if tap is not None else None)
            position = end

    try:
        feed.on_state(phase_state(0, "idle", 0))
        render_into(baseline_frame)
        feed.on_state(phase_state(0, "baseline", 0))
        plan = feed.gate_plan
        assert plan == expected_plan

        render_into(plan.pulse_open.send_frame)
        feed.tick((position - baseline_frame) / 48)
        render_into(plan.load_frame)
        feed.on_state(phase_state(56_000, "load", 0))
        render_into(plan.air_close.send_frame)
        feed.tick((position - baseline_frame) / 48)
        render_into(plan.regulate_frame)
        feed.on_state(phase_state(131_000, "regulate", 0))
        render_into(audio_end)

        pulse_hz, air_hz = (
            scene_fixture.PHASE_TONES["pulse"],
            scene_fixture.PHASE_TONES["air"],
        )
        baseline = tap[baseline_frame : plan.load_frame]
        opened = tap[
            plan.load_frame + FADE_FRAMES + RATE // 4 : plan.load_frame + FADE_FRAMES + RATE
        ]
        pulse_open = meters.tone_amplitude(opened, pulse_hz)
        pulse_floor = float(np.max(tone_blocks(baseline, pulse_hz)))
        before = windowed_tone(
            tap[plan.load_frame - phase.block_frames : plan.load_frame], pulse_hz
        )
        first = windowed_tone(
            tap[plan.load_frame : plan.load_frame + phase.block_frames], pulse_hz
        )
        after_close = tap[
            plan.pulse_close.boundary_frame + FADE_FRAMES :
            plan.pulse_close.boundary_frame + FADE_FRAMES + RATE
        ]
        assert pulse_floor < pulse_open * 1e-3
        assert before < pulse_open * 1e-3
        assert first > max(before * 20.0, pulse_open * 1e-4)
        assert meters.tone_amplitude(after_close, pulse_hz) < pulse_open * 1e-3

        # Across the whole measured session, audible air always has audible pulse under it.
        measured = tap[baseline_frame:audio_end]
        pulse_level = tone_blocks(measured, pulse_hz)
        air_level = tone_blocks(measured, air_hz)
        pulse_threshold = float(np.max(pulse_level)) * 0.01
        air_threshold = float(np.max(air_level)) * 0.01
        assert np.max(air_level) > max(float(np.max(tone_blocks(baseline, air_hz))) * 100, 1e-5)
        assert not np.any((air_level > air_threshold) & (pulse_level <= pulse_threshold))
        assert plan.air_close.boundary_frame + FADE_FRAMES < (
            plan.pulse_close.boundary_frame + FADE_FRAMES
        )

        crossings = [fields for name, _, fields in log.events if name == "engine_gate_crossing"]
        assert [event["late_frames"] for event in crossings] == [0, 0]
        assert not any(event["missed_boundary"] for event in crossings)
        psv_log = [fields for name, _, fields in log.events if name == "engine_psv"]
        assert [record["reason"] for record in psv_log if record["reason"] != "state"] == [
            "load_pulse_open",
            "regulate_air_close",
        ]
        assert next(record for record in psv_log if record["segment"] == "baseline")[
            "hysteresis"
        ]["gates"] == {"pulse": False, "air": False}

        # Render the worst-case 281 s session without retaining it. Reset uses the same baseline
        # filter as the early capture, then both captures are correlated against the bed file.
        early_start, capture_frames = 7 * RATE, 2 * RATE
        early = tap[early_start : early_start + capture_frames].copy()
        session_end = baseline_frame + 281 * RATE
        tap = None
        render_into(session_end)
        feed.on_state(phase_state(281_000, "reset", 0))
        render_into(session_end + 6 * RATE)
        final_start = position
        final = np.empty(capture_frames, np.float32)
        out = np.empty(capture_frames, np.float32)
        shim.render_offline(out, final)
        position += capture_frames

        with wave.open(str(phase_scene.parent / "bed.wav"), "rb") as wav:
            raw = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float64)
        raw /= 32767.0
        stem_fft = np.fft.rfft(raw)
        early_raw = circular_samples(raw, early_start, capture_frames)
        final_raw = circular_samples(raw, final_start, capture_frames)
        expected_delta = (
            correlation_peak(stem_fft, final_raw, len(raw))
            - correlation_peak(stem_fft, early_raw, len(raw))
        ) % len(raw)
        measured_delta = (
            correlation_peak(stem_fft, final, len(raw))
            - correlation_peak(stem_fft, early, len(raw))
        ) % len(raw)
        assert circular_distance(measured_delta, expected_delta, len(raw)) <= 8
        assert shim.frames_rendered() == position
    finally:
        host.close()


def test_a_fixture_session_through_the_feed_drives_the_real_engine(scene):
    """Every state message of tools/fixtures/synthetic-clean.jsonl through PsvFeed and SessionGain
    into the engine and the shim, audio rendered up to each message's t_engine, the source
    switched to pose as regulate begins. Output frame f leaves at f / 48 ms. Every override and
    gain command is accepted; resolve's last 10 s and everything after are digital silence (no
    beats are pushed here); the whole session stays under the ceiling."""
    records = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]
    states = [r["msg"] for r in records if r["dir"] == "out" and r["msg"]["type"] == "state"]
    host = EngineHost()
    host.open(scene)
    log = Events()
    feed = PsvFeed(host.engine, log, phase=PhaseTracker.for_shim(host.shim))
    gain = SessionGain(host.shim, log)
    host.shim.set_clock_anchor(0.0, 0)
    end_ms = states[-1]["t_engine"] + 2000
    out = np.zeros(round(end_ms * 48), np.float32)
    done = 0
    try:
        for msg in states:
            frame = round(msg["t_engine"] * 48)
            if frame > done:
                host.shim.render_offline(out[done:frame])
                done = frame
            if msg["segment"] == "regulate" and feed.source == "body":
                feed.set_source("pose", t_engine=msg["t_engine"])
            feed.on_state(msg)
            gain.on_state(msg)
        host.shim.render_offline(out[done:])
        stats = host.shim.stats()
    finally:
        host.close()
    resolve = next(m for m in states if m["segment"] == "resolve")
    silent_from_ms = resolve["t_engine"] - resolve["segment_elapsed_ms"] + 35_000
    silent_from = round(silent_from_ms * 48) + LATENCY + 1
    sounding = out[: silent_from - LATENCY - 48]
    peak = meters.true_peak_dbtp(out)
    overrides = sum(1 for name, _, _ in log.events if name == "engine_psv")
    print(
        f"fixture session: {overrides} overrides, {stats.frames_rendered} frames, "
        f"{peak:.2f} dBTP, silent from {silent_from} of {len(out)}"
    )
    assert overrides == len(states)
    assert stats.render_errors == 0
    assert np.all(out[silent_from:] == 0.0)
    assert np.any(sounding[-RATE:] != 0.0)  # it was the ramp that silenced it, not a cut
    assert peak <= CEILING_DBTP


# --- EngineHost ----------------------------------------------------------------------------------


def test_start_gives_the_shim_t_engine_before_starting_the_stream(host, monkeypatch):
    calls = []
    monkeypatch.setattr(Shim, "set_time_origin_ns", lambda self, ns: calls.append(("origin", ns)))
    monkeypatch.setattr(Shim, "start", lambda self: calls.append(("start",)))
    host.start()
    assert calls == [("origin", clock._ORIGIN_NS), ("start",)]


def test_start_resumes_both_level_controllers_once_the_stream_has_started(host, monkeypatch):
    """A refused start leaves both holding: nothing goes into a stream that is not running."""
    calls = []
    gain = SimpleNamespace(resume=lambda: calls.append(("resume gain",)))
    heartbeat = SimpleNamespace(resume=lambda: calls.append(("resume heartbeat",)))
    monkeypatch.setattr(Shim, "set_time_origin_ns", lambda self, ns: calls.append(("origin", ns)))
    monkeypatch.setattr(Shim, "start", lambda self: calls.append(("start",)))
    host.start(gain, heartbeat)
    assert calls == [
        ("origin", clock._ORIGIN_NS),
        ("start",),
        ("resume gain",),
        ("resume heartbeat",),
    ]
    calls.clear()

    def refused(self):
        raise ShimError("pls_start", 6)

    monkeypatch.setattr(Shim, "start", refused)
    with pytest.raises(ShimError, match="PLS_ERROR_SAMPLE_RATE"):
        host.start(gain, heartbeat)
    assert calls == [("origin", clock._ORIGIN_NS)]


PERIOD = 480  # a 10 ms device period: the device renders this many frames per callback
# A sleep can end before the device has rendered what it asked for: by one period, since the device
# renders a period at a time, and by one 15.6 ms clock tick, since asyncio fires timers that early.
EARLY = PERIOD + 750


def state(t, segment, elapsed=0, nominal=75_000):
    return {
        "t_engine": t,
        "segment": segment,
        "segment_elapsed_ms": elapsed,
        "segment_nominal_ms": nominal,
    }


# The stop starts at t_engine 1000 ms. Each case: the state message SessionGain saw before it, and
# what arrives halfway through its ramp, each of which would re-target the gain if nothing held it.
STOP_CASES = {
    "no SessionGain": None,
    "a reset during the wait": (state(0, "regulate"), state(2_500, "reset", 0, 3_000)),
    "resolve past T-22 s during the wait": (
        state(0, "resolve", 21_000, 45_000),
        state(2_500, "resolve", 23_500, 45_000),
    ),
    "idle to baseline during the wait": (state(0, "idle"), state(2_500, "baseline", 0, 56_000)),
}


@pytest.mark.parametrize("case", list(STOP_CASES))
def test_stop_fades_to_digital_silence_before_the_stream_stops(host, monkeypatch, case):
    """The session gain and the heartbeat fade over 3 s, and the stream stops only once the end of
    that fade has left the limiter: what the device played last is digital silence, and it was a
    fade, not a cut. Rendered as a device would: in 10 ms callbacks, the commands taken one whole
    block after frames_rendered was read, sleeps ending up to a period and a clock tick short of
    what they asked, and nothing rendered after the stop. With a SessionGain, a state message
    arriving during the wait sends nothing: stop's fades are the only two level commands."""
    shim = host.shim
    block = shim.config.max_block_frames
    host.engine.set_mood_override(*LOUDEST)
    shim.set_session_gain(1.0, 0.0)
    shim.set_heartbeat_level(-9.0, 0.0)
    shim.set_clock_anchor(0.0, 0)
    for k in range(1, 15):
        shim.push_beat(k * 400.0, 400.0)
    gain = during = None
    if STOP_CASES[case] is not None:
        before, during = STOP_CASES[case]
        would = SessionGain(SimpleNamespace(set_session_gain=lambda *_: None), Events())
        would.on_state(before)
        assert would.on_state(during) is not None  # the case matters: unheld, it re-targets
        gain = SessionGain(shim, Events())
        gain.on_state(before)
    heartbeat = HeartbeatLevel(shim, Events())
    out = np.zeros(6 * RATE, np.float32)
    done = 0

    def play(frames, size=PERIOD):
        nonlocal done
        for _ in range(frames // size):
            shim.render_offline(out[done : done + size])
            done += size

    play(RATE)
    commands, heartbeat_commands = [], []
    set_session_gain = Shim.set_session_gain
    set_heartbeat_level = Shim.set_heartbeat_level

    def recorded(self, target, ramp_ms):
        commands.append((target, ramp_ms))
        set_session_gain(self, target, ramp_ms)

    def recorded_heartbeat(self, target_dbfs, ramp_ms):
        heartbeat_commands.append((target_dbfs, ramp_ms))
        set_heartbeat_level(self, target_dbfs, ramp_ms)

    frames_rendered, reads = Shim.frames_rendered, []

    def read_mid_block(self):
        # The first read is stop's, before it pushes: the audio thread is inside a block that has
        # already taken its commands, so they wait for the next one.
        reads.append(frames_rendered(self))
        if len(reads) == 1:
            play(block, block)
        return reads[-1]

    sleeps, stops, sent_during = [], [], []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            frames = round(seconds * RATE) - EARLY
            play(frames // 2)
            if during is not None:
                sent_during.append(gain.on_state(during))
                assert heartbeat.on_state(during) is None
            play(frames - frames // 2)
        else:
            play(PERIOD)

    monkeypatch.setattr(Shim, "set_session_gain", recorded)
    monkeypatch.setattr(Shim, "set_heartbeat_level", recorded_heartbeat)
    monkeypatch.setattr(Shim, "frames_rendered", read_mid_block)
    # Output frame f plays at f / 48 ms.
    monkeypatch.setattr(clock, "t_engine_ms", lambda: done / 48)
    monkeypatch.setattr(engine_module, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(Shim, "stop", lambda self: stops.append(frames_rendered(self)))
    asyncio.run(host.stop(gain, heartbeat))
    assert len(stops) == 1 and frames_rendered(shim) == stops[0] == done
    played = out[: stops[0]]
    ramp = round(STOP_RAMP_MS * 48)
    silent_from = RATE + block + ramp - 1 + LATENCY  # the commands were taken at RATE + block
    print(f"{case}: stopped at {stops[0]}, silent from {silent_from}, {len(sleeps) - 1} polls")
    assert np.all(played[-block:] == 0.0), "the stream stopped while the fade was still sounding"
    assert np.any(played[silent_from - RATE // 4 : silent_from] != 0.0), "a cut, not a fade"
    halfway = out[round(2.5 * RATE) + LATENCY - RATE // 4 : round(2.5 * RATE) + LATENCY]
    assert 0.0 < np.max(np.abs(halfway)) < np.max(np.abs(out[LATENCY : RATE // 2 + LATENCY]))
    assert commands == [(0.0, STOP_RAMP_MS)], "the session gain was re-targeted during the wait"
    assert heartbeat_commands == [(-math.inf, STOP_RAMP_MS)]
    assert sent_during == ([] if during is None else [None])
    assert gain is None or gain.holding
    assert heartbeat.holding
    faded_out = RATE + 2 * block + ramp + LATENCY
    assert faded_out <= stops[0] < faded_out + PERIOD
    assert sleeps[0] == STOP_RAMP_MS / 1000 and set(sleeps[1:]) == {STOP_POLL_S}


def test_stop_stops_a_stream_that_is_not_advancing_at_its_deadline(host, monkeypatch):
    """Never started, or its endpoint lost: frames_rendered never reaches the end of the fade, and
    the bridge must still shut down."""
    now = [0.0]
    sleeps, stops = [], []

    async def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds * 1000.0

    monkeypatch.setattr(clock, "t_engine_ms", lambda: now[0])
    monkeypatch.setattr(engine_module, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(Shim, "stop", lambda self: stops.append(now[0]))
    asyncio.run(host.stop())
    assert stops == [pytest.approx(STOP_RAMP_MS + STOP_DEADLINE_MS)]
    assert len(sleeps) == 1 + round(STOP_DEADLINE_MS / 1000 / STOP_POLL_S)
    assert host.shim.frames_rendered() == 0


def test_close_frees_the_shim_before_the_engine(host, monkeypatch):
    order = []
    shim_close, engine_destroy = Shim.close, Engine.destroy
    monkeypatch.setattr(Shim, "close", lambda self: (order.append("shim"), shim_close(self)))
    monkeypatch.setattr(
        Engine, "destroy", lambda self: (order.append("engine"), engine_destroy(self))
    )
    host.close()
    host.close()
    assert order == ["shim", "engine"]


def test_the_override_goes_out_with_valence_half_confidence_one_and_no_hint(host):
    engine = host.engine
    sent = []
    real = engine._lib

    class Spy:
        def __getattr__(self, name):
            return getattr(real, name)

        def prism_set_mood_override(self, core, override):
            o = override._obj
            sent.append(
                (o.mode_hint, o.arousal, o.valence, o.cognitive_load, o.readiness, o.confidence)
            )
            return real.prism_set_mood_override(core, override)

    engine._lib = Spy()
    try:
        engine.set_mood_override(0.25, 0.75, 0.125)
        with pytest.raises(EngineError) as refused:
            engine.set_mood_override(1.5, 0.5, 0.5)
    finally:
        engine._lib = real
    assert sent[0] == (None, 0.25, 0.5, 0.75, 0.125, 1.0)
    assert refused.value.name == "PRISM_ERROR_INVALID_ARGUMENT"


# --- nothing starts the engine or its device -----------------------------------------------------

NEVER_CALLED = ("prism_start", "prism_device_start", "prism_device_stop")
NEVER_CALLED_RE = re.compile(r"\b(" + "|".join(NEVER_CALLED) + r")\b")


def python_code_tokens(source):
    """(line, text) of every name and every string literal in Python source, except comments and
    docstrings, which may name anything."""
    docstrings = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add((first.lineno, first.col_offset))
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.NAME or (
            token.type == tokenize.STRING and token.start not in docstrings
        ):
            yield token.start[0], token.string


def test_nothing_in_prism_live_calls_prism_start_or_the_engines_device():
    """Hard rule 8 and prompt 2.5: the bridge is the engine's only PSV writer and the shim owns the
    stream. A comment or a docstring may name these calls; code may not, not even as a string. In
    Python, comments and docstrings are told apart by the tokenizer and the syntax tree; in C and
    C++, by stripping // and /* */ comments while keeping string literals. This file is the one
    exception: it has to spell the names to look for them."""
    offenders = []
    for folder in ("bridge", "tools", "tests"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            if path == Path(__file__).resolve() or "__pycache__" in path.parts:
                continue
            for line, text in python_code_tokens(path.read_text(encoding="utf-8")):
                if NEVER_CALLED_RE.search(text):
                    offenders.append(f"{path.relative_to(ROOT)}:{line}: {text}")
    for folder in ("native/src", "native/include", "native/tests"):
        for path in sorted((ROOT / folder).rglob("*")):
            if path.suffix in {".c", ".cpp", ".h", ".hpp"}:
                code = c_code(path.read_text(encoding="utf-8"), keep_strings=True)
                offenders += [
                    f"{path.relative_to(ROOT)}: {m}" for m in NEVER_CALLED_RE.findall(code)
                ]
    assert offenders == []


def test_the_bridge_never_calls_prism_render_itself():
    """bridge/engine.py takes prism_render's address for the shim; only the shim's audio thread
    calls it. And native/src names no engine function in code at all: it calls whatever render
    function it was given."""
    calls = []
    for path in sorted((ROOT / "bridge").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", None))
                if name == "prism_render":
                    calls.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert calls == []
    engine_calls = re.compile(r"\bprism_(render|start|stop|device_\w+|create|destroy|load_scene)\b")
    for path in sorted((ROOT / "native" / "src").iterdir()):
        assert not engine_calls.search(c_code(path.read_text(encoding="utf-8"))), path.name
