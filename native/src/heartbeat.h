// heartbeat.h: the heartbeat layer's preallocated beat slots and production voice (chain step 4).
//
// Called heartbeat_layer everywhere, never "pulse": the pulse stem is a different thing.
//
// A beat is placed by the input frame of its onset: the frame, counted from pls_open, at which the
// voice starts in the mix before the limiter, so that its onset leaves the limiter kLatency frames
// later, on the output frame the anchor gave it. Voice: sin(2 pi 44 t) from phase 0 at the onset,
// times an envelope rising 0 -> 1 over an 8 ms raised cosine, then falling 1 -> 0 over a
// half-cosine of min(220 ms, 0.55 rr). Fourth-order Butterworth sections at 36 and 62 Hz confine
// the finite envelope's sidebands to the reserved band; the low-pass rolls off at 24 dB/octave.
// A 20 ms raised-cosine tail takes the filter state smoothly to digital zero. Complete support is
// at most 248 ms, so even a long-RR beat followed by the minimum legal 250 ms spacing cannot
// overlap the next voice.

#ifndef PLS_HEARTBEAT_H
#define PLS_HEARTBEAT_H

#include <cstdint>

#include "heartbeat_filter_coefficients.h"
#include "spsc.h"

namespace pls {

constexpr double kHeartbeatHz = 44.0;
constexpr double kHeartbeatAttackMs = 8.0;
constexpr double kHeartbeatDecayMaxMs = 220.0;
constexpr double kHeartbeatDecayRrFraction = 0.55;
constexpr double kHeartbeatFilterTailMs = 20.0;
// Calibrates a long (220 ms decay) complete shaped voice to a 1.0 sample peak. Shorter legal
// voices are measured separately in the tests and remain within 0.4 dB of their level target.
constexpr double kHeartbeatVoiceGain = 1.178422806750063;

struct BeatSlot {
  int64_t onset_frame;   // input frame of the voice's first sample
  int32_t decay_frames;
  int32_t active;        // 0 free, 1 holds a beat
  double t_play_ms;
  uint64_t generation;
  double filter_z1[kHeartbeatFilterSections];
  double filter_z2[kHeartbeatFilterSections];
};

enum class BeatPlacement { kPlaced, kLate, kFull };

struct OnsetClock {
  double t_engine_ms;      // measured time of output_frame
  int64_t output_frame;
  int64_t latency_frames;  // from voice input to this output timeline
};

struct HeartbeatRenderResult {
  uint32_t onsets = 0;
  uint32_t measured_onsets = 0;
  uint32_t telemetry_dropped = 0;
  double last_error_ms = 0.0;
  double max_abs_error_ms = 0.0;
};

struct HeartbeatOnset {
  double t_play_ms;
  double error_ms;
};

class HeartbeatLayer {
 public:
  // Control thread, at open. `slots` has `capacity` entries and outlives the layer.
  void attach(BeatSlot* slots, uint32_t capacity, double sample_rate);

  // Audio thread. A beat whose onset is before `block_start` is late: its first sample has passed.
  BeatPlacement place(int64_t onset_frame, int64_t block_start, double t_play_ms, double rr_ms,
                      uint64_t generation);

  // Audio thread, when a new stream origin supersedes the old one. Returns occupied slots
  // invalidated because their frames belonged to another stream run.
  uint32_t keep_generation(uint64_t generation);

  // Audio thread. Writes the summed voices for input frames block_start .. block_start + frames
  // into `voice` (overwriting it). `clock`, when non-null, is the latest real device position and
  // lets the caller expose onset error as atomics; offline renders deliberately pass null.
  HeartbeatRenderResult render(int64_t block_start, uint32_t frames, double* voice,
                               const OnsetClock* clock,
                               SpscQueue<HeartbeatOnset>* onset_telemetry);

  int32_t attack_frames() const { return attack_frames_; }
  int32_t tail_frames() const { return tail_frames_; }

 private:
  BeatSlot* slots_ = nullptr;
  uint32_t capacity_ = 0;
  double sample_rate_ = 48000.0;
  int32_t attack_frames_ = 0;
  int32_t tail_frames_ = 0;
};

}  // namespace pls

#endif  // PLS_HEARTBEAT_H
