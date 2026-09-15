/*
 * prism_live_shim.h: the prism-live audio shim's C ABI (prompts 2.5-2.6).
 *
 * prism-live owns the audio device (CLAUDE.md hard rule 8). This library owns the output
 * stream, and its callback is the only place prism_render is ever called. Per block, in this
 * order:
 *
 *   1. render(core, engine_buf, n): prism_render, into a preallocated mono float32 buffer.
 *      48 kHz, no resampling anywhere.
 *   2. High-pass the engine buffer: a 10th-order Chebyshev type II high-pass, at least 30 dB
 *      down from 62 Hz, within 1 dB from 69.35 Hz. 36 to 62 Hz belongs to the heartbeat layer
 *      alone (hard rule 2), enforced in code rather than trusted to the stems.
 *   3. Multiply by the static engine trim and the session gain (ramped).
 *   4. Add the heartbeat layer. It has its own level and is not under the session gain.
 *   5. True-peak limit at -1.0 dBTP. The last stage.
 *   6. Write to the device, the same sample on every channel it has.
 *
 * Threading. Exactly one CONTROL thread makes every call below except pls_render_offline,
 * and never from the audio callback: the control calls are the single producer of a
 * lock-free command queue, so two control threads would corrupt it. The AUDIO thread is the
 * device callback after pls_start, or the thread calling pls_render_offline while the device
 * is stopped; never both. The render function given to pls_open is called only from the
 * audio thread. Nothing on the audio thread allocates, locks, logs, touches a file or calls COM
 * (hard rule 4). A non-audio timing thread polls IAudioClock and publishes fixed-size snapshots
 * to the callback through a lock-free mailbox.
 *
 * Time. Beats are placed from t_play, milliseconds on T_engine (bridge/clock.py): Python's
 * time.perf_counter_ns() minus the bridge's origin, over 1e6. An anchor maps T_engine to an
 * output frame, the frame index as it leaves the limiter: output frame k is the k-th sample
 * written out. A beat for output frame F starts in the mix at input frame F minus
 * pls_latency_frames(). With the device running, IAudioClock supplies a stream position and its
 * correlated QueryPerformanceCounter value. The first usable polling result establishes the map;
 * QueryPerformanceCounter plus one device period is the startup fallback. Later results move only
 * its target, and the live anchor follows at no more than 1 ms per second of stream time. Offline,
 * pls_set_clock_anchor sets an exact map. Per-beat timing records are drained and logged on the
 * control thread. The real DAC relationship still needs the hardware check in
 * docs/known-limits.md.
 *
 * Never call prism_device_start or prism_device_stop on the engine handle given here. Stop is
 * the bridge's: ramp the session gain and the heartbeat level to zero over 3 s, wait until the
 * end of the ramp has left the limiter (pls_latency_frames() more frames rendered), then
 * pls_stop.
 */

#ifndef PRISM_LIVE_SHIM_H
#define PRISM_LIVE_SHIM_H

#include <stdint.h>

#if defined(_WIN32)
#if defined(PLS_BUILD_SHARED)
#define PLS_API __declspec(dllexport)
#else
#define PLS_API
#endif
#else
#define PLS_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* 3: pls_stats gained device-clock and heartbeat-onset telemetry, appended. */
#define PLS_ABI_VERSION 3
#define PLS_SAMPLE_RATE 48000u
#define PLS_CEILING_DBTP (-1.0)

typedef enum pls_result {
  PLS_OK = 0,
  PLS_ERROR_INVALID_ARGUMENT = 1, /* null, out of range, NaN */
  PLS_ERROR_INVALID_STATE = 2,    /* call violates the lifecycle */
  PLS_ERROR_DEVICE = 3,           /* the output device could not be opened or started */
  PLS_ERROR_OUT_OF_MEMORY = 4,
  PLS_ERROR_QUEUE_FULL = 5,       /* the command queue is full; nothing was queued */
  PLS_ERROR_SAMPLE_RATE = 6       /* not 48 kHz: the config, or the device would resample */
} pls_result;

