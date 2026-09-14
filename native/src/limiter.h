// limiter.h: the true-peak limiter, the last stage of the chain (step 5).
//
// The ceiling is -1.0 dBTP (PLS_CEILING_DBTP). The engine's own limiter is -3 dBFS sample peak and
// the heartbeat layer is added after it, so nothing upstream holds the ceiling. The limiter is a
// delay and a gain, nothing else: well below the ceiling its output is its input, kLatency frames
// later. It aims at kTargetDbtp, under the ceiling by more than its detector and its gain shape
// were measured to miss: the worst overshoot of the target on guaranteed material is 0.13 dB
// (+6 dBFS white noise, tests/shim_dsp_test.cpp, against a 4096-tap-per-side reference meter), so
// -1.5 dBTP leaves about 0.37 dB under the ceiling. native/README.md has the table.
//
// Per input sample x[i]:
//
//   detector  (PeakDetector) the peak of the band-limited reconstruction of x. Two interpolation
//             stages: a half-band windowed sinc to 96 kHz (Kaiser, kHalfbandHalfTaps taps each
//             side), then a 4x windowed sinc of that stream (kFineHalfTaps each side), which is 8x
//             of 48 kHz; each 8x point is refined by the vertex of the parabola through it and its
//             neighbours. q[m] is the peak over [m, m+1], both ends included, and
//             p[m] = max(q[m-1], q[m]) the peak over [m-1, m+1]. p[m] is known kDetectorDelay
//             samples after x[m] arrives. The long first stage is what full-band material needs:
//             a short kernel ignores sinc tails that a long reference meter sees (measured with
//             64 taps per phase: +0.29 dBTP out on +6 dBFS white noise at a -1.2 dBTP target).
//   required  r[m] = min(1, C / p[m]), C = 10^(kTargetDbtp / 20).
//   hold      h = min of the last L + 1 + 2G values of r (L = kLookahead, G = kGuard), kept in a
//             monotonic deque.
//   release   e = min(h, e_prev + (1 - e_prev) a_rel), about 80 ms. e <= h always.
//   smooth    s = mean of the last L + 1 values of e, clamped to <= r[m - L - G] (see below).
//   output    y = x[m - L - G] s, where m is the detector index of the current step.
//
// Alignment. Index the steps by k; step k pushes r_k := r[m_k], m_k = k - kDetectorDelay, and
// outputs y_k = x[m_k - L - G] s_k. Take any output step k' with |k' - k| <= G. Every e_j
// averaged into s_k' has j in [k' - L, k'], so j in [k - L - G, k + G], and
// e_j <= h_j = min(r_{j-L-2G}, ..., r_j). Both j - L - 2G <= k - L - G and k - L - G <= j hold,
// so r_{k-L-G} is inside that window, and s_k' <= r_{k-L-G} = r[m_k - L - G]. So the output
// sample x[m_k - L - G] and every output sample within G of it are multiplied by gains no larger
// than C / p[m_k - L - G]. p[m] and p[m+1] both cover [m, m+1], so this holds for every
// inter-sample interval as well. The clamp in `smooth` makes the centre case exact under
// rounding; mathematically it never binds.
//
// Why G, and what is not guaranteed. The bound at the samples themselves is not a true-peak bound:
// the reconstruction between samples sums every neighbour through the sinc kernel, and a gain that
// differs across those neighbours changes that sum. G widens the hold so the gain is flat to
// within +-G samples of the peak that sets it. Measured at a -1.2 dBTP target: on +6 dBFS white
// noise the overshoot falls from 0.18 dB at G = 32 to 0.13 dB at G = 512, and on 513-sample
// sign(sinc) sequences from 0.42 dB at G = 128 to 0.20 dB. Beyond G the kernel's tail is small
// but not zero, which is what the margin under the ceiling is for.
//
// Material with sustained energy at exactly fs/2 that starts or stops abruptly (a gated
// alternating +-1 sequence) has a reconstruction that grows with the logarithm of its length: its
// measured true peak depends on the meter's kernel length and no finite lookahead bounds it. The
// programme here never carries it: the engine's master low-pass tops out at 12 kHz and the
// heartbeat layer is 44 Hz. tests/shim_dsp_test.cpp prints what the limiter does with it
// (+0.8 dBTP) and does not assert it.
//
// pls_latency_frames() = kLatency = L + G + kDetectorDelay, constant.

