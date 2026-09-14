// process.h: the per-block chain, independent of the device, so tests can drive it offline.
//
// One block of n <= max_block_frames frames, in order:
//
//   0. take every queued control command (session gain, heartbeat level, beats, anchors)
//   1. render(core, engine_buf, n); a non-zero result, or any non-finite sample in engine_buf, is
//      counted and the buffer zeroed. frames_rendered += n
//   2. high-pass engine_buf in place; the engine tap, if any, is copied here
//   3. engine_buf[i] * engine trim * session_gain[i]
//   4. + heartbeat_level[i] * voice[i]
//   5. true-peak limiter -> out[i], kLatency frames later
//
// Everything here runs on the audio thread and works only in storage allocated at pls_open. The
// counters are atomics, written only by the audio thread and readable from any thread.

#ifndef PLS_PROCESS_H
#define PLS_PROCESS_H

#include <atomic>
#include <cstdint>

#include "heartbeat.h"
#include "highpass.h"
#include "limiter.h"
#include "prism_live_shim.h"
#include "spsc.h"

namespace pls {

constexpr uint32_t kSampleRate = PLS_SAMPLE_RATE;

enum class CommandKind : uint32_t {
  kSessionGain,     // value = linear target, ms = ramp
  kHeartbeatLevel,  // value = linear amplitude target, ms = ramp
  kBeat,            // value = t_play ms, ms = rr ms, quality
  kClockAnchor,     // value = t_engine ms, frame = output frame
  kTimeOrigin,      // origin_ns = perf_counter origin
};

struct Command {
  CommandKind kind;
  int32_t quality;
  double value;
  double ms;
  uint64_t frame;
  int64_t origin_ns;
};

// A value that moves in a straight line to its target, one step per frame. Taking a target over
// N frames: frame 1 .. N get start + (target - start) i / N, and frame N is the target exactly.
class LinearRamp {
 public:
  void reset(double value) {
    value_ = value;
    start_ = value;
    target_ = value;
    length_ = 0;
    position_ = 0;
  }

  void set(double target, int64_t frames) {
    if (frames <= 0) {
      reset(target);
      return;
    }
    start_ = value_;
    target_ = target;
    length_ = frames;
    position_ = 0;
  }

  double next() {
    if (position_ < length_) {
      ++position_;
      value_ = position_ == length_
                   ? target_
                   : start_ + (target_ - start_) * (static_cast<double>(position_) /
                                                    static_cast<double>(length_));
    }
    return value_;
  }

  double value() const { return value_; }

 private:
  double value_ = 0.0;
  double start_ = 0.0;
  double target_ = 0.0;
  int64_t length_ = 0;
  int64_t position_ = 0;
};

// The counters are read from any thread without a lock; neither type may fall back to one.
static_assert(std::atomic<uint64_t>::is_always_lock_free, "atomic<uint64_t> must be lock-free");
static_assert(std::atomic<double>::is_always_lock_free, "atomic<double> must be lock-free");

struct Counters {
  std::atomic<uint64_t> frames_rendered{0};
  std::atomic<uint64_t> render_errors{0};
  std::atomic<uint64_t> beats_played{0};
  std::atomic<uint64_t> beats_dropped_late{0};
  std::atomic<uint64_t> beats_dropped_full{0};
  std::atomic<uint64_t> limiter_active_frames{0};
  std::atomic<double> limiter_min_gain{1.0};
};

struct Chain {
  // Written once at open, before any audio thread exists.
  pls_render_fn render = nullptr;
  void* core = nullptr;
  uint32_t max_block_frames = 0;
  double engine_trim = 1.0;
  float* engine_buffer = nullptr;   // max_block_frames
  double* mix_buffer = nullptr;     // max_block_frames
  double* voice_buffer = nullptr;   // max_block_frames
  float* device_buffer = nullptr;   // max_block_frames, mono before the channel copy
  int64_t qpc_frequency = 0;
  // Written by the control thread each time the device is initialised, before it starts.
  uint32_t device_period_frames = 0;

  SpscQueue<Command> commands;
  Counters counters;

  // Audio thread only.
  Highpass highpass;
  LinearRamp session_gain;
  LinearRamp heartbeat_level;
  HeartbeatLayer heartbeat;
  Limiter limiter;
  bool has_anchor = false;
  double anchor_ms = 0.0;
  int64_t anchor_output_frame = 0;
};

// Round ms at 48 kHz to whole frames, saturating far beyond any real value.
int64_t ms_to_frames(double ms);

// Audio thread: the chain for one call of pls_render_offline, split into blocks.
void render_offline(Chain& chain, float* out, float* tap, uint32_t frames);

// Audio thread: the device callback, `frames` interleaved frames of `channels` channels.
void render_device(Chain& chain, float* interleaved, uint32_t channels, uint32_t frames);

// The chain inside a shim from pls_open (shim.cpp). Not exported from the DLL: the native tests
// link the objects and use it to drive render_device without opening a device.
Chain& chain_of(pls_shim* shim);

}  // namespace pls

#endif  // PLS_PROCESS_H