typedef enum pls_beat_quality {
  PLS_BEAT_OK = 0,
  PLS_BEAT_INTERPOLATED = 1
  /* rejected beats are never pushed: PLS_ERROR_INVALID_ARGUMENT */
} pls_beat_quality;

/* Opaque shim handle. */
typedef struct pls_shim pls_shim;

/* Same shape as prism_render: prism_result is a C enum, an int. */
typedef int32_t (*pls_render_fn)(void* core, float* out_frames, uint32_t frame_count);

typedef struct pls_config {
  uint32_t sample_rate;      /* must be PLS_SAMPLE_RATE */
  uint32_t max_block_frames; /* capacity of every per-block buffer; larger blocks are split */
  uint32_t command_capacity; /* command queue slots, a power of two */
  uint32_t beat_capacity;    /* beats held for the future at once */
  double engine_trim_db;     /* static trim on the engine buffer, <= 0; default -6 */
} pls_config;

typedef struct pls_stats {
  uint64_t frames_rendered;        /* same as pls_frames_rendered */
  uint64_t render_errors;          /* blocks whose render call returned non-zero or wrote a
                                      non-finite sample; each was zeroed before the high-pass */
  uint64_t beats_played;
  uint64_t beats_dropped_late;     /* onset already passed when the audio thread saw it */
  uint64_t beats_dropped_full;     /* no free slot for a future beat */
  uint64_t limiter_active_frames;  /* frames with gain reduction applied */
  double limiter_min_gain;         /* lowest limiter gain since pls_open, 1.0 = never engaged */
  uint64_t device_unrequested_stops; /* streams miniaudio stopped when pls_stop did not ask: a
                                        lost endpoint. frames_rendered stops with it */
  uint64_t device_clock_samples;   /* successful IAudioClock positions used by the anchor */
  uint64_t device_clock_failures;  /* consumed polls where IAudioClock gave no usable position */
  uint64_t heartbeat_onset_measurements; /* played beats compared with a device-clock position */
  double device_anchor_error_ms;   /* latest observed error before any later slew */
  double device_anchor_slew_ms;    /* cumulative correction applied this stream */
  double heartbeat_onset_error_last_ms; /* estimated actual onset minus t_play, latest beat */
  double heartbeat_onset_error_abs_max_ms; /* largest absolute measured onset error */
  uint64_t heartbeat_onset_telemetry_dropped; /* measurements lost because their queue was full */
} pls_stats;

typedef struct pls_heartbeat_onset {
  double t_play_ms; /* scheduled T_engine time */
  double error_ms;  /* estimated actual device onset minus t_play */
} pls_heartbeat_onset;

/* PLS_ABI_VERSION of the loaded library. */
PLS_API int32_t pls_abi_version(void);

/* SHA-256, lowercase hex, over what the DLL was built from: native/CMakeLists.txt and every file
 * under native/include, native/src and native/third_party (recipe in native/CMakeLists.txt). Not
 * the build type or the compiler. A test compares it with the checkout, so a committed DLL built
 * from other sources fails. Never NULL. */
PLS_API const char* pls_source_hash(void);

/* Frames of delay from the start of the chain to the output, all in the limiter: its lookahead,
 * its guard and its detector's delay. 1639 frames, 34.1 ms. */
PLS_API uint32_t pls_latency_frames(void);

PLS_API pls_config pls_config_default(void);

/* Allocate everything the audio thread will ever touch. Does not open the device.
 * `render` and `core` are kept: the render function must stay loaded, and `core` alive and
 * with a scene loaded, until pls_close returns. frames_rendered counts from here, so open the
 * shim right after prism_load_scene and it equals the engine's phase. */
PLS_API int32_t pls_open(const pls_config* config, pls_render_fn render, void* core,
                         pls_shim** out_shim);

