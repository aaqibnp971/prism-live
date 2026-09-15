"""native/: the audio shim through its C ABI (prompts 2.5-2.6). No test opens a device.

Every render runs on the test's thread through pls_render_offline, the chain the device callback
runs. The render function here is Python, a ctypes callback over a numpy signal, so the chain's
input is known sample for sample; tests/test_engine.py renders the real engine. Levels are read
with tests/audio_meters.py, which shares no code with the shim.

What is pinned:

- the committed DLL is built from the checkout (its source hash), imports nothing but KERNEL32,
  ole32 and the Universal C Runtime, and native/README.md describes it;
- frames_rendered is exact, gain ramps land exactly, beats leave on their anchored frame, late
  and surplus beats are dropped and counted, fixture beats do not overlap, a full queue refuses
  and queues nothing;
- across the supported voice lengths, at least 97.8% of heartbeat energy stays in 36 to 62 Hz;
  energy above 62 Hz and measured components at 120 Hz, 250 Hz and 1 kHz are pinned, and the final
  three-second fade is equal-power and reaches digital silence on the output timeline;
- the high-pass: the committed coefficients meet the design, and white noise, a 36 to 62 Hz sweep
  and tones through the chain show the 36 to 62 Hz band at least 24 dB down and 73.4 Hz and up
  within 1 dB;
- the limiter: adversarial material never above -1.0 dBTP by the reference meter, and well below
  the ceiling the chain is a pure delay;
- the audio thread: the allocation tripwire (native/tests/rt_tripwire_test.cpp), built from the
  checkout, passes, and its sources name none of the usual calls that allocate, lock, wait, log or
  do I/O.
"""

import ctypes
import hashlib
import json
import math
import re
import struct
import subprocess
import sys
from pathlib import Path

import audio_meters as meters
import numpy as np
import pytest
from conftest import c_code

from bridge.engine import RENDER_FN, SHIM_DLL, Shim, ShimError
from bridge.engine_feed import (
    HEARTBEAT_CHAIN_LATENCY_MS,
    HEARTBEAT_FINAL_FADE_MS,
    HEARTBEAT_RESTART_RAMP_MS,
    HeartbeatLevel,
)

if sys.platform != "win32":
    pytest.skip("the shim is a Windows DLL", allow_module_level=True)

ROOT = Path(__file__).resolve().parent.parent
NATIVE = ROOT / "native"
README = NATIVE / "README.md"
TRIPWIRE = NATIVE / "build" / "rt_tripwire_test.exe"
RATE = 48_000
LATENCY = Shim.latency_frames()
CEILING_DBTP = -1.0
FIXTURE = ROOT / "tools" / "fixtures" / "synthetic-clean.jsonl"


class Signal:
    """A render function playing `samples` from its first call, then silence. Records its blocks."""

    def __init__(self, samples=()):
        self.samples = np.ascontiguousarray(samples, dtype=np.float32)
        self.position = 0
        self.blocks = []
        self.fn = RENDER_FN(self._render)

    def _render(self, core, out, frames):
        address = ctypes.cast(out, ctypes.c_void_p).value
        count = max(0, min(frames, len(self.samples) - self.position))
        if count:
            ctypes.memmove(address, self.samples.ctypes.data + 4 * self.position, 4 * count)
        ctypes.memset(address + 4 * count, 0, 4 * (frames - count))
        self.position += frames
        self.blocks.append(frames)
        return 0


def open_shim(signal, **config):
    settings = Shim.config_default()
    for name, value in config.items():
        setattr(settings, name, value)
    return Shim(signal.fn, config=settings)


def render(shim, frames, chunk=RATE):
    """(out, tap) for the next `frames` frames, in calls of `chunk` frames."""
    out = np.zeros(frames, np.float32)
    tap = np.zeros(frames, np.float32)
    for start in range(0, frames, chunk):
        shim.render_offline(out[start : start + chunk], tap[start : start + chunk])
    return out, tap


# --- the committed DLL ---------------------------------------------------------------------------


def checkout_source_hash():
    """native/CMakeLists.txt's recipe: CMakeLists.txt itself and every file under include/, src/
    and third_party/, sorted by path relative to native/ with forward slashes, each as path + "\\n"
    + contents with CRLF as LF + "\\n", concatenated."""
    paths = sorted(
        ["CMakeLists.txt"]
        + [
            path.relative_to(NATIVE).as_posix()
            for folder in ("include", "src", "third_party")
            for path in (NATIVE / folder).rglob("*")
            if path.is_file()
        ]
    )
    digest = hashlib.sha256()
    for relative in paths:
        contents = (NATIVE / relative).read_bytes().replace(b"\r\n", b"\n")
        digest.update(relative.encode() + b"\n" + contents + b"\n")
    return digest.hexdigest()


def test_the_committed_dll_is_built_from_the_checkout():
    assert Shim.source_hash() == checkout_source_hash(), (
        "native/bin/libprism_live_shim.dll was built from other sources: rebuild (native/README.md)"
    )


