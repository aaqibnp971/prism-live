// process.cpp: see process.h. Audio thread only, except ms_to_frames.

#include "process.h"

#include <algorithm>
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
constexpr double kPi = 3.14159265358979323846;

double db_to_amplitude(double db) {
  return std::isinf(db) ? 0.0 : std::pow(10.0, db / 20.0);
}

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
void take_commands(Chain& chain, int64_t block_start, uint32_t buffer_offset, bool device,
                   const DeviceClockSample* clock_sample) {
  Counters& counters = chain.counters;
  chain.clock.advance(block_start);
  counters.device_anchor_slew_ms.store(chain.clock.total_slew_ms(), std::memory_order_relaxed);

  const bool sample_present =
      device && clock_sample != nullptr && clock_sample->present;
  const bool sample_new =
      sample_present && clock_sample->attempt != chain.last_device_clock_attempt;
  bool sample_used = false;
  chain.has_device_clock = false;  // only a usable reading from this callback may measure an onset
  if (sample_new) {
    chain.last_device_clock_attempt = clock_sample->attempt;
    if (!clock_sample->valid) {
      counters.device_clock_failures.fetch_add(1u, std::memory_order_relaxed);
    }
  }

  auto publish_device_sample = [&](double measured_ms, double error_ms) {
    chain.device_clock_ms = measured_ms;
    chain.device_clock_output_frame = clock_sample->output_frame;
    chain.has_device_clock = true;
    // Payload first, count last. An acquiring reader that sees the count cannot see the previous
    // sample's payload (it may still see a later payload, as pls_stats is deliberately a snapshot).
    counters.device_anchor_error_ms.store(error_ms, std::memory_order_relaxed);
    counters.device_anchor_slew_ms.store(chain.clock.total_slew_ms(), std::memory_order_relaxed);
    counters.device_clock_samples.fetch_add(1u, std::memory_order_release);
    sample_used = true;
  };

  Command command{};
  while (chain.commands.try_pop(command)) {
    switch (command.kind) {
      case CommandKind::kSessionGain:
        chain.session_gain.set(command.value, ms_to_frames(command.ms));
        break;
      case CommandKind::kHeartbeatLevel:
        chain.heartbeat_level.set_db(command.value, ms_to_frames(command.ms));
        break;
      case CommandKind::kBeat: {
        if (!chain.clock.has_anchor() || command.generation != chain.active_generation) {
          counters.beats_dropped_late.fetch_add(1u, std::memory_order_relaxed);
          break;
        }
        const int64_t output_frame = chain.clock.frame_for(command.value);
        const int64_t onset = output_frame - static_cast<int64_t>(kLatency);
        switch (chain.heartbeat.place(onset, block_start, command.value, command.ms,
                                      command.generation)) {
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
        chain.clock.reset(command.value, static_cast<int64_t>(command.frame), block_start);
        chain.has_time_origin = false;
        chain.has_device_clock = false;
        break;
      case CommandKind::kTimeOrigin: {
        chain.active_generation = command.generation;
        const uint32_t invalidated = chain.heartbeat.keep_generation(command.generation);
        if (invalidated > 0) {
          counters.beats_dropped_late.fetch_add(invalidated, std::memory_order_relaxed);
        }
        // Output frame k is the k-th sample written out: the limiter emits output frames
        // block_start onward in this block. The first sample of this callback's buffer is output
        // frame block_start - buffer_offset, and the device plays that buffer about one period
        // from now. The limiter's latency is not added here: beat placement already takes it off
        // (onset = F - kLatency). Offline there is no period.
        chain.has_time_origin = true;
        chain.time_origin_ns = command.origin_ns;
        if (sample_present && clock_sample->valid) {
          const double measured_ms =
              static_cast<double>(clock_sample->qpc_ns - command.origin_ns) / 1e6;
          chain.clock.reset(measured_ms, clock_sample->output_frame, block_start);
          publish_device_sample(measured_ms, 0.0);
        } else {
          const int64_t now_ns = perf_counter_ns(chain.qpc_frequency);
          const double period_ms =
              device ? static_cast<double>(chain.device_period_frames) / kFramesPerMs : 0.0;
          chain.clock.reset(static_cast<double>(now_ns - command.origin_ns) / 1e6 + period_ms,
                            block_start - static_cast<int64_t>(buffer_offset), block_start);
          chain.has_device_clock = false;
        }
        break;
      }
    }
  }

  // A new origin above resets instead of correcting: startup is not a slew. Otherwise a fresh
  // position changes only the mapper's target. Reusing a still-fresh position within one device
  // callback is safe for onset telemetry, but it is observed and counted only once.
  if (sample_present && clock_sample->valid && chain.has_time_origin) {
    const double measured_ms =
        static_cast<double>(clock_sample->qpc_ns - chain.time_origin_ns) / 1e6;
    if (sample_new && !sample_used) {
      const double error_ms =
          chain.clock.observe(measured_ms, clock_sample->output_frame, block_start);
      publish_device_sample(measured_ms, error_ms);
    } else {
      chain.device_clock_ms = measured_ms;
      chain.device_clock_output_frame = clock_sample->output_frame;
      chain.has_device_clock = true;
    }
  }
}

void process_block(Chain& chain, float* out, float* tap, uint32_t frames, uint32_t buffer_offset,
                   bool device, const DeviceClockSample* clock_sample) {
  Counters& counters = chain.counters;
  const uint64_t block_start = counters.frames_rendered.load(std::memory_order_relaxed);
  take_commands(chain, static_cast<int64_t>(block_start), buffer_offset, device, clock_sample);

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
  OnsetClock onset_clock{};
  const OnsetClock* onset_clock_ptr = nullptr;
  if (chain.has_device_clock) {
    onset_clock = OnsetClock{chain.device_clock_ms, chain.device_clock_output_frame,
                             static_cast<int64_t>(kLatency)};
    onset_clock_ptr = &onset_clock;
  }
  const HeartbeatRenderResult heartbeat = chain.heartbeat.render(
      static_cast<int64_t>(block_start), frames, chain.voice_buffer, onset_clock_ptr,
      &chain.onset_telemetry);
  const double* voice = chain.voice_buffer;
  for (uint32_t i = 0; i < frames; ++i) {
    mix[i] += chain.heartbeat_level.next() * voice[i];
  }

  // 5. The limiter, last.
  chain.limiter.process(mix, out, frames);

  if (heartbeat.onsets > 0) {
    counters.beats_played.store(
        counters.beats_played.load(std::memory_order_relaxed) + heartbeat.onsets,
        std::memory_order_relaxed);
  }
  if (heartbeat.measured_onsets > 0) {
    // As with device-clock telemetry, publish the payload before the release count.
    counters.heartbeat_onset_error_last_ms.store(heartbeat.last_error_ms,
                                                  std::memory_order_relaxed);
    counters.heartbeat_onset_error_abs_max_ms.store(
        std::max(counters.heartbeat_onset_error_abs_max_ms.load(std::memory_order_relaxed),
                 heartbeat.max_abs_error_ms),
        std::memory_order_relaxed);
    counters.heartbeat_onset_measurements.fetch_add(heartbeat.measured_onsets,
                                                     std::memory_order_release);
  }
  if (heartbeat.telemetry_dropped > 0) {
    counters.heartbeat_onset_telemetry_dropped.fetch_add(heartbeat.telemetry_dropped,
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

void HeartbeatRamp::reset_db(double db) {
  value_ = db_to_amplitude(db);
  start_ = value_;
  target_ = value_;
  length_ = 0;
  position_ = 0;
  curve_ = Curve::kHold;
}

void HeartbeatRamp::set_db(double target_db, int64_t frames) {
  const double target = db_to_amplitude(target_db);
  if (frames <= 0) {
    reset_db(target_db);
    return;
  }
  start_ = value_;
  target_ = target;
  length_ = frames;
  position_ = 0;
  if (start_ == 0.0 && target_ == 0.0) {
    curve_ = Curve::kHold;
  } else if (start_ == 0.0) {
    curve_ = Curve::kEqualPowerIn;
  } else if (target_ == 0.0) {
    curve_ = Curve::kEqualPowerOut;
  } else {
    curve_ = Curve::kDb;
  }
}

double HeartbeatRamp::next() {
  if (position_ >= length_) {
    return value_;
  }
  ++position_;
  if (position_ == length_) {
    value_ = target_;
    return value_;
  }
  const double progress = static_cast<double>(position_) / static_cast<double>(length_);
  switch (curve_) {
    case Curve::kHold:
      value_ = target_;
      break;
    case Curve::kDb:
      value_ = start_ * std::pow(target_ / start_, progress);
      break;
    case Curve::kEqualPowerIn:
      value_ = target_ * std::sin(0.5 * kPi * progress);
      break;
    case Curve::kEqualPowerOut:
      value_ = start_ * std::cos(0.5 * kPi * progress);
      break;
  }
  return value_;
}

void ClockMapper::reset(double t_engine_ms, int64_t output_frame, int64_t stream_frame) {
  has_anchor_ = true;
  anchor_ms_ = t_engine_ms;
  target_ms_ = t_engine_ms;
  total_slew_ms_ = 0.0;
  anchor_output_frame_ = output_frame;
  last_slew_stream_frame_ = stream_frame;
}

void ClockMapper::advance(int64_t stream_frame) {
  if (!has_anchor_ || stream_frame <= last_slew_stream_frame_) {
    return;
  }
  const double available = static_cast<double>(stream_frame - last_slew_stream_frame_) /
                           static_cast<double>(kSampleRate) * kAnchorSlewMsPerSecond;
  const double wanted = target_ms_ - anchor_ms_;
  const double applied = std::clamp(wanted, -available, available);
  anchor_ms_ += applied;
  total_slew_ms_ += applied;
  last_slew_stream_frame_ = stream_frame;
}

double ClockMapper::observe(double measured_ms, int64_t measured_output_frame,
                            int64_t stream_frame) {
  advance(stream_frame);
  const double error_ms = measured_ms - time_for(measured_output_frame);
  target_ms_ = anchor_ms_ + error_ms;
  return error_ms;
}

int64_t ClockMapper::frame_for(double t_engine_ms) const {
  return anchor_output_frame_ + ms_to_frames(t_engine_ms - anchor_ms_);
}

double ClockMapper::time_for(int64_t output_frame) const {
  return anchor_ms_ +
         static_cast<double>(output_frame - anchor_output_frame_) / kFramesPerMs;
}

void render_offline(Chain& chain, float* out, float* tap, uint32_t frames) {
  const DenormalGuard guard;
  uint32_t done = 0;
  while (done < frames) {
    const uint32_t remaining = frames - done;
    const uint32_t block = remaining < chain.max_block_frames ? remaining : chain.max_block_frames;
    process_block(chain, out + done, tap != nullptr ? tap + done : nullptr, block, done, false,
                  nullptr);
    done += block;
  }
}

void render_device(Chain& chain, float* interleaved, uint32_t channels, uint32_t frames,
                   const DeviceClockSample* clock_sample) {
  const DenormalGuard guard;
  uint32_t done = 0;
  while (done < frames) {
    const uint32_t remaining = frames - done;
    const uint32_t block = remaining < chain.max_block_frames ? remaining : chain.max_block_frames;
    const float* mono = chain.device_buffer;
    process_block(chain, chain.device_buffer, nullptr, block, done, true,
                  clock_sample);
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
