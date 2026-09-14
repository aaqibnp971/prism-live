"""The Prism Engine and the audio shim, through ctypes, and their lifecycle (prompt 2.5).

prism-live owns the audio device (CLAUDE.md hard rule 8). Two committed libraries:

- vendor/lib/libprism_core.dll, the engine at acbfd50 (vendor/lib/README.md). Only its control
  calls are bound for use: create, load the scene, the mood override, the sample rate, destroy.
  prism_start is never called, so the engine's inference thread never runs and the bridge is its
  only PSV writer (docs/engine-findings.md, "How the PSV gets in"). prism_device_start and
  prism_device_stop are never called either: the shim owns the stream. prism_render is bound
  only for its address, which the shim calls from its own audio thread; nothing in Python calls it.
- native/bin/libprism_live_shim.dll, the shim (native/include/prism_live_shim.h): pulls the engine,
  high-passes it at 62 Hz, applies the trim and the session gain, adds the heartbeat layer and
  true-peak limits to -1.0 dBTP.

Engine. One handle, one scene for its life, at 48 kHz or refused: a scene at any other rate is
destroyed with the handle and raises. set_mood_override takes arousal, cognitive_load and
readiness already blended with authority (bridge/engine_feed.py) and sends them with valence 0.5,
confidence 1.0 and mode_hint NULL, so the engine reads each value as sent.

Shim. A thin wrapper. Every call that fails raises ShimError naming the pls_result.

EngineHost. The lifecycle:

- open(scene): engine, scene, then the shim at once, on prism_render and the handle, so
  frames_rendered counts from the load and is the engine's phase (prompt 2.7).
- start(gain): T_engine's origin (bridge/clock.py) to the shim, which anchors beats to it on its
  first callback, then the stream, then the SessionGain resumed so the next state message sends
  its target again.
- await stop(gain): session gain and heartbeat level to silence over 3 s, a wait until the end of
  that ramp has left the limiter, then the stream. The SessionGain sends the fade and holds
  everything else back until start resumes it. For shutting the bridge down only. Between
  visitors the stream keeps running with the session gain at 0, so the engine's phase keeps
  advancing.
- close(): the shim first, which stops the stream, so nothing is inside prism_render when the
  engine is destroyed (the engine header's quiescence before destroy).

The session gain has one writer. Once a SessionGain exists, nothing else calls set_session_gain:
not the bridge, not the console, not EngineHost, which goes through the SessionGain it is given.

Threads. One control thread makes every call here: the bridge's loop. The shim's command queue has
a single producer and the engine's control calls are not safe against each other. Never call
anything here from an audio callback: the override takes a lock and can allocate.

    host = EngineHost()
    host.open(Path("assets/scenes.json"))
    feed, gain = PsvFeed(host.engine, log), SessionGain(host.shim, log)
    host.start(gain)
    ...
    await host.stop(gain)
    host.close()
"""

from __future__ import annotations

import asyncio
import ctypes
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bridge import clock
from bridge.engine_mapping import CONFIDENCE_SENT, VALENCE_SENT

if TYPE_CHECKING:
    from bridge.engine_feed import SessionGain

ROOT = Path(__file__).resolve().parent.parent
ENGINE_DLL = ROOT / "vendor" / "lib" / "libprism_core.dll"
SHIM_DLL = ROOT / "native" / "bin" / "libprism_live_shim.dll"

ENGINE_VERSION = "0.3.0"  # prism_version() at acbfd50
SHIM_ABI_VERSION = 2  # PLS_ABI_VERSION
SAMPLE_RATE = 48_000  # the project's rate; the engine takes whatever the stems are

# prism_config (docs/engine-findings.md, prompt 0.4 item 4). They only steer the inference thread,
# which never runs, and are set anyway so a started engine could not surprise anyone.
CADENCE_MS = 2_000
CHECK_INTERVAL_MS = 50
SIGNIFICANT_DELTA = 0.05