def pe_imported_dlls(path):
    """The DLL names in a PE32+ file's import and delay-import tables, in table order."""
    data = path.read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    assert data[pe : pe + 4] == b"PE\0\0", f"{path.name} is not a PE file"
    sections, optional_size = struct.unpack_from("<2xH12xH", data, pe + 4)
    optional = pe + 24
    assert struct.unpack_from("<H", data, optional)[0] == 0x20B, f"{path.name} is not PE32+"
    table = optional + optional_size

    def offset(rva):
        for k in range(sections):
            size, address, raw_size, raw = struct.unpack_from("<4I", data, table + 40 * k + 8)
            if address <= rva < address + max(size, raw_size):
                return rva - address + raw
        raise AssertionError(f"RVA {rva:#x} is in no section of {path.name}")

    def name(rva):
        start = offset(rva)
        return data[start : data.index(b"\0", start)].decode("ascii")

    names = []
    # Data directories 1 (imports: 20-byte descriptors, the name's RVA 4th) and 13 (delay imports:
    # 32-byte descriptors, the name's RVA 2nd), each list ending on a descriptor of zeros.
    for directory, size, name_field in ((1, 20, 3), (13, 32, 1)):
        rva = struct.unpack_from("<I", data, optional + 112 + 8 * directory)[0]
        if rva == 0:
            continue
        at = offset(rva)
        while any(descriptor := struct.unpack_from(f"<{size // 4}I", data, at)):
            names.append(name(descriptor[name_field]))
            at += size
    return names


def test_the_committed_dll_imports_only_windows_system_libraries():
    """No MinGW runtime (libstdc++, libgcc, libwinpthread) and nothing else to install: a booth
    laptop runs the DLL as cloned. A correct rebuild with a README to match would still pass the
    hash tests, so the imports are read from the file. ole32 is Windows' COM runtime, used only by
    the non-audio IAudioClock polling thread."""
    names = pe_imported_dlls(SHIM_DLL)
    print(f"{SHIM_DLL.name} imports: {', '.join(names)}")
    assert "kernel32.dll" in (n.lower() for n in names)
    universal_crt = re.compile(r"api-ms-win-crt-[a-z0-9-]+\.dll")
    others = [
        n
        for n in names
        if n.lower() not in ("kernel32.dll", "ole32.dll")
        and not universal_crt.fullmatch(n.lower())
    ]
    assert others == []


def test_native_readme_describes_the_committed_dll():
    text = README.read_text(encoding="utf-8")
    assert f"`{hashlib.sha256(SHIM_DLL.read_bytes()).hexdigest()}`" in text
    assert f"`{Shim.source_hash()}`" in text
    assert f"{SHIM_DLL.stat().st_size:,} bytes" in text
    assert f"**{LATENCY} frames" in text


def test_abi_version_and_defaults():
    assert Shim.abi_version() == 3
    config = Shim.config_default()
    assert (config.sample_rate, config.max_block_frames, config.command_capacity) == (
        48_000,
        2048,
        256,
    )
    assert (config.beat_capacity, config.engine_trim_db) == (64, -6.0)


@pytest.mark.parametrize(
    "field, value, name",
    [
        ("sample_rate", 44_100, "PLS_ERROR_SAMPLE_RATE"),
        ("command_capacity", 3, "PLS_ERROR_INVALID_ARGUMENT"),
        ("max_block_frames", 0, "PLS_ERROR_INVALID_ARGUMENT"),
        ("beat_capacity", 0, "PLS_ERROR_INVALID_ARGUMENT"),
        ("engine_trim_db", 0.5, "PLS_ERROR_INVALID_ARGUMENT"),
        ("engine_trim_db", math.nan, "PLS_ERROR_INVALID_ARGUMENT"),
    ],
)
def test_open_refuses_a_bad_config(field, value, name):
    with pytest.raises(ShimError) as refused:
        open_shim(Signal(), **{field: value})
    assert refused.value.name == name


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.set_session_gain(1.01, 0.0),
        lambda s: s.set_session_gain(-0.01, 0.0),
        lambda s: s.set_session_gain(math.nan, 0.0),
        lambda s: s.set_session_gain(0.5, -1.0),
        lambda s: s.set_session_gain(0.5, math.inf),
        lambda s: s.set_heartbeat_level(0.1, 0.0),
        lambda s: s.set_heartbeat_level(math.nan, 0.0),
        lambda s: s.push_beat(1000.0, 249.0),
        lambda s: s.push_beat(1000.0, 2501.0),
        lambda s: s.push_beat(1000.0, 800.0, 2),
        lambda s: s.push_beat(math.nan, 800.0),
        lambda s: s.push_beat(math.inf, 800.0),
        lambda s: s.set_clock_anchor(math.nan, 0),
        lambda s: s.set_time_origin_ns(-1),
    ],
)
def test_control_calls_refuse_invalid_arguments(call):
    shim = open_shim(Signal())
    with pytest.raises(ShimError) as refused:
        call(shim)
    assert refused.value.name == "PLS_ERROR_INVALID_ARGUMENT"
    shim.close()


# --- counting, ramps, beats ----------------------------------------------------------------------


def test_frames_rendered_is_every_frame_passed_to_render():
    signal = Signal()
    shim = open_shim(signal, max_block_frames=480)
    chunks = [1, 479, 480, 481, 2048, 48_000, 7, 96_000]
    total = 0
    for frames in chunks:
        shim.render_offline(np.zeros(frames, np.float32))
        total += frames
        assert shim.frames_rendered() == total
    assert sum(signal.blocks) == total
    assert max(signal.blocks) == 480
    assert len(signal.blocks) == sum(math.ceil(frames / 480) for frames in chunks)
    assert shim.stats().frames_rendered == total
    assert shim.stats().render_errors == 0