/* Start the stream, opening the default output device if no device is open. Refuses with
 * PLS_ERROR_SAMPLE_RATE unless the device runs at 48 kHz with no conversion.
 * INVALID_STATE if already started. The stream never follows a default-device change: it stays on
 * the endpoint it opened, so plug the headphones in before pls_start. If that endpoint goes away,
 * the stream stops by itself, frames_rendered stops advancing and device_unrequested_stops counts
 * it; recover with pls_stop, pls_set_time_origin_ns and pls_start, which opens the default device
 * afresh and checks it again. A kept device that fails to start is reopened the same way, once.
 * Send pls_set_time_origin_ns before every pls_start. */
PLS_API int32_t pls_start(pls_shim* shim);

/* Stop the stream. No fade: the bridge fades first. Idempotent. The engine's phase pauses
 * with the stream and is not reset. After a stop pls_stop did not ask for, it also closes the
 * device, so the next pls_start opens it afresh. */
PLS_API int32_t pls_stop(pls_shim* shim);

/* Stop and free. NULL is a no-op. After it returns nothing calls the render function. */
PLS_API void pls_close(pls_shim* shim);

/* Session gain: linear, target in [0, 1], reached by a straight ramp over ramp_ms from
 * wherever it is when the audio thread takes the command. ramp_ms >= 0; 0 steps. Starts at 0:
 * the engine is silent until the first command. */
PLS_API int32_t pls_set_session_gain(pls_shim* shim, double target, double ramp_ms);

/* Heartbeat layer level: the peak of a beat in dBFS, <= 0, or -INFINITY for silence. Audible
 * targets move linearly in dB; a boundary to or from silence is equal-power. The final frame of a
 * fade to silence is exactly zero. Starts at silence. */
PLS_API int32_t pls_set_heartbeat_level(pls_shim* shim, double target_dbfs, double ramp_ms);

/* A beat to sound at t_play (T_engine ms), closing an interval of rr_ms (250 to 2500).
 * quality is a pls_beat_quality. A beat whose onset has passed when the audio thread takes
 * it is dropped and counted, never played late. */
PLS_API int32_t pls_push_beat(pls_shim* shim, double t_play_ms, double rr_ms, int32_t quality);

/* T_engine's origin, as Python's time.perf_counter_ns() read it. The next device callback anchors
 * T_engine to the latest IAudioClock position and correlated QueryPerformanceCounter value from
 * the non-audio poller. If that first result is unavailable, its buffer's first output frame uses
 * QueryPerformanceCounter less the origin plus one device period until a later observation
 * corrects it by slew. A new origin starts a stream generation: beats queued or placed for the
 * previous run are dropped and counted instead of playing against a changed map. */
PLS_API int32_t pls_set_time_origin_ns(pls_shim* shim, int64_t perf_counter_origin_ns);

/* Output frame `output_frame` leaves the limiter at t_engine_ms. Replaces any anchor. */
PLS_API int32_t pls_set_clock_anchor(pls_shim* shim, double t_engine_ms, uint64_t output_frame);

/* Frames passed to the render function since pls_open. Any thread; one atomic load. */
PLS_API uint64_t pls_frames_rendered(const pls_shim* shim);

/* Counters, limiter state and device timing. Any thread; a snapshot of atomics, not one instant.
 * Also reads the device's state, so a lost stream is counted here even when miniaudio reports
 * nothing. The callback only records timing atomics; the control thread does any logging. */
PLS_API int32_t pls_get_stats(const pls_shim* shim, pls_stats* out_stats);

/* Drain at most capacity individual onset measurements in FIFO order. The control thread is the
 * only consumer. Returns the number written; 0 for an empty queue or invalid/null arguments. No
 * logging occurs in the native layer or on the audio thread. */
PLS_API uint32_t pls_drain_heartbeat_onsets(pls_shim* shim, pls_heartbeat_onset* out_measurements,
                                            uint32_t capacity);

/* Run the whole chain for `frame_count` frames on the calling thread, as the device callback
 * would, writing the mono output to out_frames. engine_tap, if not NULL, receives the engine
 * buffer after the high-pass and before any gain. INVALID_STATE while the device is started.
 * For tests and offline renders. */
PLS_API int32_t pls_render_offline(pls_shim* shim, float* out_frames, float* engine_tap,
                                   uint32_t frame_count);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* PRISM_LIVE_SHIM_H */
