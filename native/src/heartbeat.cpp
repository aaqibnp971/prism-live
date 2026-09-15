// heartbeat.cpp: see heartbeat.h.

#include "heartbeat.h"

#include <algorithm>
#include <cmath>

namespace pls {

namespace {

constexpr double kPi = 3.14159265358979323846;

}  // namespace

void HeartbeatLayer::attach(BeatSlot* slots, uint32_t capacity, double sample_rate) {
  slots_ = slots;
  capacity_ = capacity;
  sample_rate_ = sample_rate;
  attack_frames_ = static_cast<int32_t>(std::lround(kHeartbeatAttackMs * sample_rate / 1000.0));
  tail_frames_ =
      static_cast<int32_t>(std::lround(kHeartbeatFilterTailMs * sample_rate / 1000.0));
  for (uint32_t i = 0; i < capacity_; ++i) {
    slots_[i] = BeatSlot{};
  }
}

BeatPlacement HeartbeatLayer::place(int64_t onset_frame, int64_t block_start, double t_play_ms,
                                    double rr_ms, uint64_t generation) {
  if (onset_frame < block_start) {
    return BeatPlacement::kLate;
  }
  for (uint32_t i = 0; i < capacity_; ++i) {
    if (slots_[i].active == 0) {
      double decay_ms = kHeartbeatDecayRrFraction * rr_ms;
      if (decay_ms > kHeartbeatDecayMaxMs) {
        decay_ms = kHeartbeatDecayMaxMs;
      }
      slots_[i].onset_frame = onset_frame;
      slots_[i].decay_frames = static_cast<int32_t>(std::lround(decay_ms * sample_rate_ / 1000.0));
      slots_[i].t_play_ms = t_play_ms;
      slots_[i].generation = generation;
      for (int s = 0; s < kHeartbeatFilterSections; ++s) {
        slots_[i].filter_z1[s] = 0.0;
        slots_[i].filter_z2[s] = 0.0;
      }
      slots_[i].active = 1;
      return BeatPlacement::kPlaced;
    }
  }
  return BeatPlacement::kFull;
}

uint32_t HeartbeatLayer::keep_generation(uint64_t generation) {
  uint32_t dropped = 0;
  for (uint32_t i = 0; i < capacity_; ++i) {
    if (slots_[i].active != 0 && slots_[i].generation != generation) {
      slots_[i].active = 0;
      ++dropped;
    }
  }
  return dropped;
}

HeartbeatRenderResult HeartbeatLayer::render(int64_t block_start, uint32_t frames, double* voice,
                                             const OnsetClock* clock,
                                             SpscQueue<HeartbeatOnset>* onset_telemetry) {
  for (uint32_t i = 0; i < frames; ++i) {
    voice[i] = 0.0;
  }
  const int64_t block_end = block_start + static_cast<int64_t>(frames);
  const double phase_step = 2.0 * kPi * kHeartbeatHz / sample_rate_;
  HeartbeatRenderResult result;
  for (uint32_t s = 0; s < capacity_; ++s) {
    BeatSlot& slot = slots_[s];
    if (slot.active == 0) {
      continue;
    }
    const int64_t raw_length = static_cast<int64_t>(attack_frames_) + slot.decay_frames;
    const int64_t length = raw_length + tail_frames_;
    const int64_t end = slot.onset_frame + length;
    if (slot.onset_frame >= block_end) {
      continue;  // not yet
    }
    if (slot.onset_frame >= block_start) {
      ++result.onsets;
      if (clock != nullptr) {
        const int64_t output_frame = slot.onset_frame + clock->latency_frames;
        const double actual_ms = clock->t_engine_ms +
                                 static_cast<double>(output_frame - clock->output_frame) * 1000.0 /
                                     sample_rate_;
        const double error_ms = actual_ms - slot.t_play_ms;
        ++result.measured_onsets;
        result.last_error_ms = error_ms;
        result.max_abs_error_ms = std::max(result.max_abs_error_ms, std::fabs(error_ms));
        const HeartbeatOnset measurement{slot.t_play_ms, error_ms};
        if (onset_telemetry == nullptr || !onset_telemetry->try_push(measurement)) {
          ++result.telemetry_dropped;
        }
      }
    }
    const int64_t first = slot.onset_frame > block_start ? slot.onset_frame : block_start;
    const int64_t last = end < block_end ? end : block_end;
    for (int64_t frame = first; frame < last; ++frame) {
      const int64_t k = frame - slot.onset_frame;
      double x = 0.0;
      if (k < raw_length) {
        double envelope;
        if (k < attack_frames_) {
          envelope = 0.5 - 0.5 * std::cos(kPi * static_cast<double>(k) / attack_frames_);
        } else {
          const double j = static_cast<double>(k - attack_frames_);
          envelope = 0.5 + 0.5 * std::cos(kPi * j / slot.decay_frames);
        }
        x = std::sin(phase_step * static_cast<double>(k)) * envelope;
      }
      for (int section = 0; section < kHeartbeatFilterSections; ++section) {
        const double* c = kHeartbeatFilterCoefficients[section];
        const double y = c[0] * x + slot.filter_z1[section];
        slot.filter_z1[section] = c[1] * x - c[3] * y + slot.filter_z2[section];
        slot.filter_z2[section] = c[2] * x - c[4] * y;
        x = y;
      }
      if (k >= raw_length) {
        const double tail_position = static_cast<double>(k - raw_length + 1);
        const double tail_window =
            0.5 + 0.5 * std::cos(kPi * tail_position / static_cast<double>(tail_frames_));
        x *= tail_window;
      }
      voice[frame - block_start] += kHeartbeatVoiceGain * x;
    }
    if (end <= block_end) {
      slot.active = 0;
    }
  }
  return result;
}

}  // namespace pls