class LinearRamp:
    """native/src/process.h's LinearRamp, step for step."""

    def __init__(self):
        self.value = self.start = self.target = 0.0
        self.length = self.position = 0

    def set(self, target, ramp_ms):
        frames = math.floor(ramp_ms * 48.0 + 0.5)  # ms_to_frames: llround, for ramp_ms >= 0
        if frames <= 0:
            self.value = self.start = self.target = target
            self.length = self.position = 0
        else:
            self.start, self.target, self.length, self.position = self.value, target, frames, 0

    def next(self):
        if self.position < self.length:
            self.position += 1
            self.value = (
                self.target
                if self.position == self.length
                else self.start + (self.target - self.start) * (self.position / self.length)
            )
        return self.value


def test_session_gain_ramps_are_exact_and_land_on_their_targets():
    """Each command is taken at the start of the block after it is pushed and ramps from wherever
    the gain is then, mid-ramp included. Output = tap x trim x gain, delayed, to the bit."""
    n = 30_000
    x = 0.8 * np.sin(2 * math.pi * 1000 * np.arange(n + LATENCY) / RATE)
    shim = open_shim(Signal(x), max_block_frames=480)  # trim -6 dB
    commands = {0: (1.0, 100.0), 9600: (0.25, 12.5), 14_400: (0.75, 50.0), 15_600: (0.0, 1000 / 48)}
    commands[19_200] = (0.5, 0.0)
    commands[24_000] = (1.0, 0.0)
    boundaries = sorted(commands) + [n]
    out = np.zeros(n + LATENCY, np.float32)
    tap = np.zeros(n + LATENCY, np.float32)
    ramp, gains = LinearRamp(), np.zeros(n)
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        shim.set_session_gain(*commands[start])
        ramp.set(*commands[start])
        gains[start:end] = [ramp.next() for _ in range(end - start)]
        shim.render_offline(out[start:end], tap[start:end])
    shim.render_offline(out[n:], tap[n:])
    trim = 10.0 ** (-6.0 / 20.0)
    expected = (tap[:n].astype(np.float64) * (trim * gains)).astype(np.float32)
    assert np.all(out[:LATENCY] == 0.0)
    assert np.array_equal(out[LATENCY:], expected)
    assert gains[9600 + 600 - 1] == 0.25 and gains[19_199] == 0.0  # landed, exactly
    assert shim.stats().limiter_active_frames == 0


def onsets(out):
    """The first non-zero sample of each run of non-zero samples."""
    nonzero = np.flatnonzero(out)
    return [int(nonzero[0])] + [int(v) for v in nonzero[1:][np.diff(nonzero) > 1]]


def test_beats_leave_the_limiter_on_their_anchored_frame():
    """F = anchor frame + round((t_play - anchor ms) x 48). The voice is exactly 0 on its first
    frame (sin 0), so its first non-zero sample is F + 1: within one sample."""
    shim = open_shim(Signal())
    shim.set_heartbeat_level(-12.0, 0.0)
    shim.set_clock_anchor(1000.0, 48_000)
    beats = {1500.0: 72_000, 2000.0: 96_000, 2750.25: 132_012, 3500.0: 168_000}
    for t_play in beats:
        shim.push_beat(t_play, 800.0)
    out, _ = render(shim, 200_000)
    starts = onsets(out)
    assert len(starts) == len(beats)
    assert all(0 <= start - frame <= 1 for start, frame in zip(starts, beats.values(), strict=True))
    stats = shim.stats()
    assert (stats.beats_played, stats.beats_dropped_late, stats.beats_dropped_full) == (4, 0, 0)


def test_a_beat_whose_onset_has_passed_is_dropped_and_counted():
    shim = open_shim(Signal())
    shim.set_heartbeat_level(-12.0, 0.0)
    shim.push_beat(5000.0, 800.0)  # no anchor yet: nowhere to put it
    shim.set_clock_anchor(0.0, 0)
    render(shim, 48_000)
    assert shim.stats().beats_dropped_late == 1
    # The next block starts at input frame 48000. A voice starting there leaves on 48000 + latency.
    on_time = (48_000 + LATENCY) / 48
    shim.push_beat(on_time - 1 / 48, 800.0)  # its onset frame is 47999: passed
    shim.push_beat(on_time, 800.0)
    out, _ = render(shim, 48_000)
    stats = shim.stats()
    assert (stats.beats_dropped_late, stats.beats_played) == (2, 1)
    assert onsets(out)[0] - LATENCY in (0, 1)


def test_beats_beyond_the_slots_are_dropped_and_counted():
    shim = open_shim(Signal(), beat_capacity=2)
    shim.set_clock_anchor(0.0, 0)
    for t_play in (10_000.0, 11_000.0, 11_500.0):
        shim.push_beat(t_play, 800.0)
    render(shim, 480)
    stats = shim.stats()
    assert (stats.beats_dropped_full, stats.beats_dropped_late) == (1, 0)


