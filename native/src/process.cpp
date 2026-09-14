// process.cpp: see process.h. Audio thread only, except ms_to_frames.

#include "process.h"

#include <cmath>

#include <xmmintrin.h>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <chrono>
#endif

namespace pls {

namespace {

constexpr double kFramesPerMs = static_cast<double>(kSampleRate) / 1000.0;
// Far past any real offset, and well inside int64 after rounding.
constexpr double kFrameLimit = 1e15;

// Flush denormals to zero for the whole call (MXCSR FTZ | DAZ) and restore the caller's MXCSR.
class DenormalGuard {
 public:
  DenormalGuard() : saved_(_mm_getcsr()) { _mm_setcsr(saved_ | 0x8040u); }
  ~DenormalGuard() { _mm_setcsr(saved_); }

 private:
  unsigned int saved_;
};

// time.perf_counter_ns() as CPython 3.11 computes it on Windows (_PyTime_MulDiv): the same
// integer arithmetic on the same QueryPerformanceCounter reading.
int64_t perf_counter_ns(int64_t frequency) {
#ifdef _WIN32
  LARGE_INTEGER now;
  QueryPerformanceCounter(&now);
  const int64_t ticks = now.QuadPart;
  return (ticks / frequency) * 1000000000LL + (ticks % frequency) * 1000000000LL / frequency;
#else
  (void)frequency;
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
#endif
}

// block_start is this block's first input frame; buffer_offset is how far into the caller's buffer
// (the device callback's, or the pls_render_offline call's) the block starts.
void take_commands(Chain& chain, int64_t block_start, uint32_t buffer_offset, bool device) {
  Counters& counters = chain.counters;
  Command command{};
  while (chain.commands.try_pop(command)) {
    switch (command.kind) {
      case CommandKind::kSessionGain:
        chain.session_gain.set(command.value, ms_to_frames(command.ms));
        break;
      case CommandKind::kHeartbeatLevel:
        chain.heartbeat_level.set(command.value, ms_to_frames(command.ms));
        break;
      case CommandKind::kBeat: {
        if (!chain.has_anchor) {
          counters.beats_dropped_late.store(
              counters.beats_dropped_late.load(std::memory_order_relaxed) + 1u,
              std::memory_order_relaxed);
          break;
        }
        const int64_t output_frame =
            chain.anchor_output_frame + ms_to_frames(command.value - chain.anchor_ms);
        const int64_t onset = output_frame - static_cast<int64_t>(kLatency);
        switch (chain.heartbeat.place(onset, block_start, command.ms)) {
          case BeatPlacement::kPlaced:
            break;
          case BeatPlacement::kLate:
            counters.beats_dropped_late.store(
                counters.beats_dropped_late.load(std::memory_order_relaxed) + 1u,
                std::memory_order_relaxed);
            break;
          case BeatPlacement::kFull:
            counters.beats_dropped_full.store(
                counters.beats_dropped_full.load(std::memory_order_relaxed) + 1u,
                std::memory_order_relaxed);
            break;
        }
        break;
      }
      case CommandKind::kClockAnchor:
        chain.has_anchor = true;
        chain.anchor_ms = command.value;
        chain.anchor_output_frame = static_cast<int64_t>(command.frame);
        break;
      case CommandKind::kTimeOrigin: {
        // Output frame k is the k-th sample written out: the limiter emits output frames
        // block_start onward in this block. The first sample of this callback's buffer is output
        // frame block_start - buffer_offset, and the device plays that buffer about one period
        // from now. The limiter's latency is not added here: beat placement already takes it off
        // (onset = F - kLatency). Offline there is no period.
        const int64_t now_ns = perf_counter_ns(chain.qpc_frequency);
        const double period_ms =
            device ? static_cast<double>(chain.device_period_frames) / kFramesPerMs : 0.0;
        chain.has_anchor = true;
        chain.anchor_ms = static_cast<double>(now_ns - command.origin_ns) / 1e6 + period_ms;
        chain.anchor_output_frame = block_start - static_cast<int64_t>(buffer_offset);
        break;
      }
    }
  }
}

void process_block(Chain& chain, float* out, float* tap, uint32_t frames, uint32_t buffer_offset,
                   bool device) {
  Counters& counters = chain.counters;
  const uint64_t block_start = counters.frames_rendered.load(std::memory_order_relaxed);
  take_commands(chain, static_cast<int64_t>(block_start), buffer_offset, device);

  // 1. The engine. A failed call, or one NaN or infinity in what it wrote, fails the whole block:
  // a non-finite sample would stay in the high-pass state for good, and the limiter passes NaN.
  float* engine = chain.engine_buffer;
  bool failed = chain.render(chain.core, engine, frames) != 0;
  for (uint32_t i = 0; i < frames && !failed; ++i) {
    failed = !std::isfinite(engine[i]);
  }
  if (failed) {
    counters.render_errors.store(counters.render_errors.load(std::memory_order_relaxed) + 1u,
                                 std::memory_order_relaxed);
    for (uint32_t i = 0; i < frames; ++i) {
      engine[i] = 0.0f;
    }
  }
  counters.frames_rendered.store(block_start + frames, std::memory_order_relaxed);

  // 2. High-pass, then the tap.
  chain.highpass.process(engine, frames);
  if (tap != nullptr) {
    for (uint32_t i = 0; i < frames; ++i) {
      tap[i] = engine[i];
    }
  }

  // 3. Trim and session gain.
  double* mix = chain.mix_buffer;
  for (uint32_t i = 0; i < frames; ++i) {
    mix[i] = static_cast<double>(engine[i]) * (chain.engine_trim * chain.session_gain.next());
  }

  // 4. The heartbeat layer, under its own level only.
  const uint32_t onsets =
      chain.heartbeat.render(static_cast<int64_t>(block_start), frames, chain.voice_buffer);
  const double* voice = chain.voice_buffer;
  for (uint32_t i = 0; i < frames; ++i) {
    mix[i] += chain.heartbeat_level.next() * voice[i];
  }

  // 5. The limiter, last.
  chain.limiter.process(mix, out, frames);

  if (onsets > 0) {
    counters.beats_played.store(counters.beats_played.load(std::memory_order_relaxed) + onsets,
                                std::memory_order_relaxed);
  }
  counters.limiter_active_frames.store(chain.limiter.active_frames(), std::memory_order_relaxed);
  counters.limiter_min_gain.store(chain.limiter.min_gain(), std::memory_order_relaxed);
}

}  // namespace

int64_t ms_to_frames(double ms) {
  double frames = ms * kFramesPerMs;
  if (frames > kFrameLimit) {
    frames = kFrameLimit;
  } else if (frames < -kFrameLimit) {
    frames = -kFrameLimit;
  }
  return static_cast<int64_t>(std::llround(frames));
}

void render_offline(Chain& chain, float* out, float* tap, uint32_t frames) {
  const DenormalGuard guard;
  uint32_t done = 0;
  while (done < frames) {
    const uint32_t remaining = frames - done;
    const uint32_t block = remaining < chain.max_block_frames ? remaining : chain.max_block_frames;
    process_block(chain, out + done, tap != nullptr ? tap + done : nullptr, block, done, false);
    done += block;
  }
}

void render_device(Chain& chain, float* interleaved, uint32_t channels, uint32_t frames) {
  const DenormalGuard guard;
  uint32_t done = 0;
  while (done < frames) {
    const uint32_t remaining = frames - done;
    const uint32_t block = remaining < chain.max_block_frames ? remaining : chain.max_block_frames;
    const float* mono = chain.device_buffer;
    process_block(chain, chain.device_buffer, nullptr, block, done, true);
    float* frame_out = interleaved + static_cast<uint64_t>(done) * channels;
    for (uint32_t i = 0; i < block; ++i) {
      for (uint32_t c = 0; c < channels; ++c) {
        frame_out[c] = mono[i];
      }
      frame_out += channels;
    }
    done += block;
  }
}

}  // namespace pls