STOP_RAMP_MS = 3_000.0  # session gain and heartbeat level to silence before the stream stops
STOP_POLL_S = 0.01  # after the ramp, how often stop looks at frames_rendered
STOP_DEADLINE_MS = 500.0  # past the ramp: a stream that is not advancing is stopped anyway

PRISM_RESULTS = {
    0: "PRISM_OK",
    1: "PRISM_ERROR_INVALID_ARGUMENT",
    2: "PRISM_ERROR_INVALID_STATE",
    3: "PRISM_ERROR_IO",
    4: "PRISM_ERROR_DEVICE",
    5: "PRISM_ERROR_OUT_OF_MEMORY",
    6: "PRISM_ERROR_BUSY",
}
PLS_RESULTS = {
    0: "PLS_OK",
    1: "PLS_ERROR_INVALID_ARGUMENT",
    2: "PLS_ERROR_INVALID_STATE",
    3: "PLS_ERROR_DEVICE",
    4: "PLS_ERROR_OUT_OF_MEMORY",
    5: "PLS_ERROR_QUEUE_FULL",
    6: "PLS_ERROR_SAMPLE_RATE",
}
PLS_BEAT_OK = 0
PLS_BEAT_INTERPOLATED = 1


class EngineError(RuntimeError):
    """An engine call failed, or the engine was refused. `result` is the prism_result, if any."""

    def __init__(self, message: str, result: int | None = None) -> None:
        super().__init__(message)
        self.result = result
        self.name = PRISM_RESULTS.get(result, str(result)) if result is not None else None


class ShimError(RuntimeError):
    """A shim call failed. `name` is the pls_result's name, e.g. PLS_ERROR_QUEUE_FULL."""

    def __init__(self, call: str, result: int) -> None:
        self.call = call
        self.result = result
        self.name = PLS_RESULTS.get(result, f"pls_result {result}")
        super().__init__(f"{call}: {self.name}")


# --- the C structs, field for field --------------------------------------------------------------


class PrismConfig(ctypes.Structure):
    _fields_ = [
        ("tz_offset_min", ctypes.c_int32),
        ("vertical", ctypes.c_int32),
        ("cadence_ms", ctypes.c_int64),
        ("check_interval_ms", ctypes.c_int64),
        ("significant_delta", ctypes.c_double),
    ]


class PrismMoodOverride(ctypes.Structure):
    _fields_ = [
        ("mode_hint", ctypes.c_char_p),
        ("arousal", ctypes.c_double),
        ("valence", ctypes.c_double),
        ("cognitive_load", ctypes.c_double),
        ("readiness", ctypes.c_double),
        ("confidence", ctypes.c_double),
    ]


class PlsConfig(ctypes.Structure):
    _fields_ = [
        ("sample_rate", ctypes.c_uint32),
        ("max_block_frames", ctypes.c_uint32),
        ("command_capacity", ctypes.c_uint32),
        ("beat_capacity", ctypes.c_uint32),
        ("engine_trim_db", ctypes.c_double),
    ]


class PlsStats(ctypes.Structure):
    _fields_ = [
        ("frames_rendered", ctypes.c_uint64),
        ("render_errors", ctypes.c_uint64),
        ("beats_played", ctypes.c_uint64),
        ("beats_dropped_late", ctypes.c_uint64),
        ("beats_dropped_full", ctypes.c_uint64),
        ("limiter_active_frames", ctypes.c_uint64),
        ("limiter_min_gain", ctypes.c_double),
        ("device_unrequested_stops", ctypes.c_uint64),  # ABI 2, appended
    ]


# pls_render_fn: the shape of prism_render. For a render function written in Python (tests only:
# the shim calls it on its audio thread, where Python has no business in the bridge).
RENDER_FN = ctypes.CFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_uint32
)

_c = ctypes
_FLOAT_P = ctypes.POINTER(ctypes.c_float)


def _bind(lib: ctypes.CDLL, name: str, argtypes: list, restype: Any) -> None:
    function = getattr(lib, name)
    function.argtypes = argtypes
    function.restype = restype