def fixture_messages(message_type):
    records = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]
    return [record for record in records if record.get("msg", {}).get("type") == message_type]


def heartbeat_support_frames(rr_ms):
    attack = round(8.0 * 48)
    decay = math.floor(min(220.0, 0.55 * rr_ms) * 48.0 + 0.5)
    filter_tail = round(20.0 * 48)
    return attack + decay + filter_tail


def test_recorded_fixture_beats_land_on_their_samples_and_never_overlap():
    """Replay the fixture's actual delivery times. Every accepted beat reaches its fixed-anchor
    sample; its envelope and filter tail end before the next one, with real digital zero between."""
    records = fixture_messages("beat")
    shim = open_shim(Signal())
    shim.set_heartbeat_level(-12.0, 0.0)
    shim.set_clock_anchor(0.0, 0)
    end_frame = round((records[-1]["msg"]["t_play"] + 300.0) * 48)
    out = np.zeros(end_frame, np.float32)
    done = 0
    for record in records:
        arrival = round(record["t_engine"] * 48)
        shim.render_offline(out[done:arrival])
        done = arrival
        beat = record["msg"]
        shim.push_beat(beat["t_play"], beat["rr_ms"])
    shim.render_offline(out[done:])

    starts = onsets(out)
    scheduled = [round(record["msg"]["t_play"] * 48) for record in records]
    assert starts == [frame + 1 for frame in scheduled]
    for record, frame, next_frame in zip(records, scheduled, scheduled[1:], strict=False):
        end = frame + heartbeat_support_frames(record["msg"]["rr_ms"])
        assert end < next_frame
        assert np.all(out[end:next_frame] == 0.0)
    stats = shim.stats()
    assert (stats.beats_played, stats.beats_dropped_late, stats.beats_dropped_full) == (
        len(records),
        0,
        0,
    )
    assert stats.heartbeat_onset_measurements == 0


def test_heartbeat_voice_energy_and_measured_spectral_leakage():
    """Measure complete shortest, intermediate and capped-decay voices—not a theoretical filter.
    The >62 Hz bound directly protects the range handed to the music."""
    rrs = (250.0, 300.0, 400.0)
    expected_points = np.array(
        [
            [-55.19, -105.49, -149.48],
            [-56.72, -112.54, -147.19],
            [-59.43, -115.41, -160.05],
        ]
    )
    measured_points = []
    fractions_inside = []
    above_62 = []
    above_120 = []
    for rr_ms in rrs:
        onset = RATE
        support = heartbeat_support_frames(rr_ms)
        shim = open_shim(Signal())
        shim.set_heartbeat_level(-12.0, 0.0)
        shim.set_clock_anchor(0.0, 0)
        shim.push_beat(1000.0, rr_ms)
        out, _ = render(shim, 2 * RATE, 480)
        voice = out[onset : onset + support].astype(np.float64)
        n = np.arange(len(voice))

        components = [
            abs(np.sum(voice * np.exp(-2j * math.pi * hz * n / RATE)))
            for hz in (44, 120, 250, 1000)
        ]
        fundamental = components[0]
        measured_points.append(
            [meters.db(component / fundamental) for component in components[1:]]
        )
        padded = np.pad(voice, (0, (1 << 19) - len(voice)))
        power = np.abs(np.fft.rfft(padded)) ** 2
        hz = np.fft.rfftfreq(len(padded), 1.0 / RATE)
        inside = power[(hz >= 36.0) & (hz <= 62.0)].sum()
        fractions_inside.append(inside / power.sum())
        above_62.append(meters.power_db(power[hz > 62.0].sum() / inside))
        above_120.append(meters.power_db(power[hz >= 120.0].sum() / inside))
        assert shim.stats().limiter_active_frames == 0

    measured_points = np.asarray(measured_points)
    print(
        f"heartbeat components at RR {rrs}, 120/250/1000 Hz relative to 44 Hz:\n"
        f"{measured_points}"
    )
    print(
        f"heartbeat 36-62 Hz fractions: {np.asarray(fractions_inside) * 100}; "
        f">62 Hz: {above_62} dB; >=120 Hz: {above_120} dB"
    )
    np.testing.assert_allclose(measured_points, expected_points, atol=2.0, rtol=0.0)
    assert min(fractions_inside) >= 0.978
    assert max(above_62) <= -21.5
    assert max(above_120) <= -55.0

    check = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "design_heartbeat.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr


