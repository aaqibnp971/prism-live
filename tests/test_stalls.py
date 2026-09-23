"""The stall diagnostic's clock injection, artifact format and argument checks."""

import asyncio
import struct
from types import SimpleNamespace

import numpy as np
import pytest

from tools import check_stalls


class RecordingShim:
    def __init__(self):
        self.frames = 0
        self.anchor = None

    def set_clock_anchor(self, milliseconds, frame):
        self.anchor = (milliseconds, frame)

    def render_offline(self, out):
        out[:] = 0.125
        self.frames += len(out)


def test_stall_advances_every_audio_frame_without_running_ready_bridge_callbacks(monkeypatch):
    monkeypatch.setattr(check_stalls, "MAX_SECONDS", 1)
    shim = RecordingShim()
    audio = check_stalls.AudioTimeline(SimpleNamespace(shim=shim))
    loop = check_stalls.AudioClockLoop(audio)
    seen = []

    async def receiver():
        await asyncio.sleep(0.010)
        seen.append(("receiver", audio.milliseconds()))

    async def scenario():
        task = asyncio.create_task(receiver())
        await asyncio.sleep(0.005)
        before = shim.frames
        audio.advance(audio.seconds() + 0.020)
        assert shim.frames - before == 960
        assert seen == []
        seen.append(("stall_end", audio.milliseconds()))
        await task

    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()
    assert shim.anchor == (0, 0)
    assert seen == [("stall_end", 25.0), ("receiver", 25.0)]
    assert np.all(audio.samples[: shim.frames] == 0.125)


def test_timeline_bound_is_explicit_and_never_moves_backwards(monkeypatch):
    monkeypatch.setattr(check_stalls, "MAX_SECONDS", 1)
    shim = RecordingShim()
    audio = check_stalls.AudioTimeline(SimpleNamespace(shim=shim))
    audio.advance(0.1)
    audio.advance(0.05)
    assert audio.frame == shim.frames == 4800
    with pytest.raises(RuntimeError, match="bounded audio timeline"):
        audio.advance(1.01)


def test_subsample_timer_rounding_never_introduces_a_real_selector_wait(monkeypatch):
    monkeypatch.setattr(check_stalls, "MAX_SECONDS", 1)
    audio = check_stalls.AudioTimeline(SimpleNamespace(shim=RecordingShim()))
    loop = check_stalls.AudioClockLoop(audio)
    waits = []
    original = loop._selector.select

    def select(timeout=None):
        waits.append(timeout)
        return original(0)

    loop._selector.select = select

    async def scenario():
        await asyncio.sleep(float(np.nextafter(0.005, np.inf)))

    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()
    assert waits and all(timeout == 0 for timeout in waits)
    assert audio.frame == 241


def test_wav_preserves_original_float32_samples_without_gain_changes(tmp_path):
    samples = np.array([0.0, -0.25, 0.125, 0.0], dtype=np.float32)
    path = tmp_path / "stall.wav"
    check_stalls.write_float_wav(path, samples)
    contents = path.read_bytes()
    assert contents[:4] == b"RIFF" and contents[8:16] == b"WAVEfmt "
    assert struct.unpack("<I", contents[4:8])[0] == len(contents) - 8
    assert struct.unpack("<HHIIHH", contents[20:36]) == (3, 1, 48000, 192000, 4, 32)
    data_at = contents.index(b"data") + 8
    np.testing.assert_array_equal(np.frombuffer(contents[data_at:], dtype="<f4"), samples)


@pytest.mark.parametrize("segment, seconds", [("idle", 2), ("baseline", 3), ("reset", 10)])
def test_case_rejects_unknown_matrix_entries_before_opening_audio(tmp_path, segment, seconds):
    with pytest.raises(ValueError, match="experience segment"):
        check_stalls.run_case(tmp_path, segment, seconds)