@cache
def engine_library() -> ctypes.CDLL:
    """The engine DLL, every function this module uses typed. Refused unless it is 0.3.0."""
    lib = ctypes.CDLL(str(ENGINE_DLL))
    _bind(lib, "prism_version", [], _c.c_char_p)
    _bind(lib, "prism_result_description", [_c.c_int], _c.c_char_p)
    _bind(lib, "prism_config_default", [], PrismConfig)
    _bind(lib, "prism_create", [_c.POINTER(PrismConfig), _c.POINTER(_c.c_void_p)], _c.c_int)
    _bind(lib, "prism_destroy", [_c.c_void_p], None)
    _bind(lib, "prism_load_scene", [_c.c_void_p, _c.c_char_p], _c.c_int)
    _bind(lib, "prism_sample_rate", [_c.c_void_p], _c.c_uint32)
    _bind(lib, "prism_set_mood_override", [_c.c_void_p, _c.POINTER(PrismMoodOverride)], _c.c_int)
    _bind(lib, "prism_clear_mood_override", [_c.c_void_p], _c.c_int)
    # Typed so its address is what the shim expects. Only the shim's audio thread calls it.
    _bind(lib, "prism_render", [_c.c_void_p, _FLOAT_P, _c.c_uint32], _c.c_int)
    version = lib.prism_version().decode()
    if version != ENGINE_VERSION:
        raise EngineError(f"{ENGINE_DLL} is engine {version}, not {ENGINE_VERSION} (acbfd50)")
    return lib


@cache
def shim_library() -> ctypes.CDLL:
    """The shim DLL, every function of its header typed. Refused unless its ABI version matches."""
    lib = ctypes.CDLL(str(SHIM_DLL))
    p = _c.c_void_p
    _bind(lib, "pls_abi_version", [], _c.c_int32)
    _bind(lib, "pls_source_hash", [], _c.c_char_p)
    _bind(lib, "pls_latency_frames", [], _c.c_uint32)
    _bind(lib, "pls_config_default", [], PlsConfig)
    # The render function goes in as an address: prism_render's, or a RENDER_FN's.
    _bind(lib, "pls_open", [_c.POINTER(PlsConfig), p, p, _c.POINTER(p)], _c.c_int32)
    _bind(lib, "pls_start", [p], _c.c_int32)
    _bind(lib, "pls_stop", [p], _c.c_int32)
    _bind(lib, "pls_close", [p], None)
    _bind(lib, "pls_set_session_gain", [p, _c.c_double, _c.c_double], _c.c_int32)
    _bind(lib, "pls_set_heartbeat_level", [p, _c.c_double, _c.c_double], _c.c_int32)
    _bind(lib, "pls_push_beat", [p, _c.c_double, _c.c_double, _c.c_int32], _c.c_int32)
    _bind(lib, "pls_set_time_origin_ns", [p, _c.c_int64], _c.c_int32)
    _bind(lib, "pls_set_clock_anchor", [p, _c.c_double, _c.c_uint64], _c.c_int32)
    _bind(lib, "pls_frames_rendered", [p], _c.c_uint64)
    _bind(lib, "pls_get_stats", [p, _c.POINTER(PlsStats)], _c.c_int32)
    _bind(lib, "pls_render_offline", [p, _FLOAT_P, _FLOAT_P, _c.c_uint32], _c.c_int32)
    abi = lib.pls_abi_version()
    if abi != SHIM_ABI_VERSION:
        raise ShimError("pls_abi_version", abi)
    return lib


def _narrow_path(path: Path | str) -> bytes:
    """A path for the engine's narrow-character file calls: the ANSI code page on Windows."""
    text = str(Path(path))
    return text.encode("mbcs") if sys.platform == "win32" else text.encode()


# --- the engine ----------------------------------------------------------------------------------