def test_recorded_fixture_final_fade_is_equal_power_and_ends_at_digital_silence():
    """Use the recorded resolve boundary and beats. Compare the shim with its level held at -11
    dBFS: the only difference is the controller's exact three-second cosine fade."""
    resolve = next(
        record["msg"]
        for record in fixture_messages("state")
        if record["msg"]["segment"] == "resolve"
    )
    resolve_end = (
        resolve["t_engine"] - resolve["segment_elapsed_ms"] + resolve["segment_nominal_ms"]
    )
    fade_start = resolve_end - HEARTBEAT_FINAL_FADE_MS
    window_start = fade_start - 5_000.0
    window_end = resolve_end + 1_000.0
    beats = [
        record["msg"]
        for record in fixture_messages("beat")
        if window_start + 500.0 <= record["msg"]["t_play"] <= window_end
    ]

    class Events:
        def event(self, *args, **kwargs):
            pass

    actual = open_shim(Signal())
    reference = open_shim(Signal())
    for shim in (actual, reference):
        shim.set_clock_anchor(window_start, 0)
        for beat in beats:
            shim.push_beat(beat["t_play"], beat["rr_ms"])
    controller = HeartbeatLevel(actual, Events())
    assert controller.on_state(resolve) == ((-11.0, 2_000.0),)
    reference.set_heartbeat_level(-11.0, 2_000.0)

    assert pytest.approx(LATENCY / 48.0) == HEARTBEAT_CHAIN_LATENCY_MS
    command_at = fade_start - HEARTBEAT_CHAIN_LATENCY_MS
    before = round((command_at - window_start) * 48)
    after = round((window_end - command_at) * 48)
    actual_before, _ = render(actual, before, 480)
    reference_before, _ = render(reference, before, 480)
    assert controller.tick(command_at) == (-math.inf, HEARTBEAT_FINAL_FADE_MS)
    actual_after, _ = render(actual, after, 480)
    reference_after, _ = render(reference, after, 480)
    faded = np.concatenate([actual_before, actual_after])
    held = np.concatenate([reference_before, reference_after])

    effect_start = round((fade_start - window_start) * 48)
    fade_frames = round(HEARTBEAT_FINAL_FADE_MS * 48)
    indices = np.arange(effect_start, effect_start + fade_frames)
    audible = np.abs(held[indices]) > 1e-7
    progress = (np.arange(fade_frames) + 1.0) / fade_frames
    expected = np.cos(0.5 * math.pi * progress)
    np.testing.assert_allclose(
        faded[indices[audible]] / held[indices[audible]],
        expected[audible],
        atol=2e-5,
        rtol=2e-5,
    )
    assert np.array_equal(faded[:effect_start], held[:effect_start])
    assert np.all(faded[effect_start + fade_frames - 1 :] == 0.0)
    assert np.any(held[effect_start + fade_frames :] != 0.0)


@pytest.mark.parametrize(
    ("segment", "now", "elapsed", "nominal"),
    [
        ("baseline", 6_000.0, 6_000.0, 56_000.0),
        ("regulate", 40_000.0, 30_000.0, 75_000.0),
        ("resolve", 143_000.0, 43_000.0, 45_000.0),
    ],
)
def test_restart_staging_reaches_the_real_shim_before_its_continuation(
    segment, now, elapsed, nominal
):
    """A resumed target gets 100 ms of rendered samples before the staged continuation arrives.
    This catches the same-callback overwrite that made a final-resolve restart stay silent."""

    class Events:
        def event(self, *args, **kwargs):
            pass

    shim = open_shim(Signal())
    shim.set_clock_anchor(now, 0)
    controller = HeartbeatLevel(shim, Events())
    controller.resume()
    command = controller.on_state(
        {
            "type": "state",
            "t_engine": now,
            "segment": segment,
            "segment_elapsed_ms": elapsed,
            "segment_nominal_ms": nominal,
        }
    )
    assert command is not None and command[0][1] == HEARTBEAT_RESTART_RAMP_MS
    # A voice whose output onset is latency ms from the anchor begins at mix-input frame zero.
    beat_count = round((nominal - elapsed) / 250.0) + 1 if segment == "resolve" else 1
    for offset in range(beat_count):
        t_play = now + HEARTBEAT_CHAIN_LATENCY_MS + 250.0 * offset
        if t_play <= now + nominal - elapsed:
            shim.push_beat(t_play, 800.0 if offset == 0 else 250.0)
    first, _ = render(shim, round(HEARTBEAT_RESTART_RAMP_MS * 48), 480)
    continuation = controller.tick(now + HEARTBEAT_RESTART_RAMP_MS)
    assert continuation is not None
    remaining_ms = nominal - elapsed + 250.0 if segment == "resolve" else 500.0
    rest, _ = render(shim, round(remaining_ms * 48), 480)
    output = np.concatenate([first, rest])
    assert np.any(output[LATENCY : LATENCY + round(HEARTBEAT_RESTART_RAMP_MS * 48)] != 0.0)
    if segment == "resolve":
        end = round((nominal - elapsed) * 48)
        assert np.all(output[end:] == 0.0)


def test_a_full_command_queue_refuses_and_queues_nothing():
    x = 0.5 * np.sin(2 * math.pi * 1000 * np.arange(4 * RATE) / RATE)
    shim = open_shim(Signal(x), command_capacity=4, engine_trim_db=0.0)
    for _ in range(4):
        shim.set_session_gain(0.5, 0.0)
    with pytest.raises(ShimError) as refused:
        shim.set_session_gain(1.0, 0.0)
    assert refused.value.name == "PLS_ERROR_QUEUE_FULL"
    out, tap = render(shim, 2 * RATE)
    assert np.array_equal(
        out[LATENCY:], (tap[:-LATENCY].astype(np.float64) * 0.5).astype(np.float32)
    )
    shim.set_session_gain(1.0, 0.0)  # drained: room again


# --- the high-pass -------------------------------------------------------------------------------


