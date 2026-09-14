// heartbeat.h: the heartbeat layer's beat slots and voice (chain step 4), minimal for prompt 2.5.
//
// Called heartbeat_layer everywhere, never "pulse": the pulse stem is a different thing. Prompt 2.6
// keeps the ABI and replaces these internals (anchor slewing, onset error logging, spectral
// shaping).
//
// A beat is placed by the input frame of its onset: the frame, counted from pls_open, at which the
// voice starts in the mix before the limiter, so that its onset leaves the limiter kLatency frames
// later, on the output frame the anchor gave it. Voice: sin(2 pi 44 t) from phase 0 at the onset,
// times an envelope rising 0 -> 1 over an 8 ms raised cosine, then falling 1 -> 0 over a
// half-cosine of min(220 ms, 0.55 rr). Envelope peak 1.0. Voices that overlap are summed.

#ifndef PLS_HEARTBEAT_H
#define PLS_HEARTBEAT_H

#include <cstdint>

namespace pls {

constexpr double kHeartbeatHz = 44.0;
constexpr double kHeartbeatAttackMs = 8.0;
constexpr double kHeartbeatDecayMaxMs = 220.0;
constexpr double kHeartbeatDecayRrFraction = 0.55;

struct BeatSlot {
  int64_t onset_frame;   // input frame of the voice's first sample
  int32_t decay_frames;
  int32_t active;        // 0 free, 1 holds a beat
};

enum class BeatPlacement { kPlaced, kLate, kFull };

class HeartbeatLayer {
 public:
  // Control thread, at open. `slots` has `capacity` entries and outlives the layer.
  void attach(BeatSlot* slots, uint32_t capacity, double sample_rate);

  // Audio thread. A beat whose onset is before `block_start` is late: its first sample has passed.
  BeatPlacement place(int64_t onset_frame, int64_t block_start, double rr_ms);

  // Audio thread. Writes the summed voices for input frames block_start .. block_start + frames
  // into `voice` (overwriting it) and returns how many onsets fell in that span.
  uint32_t render(int64_t block_start, uint32_t frames, double* voice);

  int32_t attack_frames() const { return attack_frames_; }

 private:
  BeatSlot* slots_ = nullptr;
  uint32_t capacity_ = 0;
  double sample_rate_ = 48000.0;
  int32_t attack_frames_ = 0;
};

}  // namespace pls

#endif  // PLS_HEARTBEAT_H