class Engine:
    """One engine handle: its scene, its mood override. Never started. See the module docstring."""

    def __init__(self) -> None:
        self._lib = engine_library()
        config = self._lib.prism_config_default()
        config.cadence_ms = CADENCE_MS
        config.check_interval_ms = CHECK_INTERVAL_MS
        config.significant_delta = SIGNIFICANT_DELTA
        handle = ctypes.c_void_p()
        self._check(
            "prism_create", self._lib.prism_create(ctypes.byref(config), ctypes.byref(handle))
        )
        self._handle: int | None = handle.value
        self.scene: Path | None = None

    @property
    def handle(self) -> int:
        if self._handle is None:
            raise EngineError("the engine has been destroyed")
        return self._handle

    @property
    def render_address(self) -> int:
        """prism_render's address, for the shim. Never call it from Python."""
        return ctypes.cast(self._lib.prism_render, ctypes.c_void_p).value

    @property
    def sample_rate(self) -> int:
        """The loaded scene's rate; 0 before a scene is loaded."""
        return self._lib.prism_sample_rate(self.handle)

    def load_scene(self, manifest: Path | str) -> None:
        """Load the manifest's default scene, once. Anything but 48 kHz destroys the engine and
        raises: no resampling anywhere."""
        if self.scene is not None:
            # The engine refuses a second scene, and this must not destroy a handle a shim holds.
            raise EngineError(f"{self.scene} is already loaded: one scene per engine")
        result = self._lib.prism_load_scene(self.handle, _narrow_path(manifest))
        if result != 0:
            self.destroy()
            raise EngineError(f"prism_load_scene({manifest}): {PRISM_RESULTS.get(result)}", result)
        rate = self._lib.prism_sample_rate(self.handle)
        if rate != SAMPLE_RATE:
            self.destroy()
            raise EngineError(f"{manifest} is a {rate} Hz scene; prism-live runs at 48 kHz only")
        self.scene = Path(manifest)

    def set_mood_override(self, arousal: float, cognitive_load: float, readiness: float) -> None:
        """The engine's PSV: these three as effective values, valence 0.5, confidence 1.0, no
        mode_hint. Published on return; the engine consumes it at its next render block."""
        override = PrismMoodOverride(
            None, arousal, VALENCE_SENT, cognitive_load, readiness, CONFIDENCE_SENT
        )
        self._check(
            "prism_set_mood_override",
            self._lib.prism_set_mood_override(self.handle, ctypes.byref(override)),
        )

    def clear_mood_override(self) -> None:
        """Release the pin. With no inference thread nothing replaces it: the last PSV stays."""
        self._check("prism_clear_mood_override", self._lib.prism_clear_mood_override(self.handle))

    def destroy(self) -> None:
        """Free the handle. Idempotent. Nothing may be inside prism_render: close the shim first."""
        if self._handle is not None:
            self._lib.prism_destroy(self._handle)
            self._handle = None

    def _check(self, call: str, result: int) -> None:
        if result != 0:
            description = self._lib.prism_result_description(result).decode()
            raise EngineError(
                f"{call}: {PRISM_RESULTS.get(result, result)} ({description})", result
            )


# --- the shim ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ShimStats:
    """pls_stats, in its field order. A snapshot of counters, not one instant."""

    frames_rendered: int  # same as Shim.frames_rendered
    # Blocks whose render call returned non-zero or wrote a non-finite sample; each was zeroed
    # before the high-pass.
    render_errors: int
    beats_played: int
    beats_dropped_late: int  # onset already passed when the audio thread saw it
    beats_dropped_full: int  # no free slot for a future beat
    limiter_active_frames: int  # frames with gain reduction applied
    limiter_min_gain: float  # lowest limiter gain since the shim opened, 1.0 = never engaged
    # Streams stopped when pls_stop did not ask: a lost endpoint. frames_rendered stops with it;
    # recover with EngineHost.stop, then EngineHost.start.
    device_unrequested_stops: int