def highpass_sections():
    text = (NATIVE / "src" / "highpass_coefficients.h").read_text(encoding="utf-8")
    rows = re.findall(r"\{([-0-9.e,\s]+)\},\s*//", text)
    return [[float(v) for v in row.split(",")] for row in rows]


def highpass_gain(hz):
    z1 = np.exp(-2j * np.pi * np.asarray(hz, dtype=np.float64) / RATE)
    gain = np.ones_like(z1)
    for b0, b1, b2, a1, a2 in highpass_sections():
        gain *= (b0 + b1 * z1 + b2 * z1**2) / (1.0 + a1 * z1 + a2 * z1**2)
    return np.abs(gain)


def test_the_committed_high_pass_meets_its_design():
    """At least 30 dB down at and below 62 Hz, within 1 dB from 69.35 Hz (the design's 69.3 Hz
    needs order 10.03, native/README.md), never above unity. And the header is what the script
    writes."""
    assert len(highpass_sections()) == 5
    stop = 20 * np.log10(highpass_gain(np.arange(1, 6201) / 100))
    passband = 20 * np.log10(highpass_gain(np.arange(69.35, 20_000, 0.05)))
    assert stop.max() <= -30.0
    assert passband.min() >= -1.0
    assert highpass_gain(np.arange(1, 24_001)).max() <= 1.0 + 1e-9
    check = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "design_highpass.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr


def through_highpass(x, seconds_tail=0.0):
    """(input, tap) with x through the chain at trim 0 dB; the tap is aligned with the input."""
    x = np.concatenate([x, np.zeros(round(seconds_tail * RATE))])
    shim = open_shim(Signal(x), engine_trim_db=0.0)
    _, tap = render(shim, len(x))
    return x, tap


def test_white_noise_through_the_high_pass():
    rng = np.random.default_rng(62)
    x, tap = through_highpass(0.1 * rng.standard_normal(20 * RATE))
    x, tap = x[RATE:], tap[RATE:]  # past the filter's start
    band = meters.power_db(meters.band_energy(tap, 36, 62) / meters.band_energy(x, 36, 62))
    above = meters.power_db(
        meters.band_energy(tap, 73.4, 20_000) / meters.band_energy(x, 73.4, 20_000)
    )
    print(f"white noise: 36-62 Hz {band:.2f} dB, 73.4 Hz-20 kHz {above:+.4f} dB")
    assert band <= -24.0
    assert abs(above) <= 1.0


def test_a_36_to_62_hz_sweep_through_the_high_pass():
    seconds = 30.0
    t = np.arange(round(seconds * RATE)) / RATE
    ratio = 62.0 / 36.0
    phase = 2 * math.pi * 36.0 * seconds / math.log(ratio) * (ratio ** (t / seconds) - 1.0)
    fade = np.minimum(1.0, np.minimum(t, seconds - t) / 0.5)
    x, tap = through_highpass(0.5 * np.sin(phase) * np.sin(0.5 * math.pi * fade) ** 2, 3.0)
    band = meters.power_db(meters.band_energy(tap, 36, 62) / meters.band_energy(x, 36, 62))
    print(f"36-62 Hz sweep: {band:.2f} dB")
    assert band <= -24.0


@pytest.mark.parametrize("hz", [73.4, 100.0, 146.8, 440.0, 1000.0, 5000.0, 15_000.0, 20_000.0])
def test_tones_from_73_4_hz_up_pass_within_1_db(hz):
    x, tap = through_highpass(0.5 * np.sin(2 * math.pi * hz * np.arange(4 * RATE) / RATE))
    gain = meters.db(meters.tone_amplitude(tap[2 * RATE :], hz) / 0.5)
    print(f"{hz} Hz: {gain:+.4f} dB")
    assert -1.0 <= gain <= 1e-4


@pytest.mark.parametrize("hz", [36.0, 40.0, 44.0, 45.0, 50.0, 55.0, 62.0])
def test_tones_in_36_to_62_hz_are_30_db_down(hz):
    x, tap = through_highpass(0.5 * np.sin(2 * math.pi * hz * np.arange(4 * RATE) / RATE))
    gain = meters.db(meters.tone_amplitude(tap[2 * RATE :], hz) / 0.5)
    print(f"{hz} Hz: {gain:.3f} dB")
    assert gain <= -30.0


# --- the limiter ---------------------------------------------------------------------------------