#ifndef PLS_LIMITER_H
#define PLS_LIMITER_H

#include <cstdint>

namespace pls {

constexpr uint32_t kLookahead = 96;           // L: the gain's attack ramp, 2 ms at 48 kHz
constexpr uint32_t kGuard = 512;              // G
constexpr uint32_t kOversample = 8;
constexpr uint32_t kHalfbandHalfTaps = 1024;  // a multiple of 4
constexpr double kHalfbandBeta = 13.0;
constexpr uint32_t kFineHalfTaps = 12;        // a multiple of 4
constexpr double kFineBeta = 8.0;
constexpr uint32_t kDetectorDelay = kHalfbandHalfTaps + kFineHalfTaps / 2 + 1;
constexpr uint32_t kLatency = kLookahead + kGuard + kDetectorDelay;
constexpr double kTargetDbtp = -1.5;
constexpr double kReleaseMs = 80.0;

// The detector alone, so tests can hold it against a reference meter.
class PeakDetector {
 public:
  // Control thread, at open: the kernels and a silent history.
  void init();

  // Audio thread. Takes x[k] and returns p[k - kDetectorDelay]: the peak of the reconstruction
  // over [k - kDetectorDelay - 1, k - kDetectorDelay + 1].
  double push(double x);

 private:
  static constexpr uint32_t kHalfbandTaps = 2 * kHalfbandHalfTaps;
  static constexpr uint32_t kFineTaps = 2 * kFineHalfTaps;
  static constexpr uint32_t kFinePhases = kOversample / 2;

  // Point j / 4 of the way along the 96 kHz stream from the window's centre sample.
  void fine_points(double* points);

  // The half-band kernel folded about its centre: tap i weighs w[i] + w[kHalfbandTaps - 1 - i].
  float halfband_[kHalfbandHalfTaps] = {};
  float fine_[kFinePhases][kFineTaps] = {};

  // The last kHalfbandTaps inputs and the last kFineTaps 96 kHz samples, each written twice so one
  // contiguous window is always readable.
  float input_[2 * kHalfbandTaps] = {};
  uint32_t input_pos_ = 0;
  float stream_[2 * kFineTaps] = {};
  uint32_t stream_pos_ = 0;

  double previous_points_[kOversample] = {};  // the 8x points of the previous interval
  double previous_end_peak_ = 0.0;            // refined peak at the previous integer point
  double previous_q_ = 0.0;
};

class Limiter {
 public:
  // Control thread, at open: the detector's kernels and a silent, unity-gain state.
  void init(double sample_rate, double target_dbtp);

  // Audio thread. `in` and `out` hold `frames` samples and must not overlap.
  void process(const double* in, float* out, uint32_t frames);

  uint64_t active_frames() const { return active_frames_; }
  double min_gain() const { return min_gain_; }

 private:
  struct HoldEntry {
    uint64_t step;
    double gain;
  };

  static constexpr uint32_t kHoldWindow = kLookahead + 1 + 2 * kGuard;
  static constexpr uint32_t kSmoothWindow = kLookahead + 1;
  static constexpr uint32_t kRequiredDelay = kLookahead + kGuard + 1;

  PeakDetector detector_;

  HoldEntry hold_[kHoldWindow] = {};
  uint32_t hold_front_ = 0;
  uint32_t hold_count_ = 0;

  double release_prev_ = 1.0;
  double smooth_ring_[kSmoothWindow] = {};
  uint32_t smooth_pos_ = 0;
  double smooth_sum_ = 0.0;

  double required_ring_[kRequiredDelay] = {};
  uint32_t required_pos_ = 0;

  double delay_[kLatency] = {};
  uint32_t delay_pos_ = 0;

  uint64_t step_ = 0;
  double ceiling_ = 1.0;
  double release_coefficient_ = 0.0;

  uint64_t active_frames_ = 0;
  double min_gain_ = 1.0;
};

}  // namespace pls

#endif  // PLS_LIMITER_H