class Shim:
    """One pls_shim. `render` is an address (Engine.render_address) or a RENDER_FN, kept alive
    here for as long as the shim can call it. See native/include/prism_live_shim.h."""

    def __init__(
        self,
        render: int | Callable,
        core: int | None = None,
        *,
        config: PlsConfig | None = None,
    ) -> None:
        self._lib = shim_library()
        self._render = render
        address = render if isinstance(render, int) else ctypes.cast(render, ctypes.c_void_p).value
        self.config = self._lib.pls_config_default() if config is None else config
        handle = ctypes.c_void_p()
        self._check(
            "pls_open",
            self._lib.pls_open(ctypes.byref(self.config), address, core, ctypes.byref(handle)),
        )
        self._handle: int | None = handle.value

    @staticmethod
    def config_default() -> PlsConfig:
        return shim_library().pls_config_default()

    @staticmethod
    def abi_version() -> int:
        return shim_library().pls_abi_version()

    @staticmethod
    def source_hash() -> str:
        return shim_library().pls_source_hash().decode()

    @staticmethod
    def latency_frames() -> int:
        return shim_library().pls_latency_frames()

    @property
    def handle(self) -> int:
        if self._handle is None:
            raise RuntimeError("the shim is closed")
        return self._handle

    def start(self) -> None:
        self._check("pls_start", self._lib.pls_start(self.handle))

    def stop(self) -> None:
        self._check("pls_stop", self._lib.pls_stop(self.handle))

    def close(self) -> None:
        """Stop and free. Idempotent. After it returns the render function is never called."""
        if self._handle is not None:
            self._lib.pls_close(self._handle)
            self._handle = None

    def set_session_gain(self, target: float, ramp_ms: float) -> None:
        self._check(
            "pls_set_session_gain", self._lib.pls_set_session_gain(self.handle, target, ramp_ms)
        )

    def set_heartbeat_level(self, target_dbfs: float, ramp_ms: float) -> None:
        self._check(
            "pls_set_heartbeat_level",
            self._lib.pls_set_heartbeat_level(self.handle, target_dbfs, ramp_ms),
        )

    def push_beat(self, t_play_ms: float, rr_ms: float, quality: int = PLS_BEAT_OK) -> None:
        self._check(
            "pls_push_beat", self._lib.pls_push_beat(self.handle, t_play_ms, rr_ms, quality)
        )

    def set_time_origin_ns(self, perf_counter_origin_ns: int) -> None:
        self._check(
            "pls_set_time_origin_ns",
            self._lib.pls_set_time_origin_ns(self.handle, perf_counter_origin_ns),
        )

    def set_clock_anchor(self, t_engine_ms: float, output_frame: int) -> None:
        self._check(
            "pls_set_clock_anchor",
            self._lib.pls_set_clock_anchor(self.handle, t_engine_ms, output_frame),
        )

    def frames_rendered(self) -> int:
        return self._lib.pls_frames_rendered(self.handle)

    def stats(self) -> ShimStats:
        stats = PlsStats()
        self._check("pls_get_stats", self._lib.pls_get_stats(self.handle, ctypes.byref(stats)))
        return ShimStats(*(getattr(stats, name) for name, _ in PlsStats._fields_))

    def render_offline(self, out: Any, tap: Any = None) -> None:
        """Run the chain for len(out) frames on this thread. `out` and `tap` are writable float32
        buffers (a numpy float32 array will do); tap, if given, gets the high-passed engine buffer.
        Refused while the stream is started."""
        out_array = _float_array(out)
        frames = len(out_array)
        tap_array = None
        if tap is not None:
            tap_array = _float_array(tap)
            if len(tap_array) < frames:
                raise ValueError("tap is shorter than out")
        self._check(
            "pls_render_offline",
            self._lib.pls_render_offline(self.handle, out_array, tap_array, frames),
        )

    @staticmethod
    def _check(call: str, result: int) -> None:
        if result != 0:
            raise ShimError(call, result)


def _float_array(buffer: Any) -> ctypes.Array:
    view = memoryview(buffer)
    if view.format != "f" or view.readonly or not view.c_contiguous:
        raise TypeError("expected a writable, contiguous float32 buffer")
    count = view.nbytes // 4
    if count >= 1 << 32:
        raise ValueError("more frames than one call can take")
    return (ctypes.c_float * count).from_buffer(view)