def adversarial(name, seconds=11.0):
    """Material to break a true-peak limiter. The last second is context, never measured."""
    n = round(seconds * RATE)
    k = np.arange(n)
    rng = np.random.default_rng(sum(map(ord, name)))
    if name in ("white noise +6 dBFS", "white noise +6 dBFS, heartbeat 0 dBFS"):
        return rng.uniform(-2.0, 2.0, n)
    if name == "gaussian noise, rms +6 dBFS":
        return 2.0 * rng.standard_normal(n)
    if name == "square 1 kHz full scale":
        return np.where(k % 48 < 24, 1.0, -1.0)  # period 48: not 2 mod 4 (native/README.md)
    if name == "square 997 Hz full scale":
        return np.where(np.sin(2 * math.pi * 997 * k / RATE + 0.1) >= 0, 1.0, -1.0)
    if name == "sine fs/4 at 45 degrees, samples full scale":
        return math.sqrt(2.0) * np.sin(0.5 * math.pi * k + 0.25 * math.pi)
    if name == "impulses and doublets +12 dBFS":
        x = np.zeros(n)
        x[1000::4801] = 4.0
        x[2000::4801], x[2001::4801] = 4.0, 4.0
        x[3000::4801], x[3001::4801] = 4.0, -4.0
        return x
    if name == "noise bursts +12 dBFS after silence":
        x = rng.uniform(-4.0, 4.0, n)
        return np.where((k >= RATE) & (k % 24_000 < 2400), x, 0.0)
    if name == "1 kHz bursts +10 dBFS from a crest":
        bursts = (k >= RATE) & (k % 33_600 < 4800)
        return np.where(bursts, 3.16 * np.cos(2 * math.pi * 1000 * (k % 33_600) / RATE), 0.0)
    if name == "chirp 20 Hz to 20 kHz +6 dBFS":
        # Exponential over the measured span, then held at 20 kHz through the context second.
        t, span = k / RATE, seconds - 1.0
        rate = math.log(1000.0) / span
        rising = 20.0 * (np.exp(rate * np.minimum(t, span)) - 1.0) / rate
        return 2.0 * np.sin(2 * math.pi * (rising + 20_000.0 * np.maximum(0.0, t - span)))
    raise ValueError(name)


ADVERSARIAL = [
    "white noise +6 dBFS",
    "white noise +6 dBFS, heartbeat 0 dBFS",
    "gaussian noise, rms +6 dBFS",
    "square 1 kHz full scale",
    "square 997 Hz full scale",
    "sine fs/4 at 45 degrees, samples full scale",
    "impulses and doublets +12 dBFS",
    "noise bursts +12 dBFS after silence",
    "1 kHz bursts +10 dBFS from a crest",
    "chirp 20 Hz to 20 kHz +6 dBFS",
]


@pytest.mark.parametrize("name", ADVERSARIAL)
def test_adversarial_material_never_exceeds_the_ceiling(name):
    x = adversarial(name)
    shim = open_shim(Signal(x), engine_trim_db=0.0)
    shim.set_session_gain(1.0, 0.0)
    if "heartbeat" in name:
        shim.set_heartbeat_level(0.0, 0.0)
        shim.set_clock_anchor(0.0, 0)
        for t_play in np.arange(400.0, len(x) / 48 - 400.0, 400.0):
            shim.push_beat(float(t_play), 400.0)
    out, tap = render(shim, len(x))
    measured = len(x) - RATE
    into = meters.true_peak_dbtp(tap, 0, measured)
    peak = meters.true_peak_dbtp(out, 0, measured)
    stats = shim.stats()
    print(
        f"{name}: in {into:+.3f} dBTP, out {peak:+.3f} dBTP, min gain {stats.limiter_min_gain:.3f}"
    )
    assert into > CEILING_DBTP  # the limiter had something to do
    assert peak <= CEILING_DBTP


def test_below_the_ceiling_the_chain_is_a_pure_delay():
    rng = np.random.default_rng(7)
    k = np.arange(10 * RATE)
    x = (
        rng.uniform(-0.1, 0.1, len(k))
        + 0.3 * np.sin(2 * math.pi * 1000 * k / RATE)
        + 0.2 * np.sin(2 * math.pi * 7000 * k / RATE + 1.0)
    )
    shim = open_shim(Signal(x), engine_trim_db=0.0)
    shim.set_session_gain(0.5, 0.0)
    out, tap = render(shim, len(x))
    expected = (tap[:-LATENCY].astype(np.float64) * 0.5).astype(np.float32)
    difference = float(np.max(np.abs(out[LATENCY:] - expected)))
    print(
        f"pure delay: largest difference {difference:g}, peak {meters.true_peak_dbtp(out):.2f} dBTP"
    )
    assert difference <= 1e-6
    assert np.all(out[:LATENCY] == 0.0)
    stats = shim.stats()
    assert (stats.limiter_active_frames, stats.limiter_min_gain) == (0, 1.0)


def test_the_reference_meter_reads_known_true_peaks():
    """Held against signals whose true peak is known, and against a direct 256x reconstruction."""
    k = np.arange(4 * RATE)
    middle = (meters.CONTEXT, len(k) - meters.CONTEXT)  # clear of the onset's and the end's ringing
    quarter = np.sin(0.5 * math.pi * k + 0.25 * math.pi)  # samples at +-0.707, peak 1
    assert abs(meters.true_peak_dbtp(quarter, *middle)) < 0.001
    high = 0.5 * np.sin(2 * math.pi * 19_937 * k / RATE + 0.3)
    assert abs(meters.true_peak_dbtp(high, *middle) - meters.db(0.5)) < 0.001
    rng = np.random.default_rng(3)
    for _ in range(5):
        x = rng.standard_normal(4096)
        padded = np.concatenate([np.zeros(4096), x, np.zeros(4096)])
        direct = float(np.max(np.abs(meters.oversample(padded, 256))))
        assert abs(meters.true_peak_dbtp(x) - meters.db(direct)) < 0.01


# --- the audio thread ----------------------------------------------------------------------------


