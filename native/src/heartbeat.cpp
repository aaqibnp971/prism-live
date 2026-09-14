// heartbeat.cpp: see heartbeat.h.

#include "heartbeat.h"

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
  for (uint32_t i = 0; i < capacity_; ++i) {
    slots_[i] = BeatSlot{0, 0, 0};
  }
}

BeatPlacement HeartbeatLayer::place(int64_t onset_frame, int64_t block_start, double rr_ms) {
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
      slots_[i].active = 1;
      return BeatPlacement::kPlaced;
    }
  }
  return BeatPlacement::kFull;
}

uint32_t HeartbeatLayer::render(int64_t block_start, uint32_t frames, double* voice) {
  for (uint32_t i = 0; i < frames; ++i) {
    voice[i] = 0.0;
  }
  const int64_t block_end = block_start + static_cast<int64_t>(frames);
  const double phase_step = 2.0 * kPi * kHeartbeatHz / sample_rate_;
  uint32_t onsets = 0;
  for (uint32_t s = 0; s < capacity_; ++s) {
    BeatSlot& slot = slots_[s];
    if (slot.active == 0) {
      continue;
    }
    const int64_t length = static_cast<int64_t>(attack_frames_) + slot.decay_frames;
    const int64_t end = slot.onset_frame + length;
    if (slot.onset_frame >= block_end) {
      continue;  // not yet
    }
    if (slot.onset_frame >= block_start) {
      ++onsets;
    }
    const int64_t first = slot.onset_frame > block_start ? slot.onset_frame : block_start;
    const int64_t last = end < block_end ? end : block_end;
    for (int64_t frame = first; frame < last; ++frame) {
      const int64_t k = frame - slot.onset_frame;
      double envelope;
      if (k < attack_frames_) {
        envelope = 0.5 - 0.5 * std::cos(kPi * static_cast<double>(k) / attack_frames_);
      } else {
        const double j = static_cast<double>(k - attack_frames_);
        envelope = 0.5 + 0.5 * std::cos(kPi * j / slot.decay_frames);
      }
      voice[frame - block_start] += std::sin(phase_step * static_cast<double>(k)) * envelope;
    }
    if (end <= block_end) {
      slot.active = 0;
    }
  }
  return onsets;
}

}  // namespace pls