# --- the lifecycle -------------------------------------------------------------------------------


class EngineHost:
    """The engine and the shim that owns its output. See the module docstring."""

    def __init__(self, config: PlsConfig | None = None) -> None:
        self._config = config
        self.engine: Engine | None = None
        self.shim: Shim | None = None

    def open(self, scene: Path | str) -> None:
        """Engine, scene (48 kHz or refused), then the shim right away. Whatever raises, the
        engine handle is destroyed and the host stays closed."""
        if self.engine is not None:
            raise RuntimeError("already open")
        engine = Engine()
        try:
            engine.load_scene(scene)
            shim = Shim(engine.render_address, engine.handle, config=self._config)
        except BaseException:
            engine.destroy()  # idempotent: load_scene destroys it itself when the engine refuses
            raise
        self.engine, self.shim = engine, shim

    def start(self, gain: SessionGain | None = None) -> None:
        """Anchor T_engine, start the stream, then resume `gain`, the bridge's SessionGain, so the
        next state message sends its target again whatever it sent before a stop. Raises
        ShimError(PLS_ERROR_SAMPLE_RATE) unless the device runs at 48 kHz with no conversion, and
        then leaves `gain` holding. Once a SessionGain exists, nothing else calls
        set_session_gain. After a lost endpoint (ShimStats.device_unrequested_stops), stop and
        then start again: the shim opens the device afresh."""
        shim = self._open_shim()
        shim.set_time_origin_ns(clock._ORIGIN_NS)
        shim.start()
        if gain is not None:
            gain.resume()

    async def stop(self, gain: SessionGain | None = None) -> None:
        """Fade the session gain and the heartbeat layer to silence over 3 s, wait until the end of
        that fade has left the chain, then stop the stream.

        The ramps start when the audio thread takes the commands, up to one block after they are
        pushed, and the output lags the chain by Shim.latency_frames(). So after the ramp's 3 s,
        stop polls frames_rendered until it is past the count read before the push by the ramp,
        the latency and two blocks: the block that takes the commands, and the tail of the last
        device buffer a stop drops. The stream then ends in digital silence. A stream that is not
        advancing (never started, or its endpoint lost) is stopped anyway STOP_DEADLINE_MS after
        the ramp.

        `gain` is the bridge's SessionGain. It sends the fade and holds back every state message's
        target until start(gain) resumes it, so a reset, resolve's ending or a new session's
        baseline cannot re-target the gain during the wait. Without one the fade goes straight to
        the shim, which is only for when no SessionGain exists: once one does, nothing else calls
        set_session_gain. The heartbeat level is written here directly; prompt 2.6's heartbeat
        level controller will need the same hold."""
        shim = self._open_shim()
        began_ms = clock.t_engine_ms()
        pushed_at = shim.frames_rendered()
        if gain is None:
            shim.set_session_gain(0.0, STOP_RAMP_MS)
        else:
            gain.fade_out(STOP_RAMP_MS, t_engine=began_ms)
        shim.set_heartbeat_level(-math.inf, STOP_RAMP_MS)
        faded_out = (
            pushed_at
            + 2 * shim.config.max_block_frames
            + round(STOP_RAMP_MS * SAMPLE_RATE / 1000.0)
            + Shim.latency_frames()
        )
        await asyncio.sleep(STOP_RAMP_MS / 1000.0)
        deadline_ms = began_ms + STOP_RAMP_MS + STOP_DEADLINE_MS
        while shim.frames_rendered() < faded_out and clock.t_engine_ms() < deadline_ms:
            await asyncio.sleep(STOP_POLL_S)
        shim.stop()

    def close(self) -> None:
        """The shim, then the engine. Idempotent. No fade: stop first for one."""
        if self.shim is not None:
            self.shim.close()
            self.shim = None
        if self.engine is not None:
            self.engine.destroy()
            self.engine = None

    def _open_shim(self) -> Shim:
        if self.shim is None:
            raise RuntimeError("not open")
        return self.shim