def test_the_allocation_tripwire_passes():
    """native/tests/rt_tripwire_test.cpp, built by ctest (native/README.md). Its first line is
    `source <pls_source_hash()>`: an executable built from other sources than the checkout fails
    here rather than passing for sources it never ran."""
    if not TRIPWIRE.exists():
        pytest.skip(f"{TRIPWIRE.relative_to(ROOT)} is not built: cmake --build native/build")
    run = subprocess.run([str(TRIPWIRE)], capture_output=True, text=True, timeout=600)
    lines = run.stdout.splitlines()
    source = lines[0].removeprefix("source ") if lines and lines[0].startswith("source ") else None
    assert source == checkout_source_hash(), (
        f"{TRIPWIRE.relative_to(ROOT).as_posix()} was built from other sources: "
        f"cmake --build native/build (it printed {lines[0] if lines else 'nothing'!r})"
    )
    assert run.returncode == 0, run.stdout + run.stderr


# What the audio thread may never do: allocate, lock or wait, log or do I/O, throw. The lock, wait
# and sleep families match by prefix or suffix (shared_mutex, AcquireSRWLockShared, SleepEx,
# WaitForSingleObjectEx), not as whole words.
FORBIDDEN_ON_THE_AUDIO_THREAD = re.compile(
    r"\b(new|delete|malloc|calloc|realloc|free|_aligned_malloc|aligned_alloc|alloca"
    r"|make_unique|make_shared|shared_ptr|unique_ptr|push_back|emplace_back|resize|reserve"
    r"|insert|std::vector|std::string|std::map|std::deque|std::list|std::function"
    r"|stable_sort|inplace_merge|get_temporary_buffer"
    r"|HeapAlloc|VirtualAlloc|LocalAlloc|GlobalAlloc|CoTaskMemAlloc"
    r"|mutex|\w*mutex\w*|lock_guard|unique_lock|scoped_lock|shared_lock|condition_variable"
    r"|\w*[Ss]emaphore\w*|CRITICAL_SECTION|\w*CriticalSection\w*|\w*SRWLock\w*"
    r"|Wait\w*|wait|wait_for|wait_until|notify_\w+|Sleep\w*|\w*sleep\w*|SwitchToThread|yield"
    r"|std::thread|join|pthread_\w+"
    r"|printf|fprintf|sprintf|snprintf|puts|fputs|cout|cerr|clog|OutputDebugString\w*"
    r"|WriteConsole\w*|fopen|fwrite|fread|fclose|fflush|ofstream|ifstream|fstream|CreateFile\w*"
    r"|WriteFile|throw|try|catch)\b"
    r"|#\s*include\s*<(mutex|shared_mutex|thread|condition_variable|semaphore|latch|barrier"
    r"|stop_token|iostream|fstream|sstream|ostream|syncstream|vector|string|memory|functional|map"
    r"|deque|list|cstdio|stdio\.h|future)>"
)
AUDIO_THREAD_FILES = [
    "process.h",
    "process.cpp",
    "heartbeat.h",
    "heartbeat.cpp",
    "heartbeat_filter_coefficients.h",
    "limiter.h",
    "limiter.cpp",
    "highpass.h",
    "highpass_coefficients.h",
    "spsc.h",
]


def function_body(code, signature):
    start = code.index(signature)
    open_brace = code.index("{", start)
    depth = 0
    for i in range(open_brace, len(code)):
        depth += {"{": 1, "}": -1}.get(code[i], 0)
        if depth == 0:
            return code[start : i + 1]
    raise AssertionError(f"unbalanced braces after {signature}")


def test_the_audio_thread_sources_name_none_of_the_usual_calls_that_allocate_lock_log_or_do_io():
    """What the tripwire cannot see: a lock, a wait, a sleep, I/O, or a call into a system DLL
    that allocates. Every file the audio thread runs, and the functions in shim.cpp that run on
    miniaudio's device thread (the data callback, and the notification callback with the counting
    it calls), comments and literals removed. shim.cpp's other functions run on the control thread
    and allocate at open.

    This is a denylist of spellings. It catches the usual ones, and a call it has no pattern for
    passes unseen, so a new call on the audio thread still wants reading against hard rule 4."""
    sources = {
        name: c_code((NATIVE / "src" / name).read_text(encoding="utf-8"))
        for name in AUDIO_THREAD_FILES
    }
    shim = c_code((NATIVE / "src" / "shim.cpp").read_text(encoding="utf-8"))
    callback_functions = {
        "on_device_data": "void on_device_data(",
        "read_clock_mailbox": "pls::DeviceClockSample read_clock_mailbox(",
        "qpc_ns": "int64_t qpc_ns(",
        "on_device_notification": "void on_device_notification(",
        "count_unrequested_stop": "void count_unrequested_stop(",
    }
    for name, signature in callback_functions.items():
        sources[f"shim.cpp: {name}"] = function_body(shim, signature)
    found = {
        name: sorted({m.group(0) for m in FORBIDDEN_ON_THE_AUDIO_THREAD.finditer(code)})
        for name, code in sources.items()
    }
    assert {name: hits for name, hits in found.items() if hits} == {}
    # Every source file is either scanned here or is shim.cpp, so a new file cannot slip past.
    listed = set(AUDIO_THREAD_FILES) | {"shim.cpp"}
    assert {path.name for path in (NATIVE / "src").iterdir()} == listed
