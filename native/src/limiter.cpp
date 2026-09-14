// limiter.cpp: see limiter.h for the algorithm and the alignment argument.

#include "limiter.h"

#include <cmath>

#include <xmmintrin.h>

namespace pls {

namespace {

constexpr double kPi = 3.14159265358979323846;

// Modified Bessel function of the first kind, order 0, by its power series.
double bessel_i0(double x) {
  const double quarter_x2 = 0.25 * x * x;
  double term = 1.0;
  double sum = 1.0;
  for (int k = 1; k < 500; ++k) {
    term *= quarter_x2 / (static_cast<double>(k) * static_cast<double>(k));
    sum += term;
    if (term < 1e-17 * sum) {
      break;
    }
  }
  return sum;
}

// sinc(tau) under a Kaiser window reaching zero at |tau| = half.
double kaiser_sinc(double tau, double half, double beta) {
  const double u = tau / half;
  const double inside = 1.0 - u * u;
  const double window = inside > 0.0 ? bessel_i0(beta * std::sqrt(inside)) / bessel_i0(beta) : 0.0;
  const double a = kPi * tau;
  return (tau == 0.0 ? 1.0 : std::sin(a) / a) * window;
}

// Sum of kernel[t] * window[t] over a multiple of four taps, four at a time with SSE (part of every
// x86-64 CPU), so the cost is fixed whatever the compiler decides to vectorise.
inline double dot(const float* kernel, const float* window, uint32_t taps) {
  __m128 sum = _mm_setzero_ps();
  for (uint32_t t = 0; t < taps; t += 4) {
    sum = _mm_add_ps(sum, _mm_mul_ps(_mm_loadu_ps(kernel + t), _mm_loadu_ps(window + t)));
  }
  float lanes[4];
  _mm_storeu_ps(lanes, sum);
  return static_cast<double>(lanes[0]) + static_cast<double>(lanes[1]) +
         static_cast<double>(lanes[2]) + static_cast<double>(lanes[3]);
}

// The half-band tap pairs: sum of kernel[i] * (window[i] + window[taps - 1 - i]) for i < taps / 2.
inline double folded_dot(const float* kernel, const float* window, uint32_t half) {
  __m128 sum = _mm_setzero_ps();
  const float* mirror = window + 2 * half - 4;
  for (uint32_t i = 0; i < half; i += 4) {
    // window[2 half - 1 - i - 3 .. 2 half - 1 - i], reversed to line up with kernel[i .. i + 3].
    const __m128 tail = _mm_shuffle_ps(_mm_loadu_ps(mirror - i), _mm_loadu_ps(mirror - i),
                                       _MM_SHUFFLE(0, 1, 2, 3));
    const __m128 pair = _mm_add_ps(_mm_loadu_ps(window + i), tail);
    sum = _mm_add_ps(sum, _mm_mul_ps(_mm_loadu_ps(kernel + i), pair));
  }
  float lanes[4];
  _mm_storeu_ps(lanes, sum);
  return static_cast<double>(lanes[0]) + static_cast<double>(lanes[1]) +
         static_cast<double>(lanes[2]) + static_cast<double>(lanes[3]);
}

// The peak near an 8x point: |v0|, or the vertex of the parabola through its neighbours when v0 is
// a local extremum. Never below |v0|.
inline double refine(double before, double v0, double after) {
  const double sign = v0 < 0.0 ? -1.0 : 1.0;
  const double a0 = sign * v0;
  const double am = sign * before;
  const double ap = sign * after;
  double peak = a0;
  if (a0 >= am && a0 >= ap) {
    const double curvature = 2.0 * a0 - am - ap;
    if (curvature > 0.0) {
      const double slope = ap - am;
      peak += slope * slope / (8.0 * curvature);
    }
  }
  return peak;
}

}  // namespace

void PeakDetector::init() {
  // Half-band: the point halfway between the window's samples kHalfbandHalfTaps - 1 and
  // kHalfbandHalfTaps. Tap i sits i - (kHalfbandHalfTaps - 1) samples from the first of them.
  const double half = static_cast<double>(kHalfbandHalfTaps);
  double taps[kHalfbandTaps];
  double sum = 0.0;
  for (uint32_t i = 0; i < kHalfbandTaps; ++i) {
    const double position = static_cast<double>(i) - (half - 1.0);
    taps[i] = kaiser_sinc(0.5 - position, half, kHalfbandBeta);
    sum += taps[i];
  }
  for (uint32_t i = 0; i < kHalfbandHalfTaps; ++i) {
    halfband_[i] = static_cast<float>(taps[i] / sum);  // unity gain at DC
  }

  // Fine stage: point j / 4 past the window's sample kFineHalfTaps - 1, in 96 kHz samples.
  const double fine_half = static_cast<double>(kFineHalfTaps);
  for (uint32_t j = 0; j < kFinePhases; ++j) {
    double fine[kFineTaps];
    double fine_sum = 0.0;
    for (uint32_t t = 0; t < kFineTaps; ++t) {
      const double position = static_cast<double>(t) - (fine_half - 1.0);
      fine[t] = kaiser_sinc(static_cast<double>(j) / kFinePhases - position, fine_half, kFineBeta);
      fine_sum += fine[t];
    }
    for (uint32_t t = 0; t < kFineTaps; ++t) {
      fine_[j][t] = static_cast<float>(fine[t] / fine_sum);
    }
  }

  for (float& v : input_) {
    v = 0.0f;
  }
  input_pos_ = 0;
  for (float& v : stream_) {
    v = 0.0f;
  }
  stream_pos_ = 0;
  for (double& v : previous_points_) {
    v = 0.0;
  }
  previous_end_peak_ = 0.0;
  previous_q_ = 0.0;
}

void PeakDetector::fine_points(double* points) {
  const float* window = stream_ + stream_pos_;
  points[0] = window[kFineHalfTaps - 1];
  for (uint32_t j = 1; j < kFinePhases; ++j) {
    points[j] = dot(fine_[j], window, kFineTaps);
  }
}

double PeakDetector::push(double x) {
  // Input window x[k - 2H + 1] .. x[k], H = kHalfbandHalfTaps. Its samples H - 1 and H are
  // x[c] and x[c + 1], c = k - H, so this step adds x[c] and x[c + 0.5] to the 96 kHz stream.
  const float sample = static_cast<float>(x);
  input_[input_pos_] = sample;
  input_[input_pos_ + kHalfbandTaps] = sample;
  input_pos_ = input_pos_ + 1 == kHalfbandTaps ? 0 : input_pos_ + 1;
  const float* input = input_ + input_pos_;
  const float centre = input[kHalfbandHalfTaps - 1];
  const float midpoint = static_cast<float>(folded_dot(halfband_, input, kHalfbandHalfTaps));

  // Stream window v[u - K + 1] .. v[u + K], K = kFineHalfTaps, after each write. With v[2c] the
  // newest sample, u = 2c - K: the 8x points c' + 0/8 .. 3/8, c' = c - K / 2. With v[2c + 1] the
  // newest, u = 2c + 1 - K: the points c' + 4/8 .. 7/8.
  double points[kOversample];
  stream_[stream_pos_] = centre;
  stream_[stream_pos_ + kFineTaps] = centre;
  stream_pos_ = stream_pos_ + 1 == kFineTaps ? 0 : stream_pos_ + 1;
  fine_points(points);
  stream_[stream_pos_] = midpoint;
  stream_[stream_pos_ + kFineTaps] = midpoint;
  stream_pos_ = stream_pos_ + 1 == kFineTaps ? 0 : stream_pos_ + 1;
  fine_points(points + kFinePhases);

  // The previous interval's 8x points followed by this one's: refine points 1..8 of that run,
  // which are the interior and far end of the previous interval [c' - 1, c'].
  double q = previous_end_peak_;
  for (uint32_t k = 1; k <= kOversample; ++k) {
    const double before = previous_points_[k - 1];
    const double v0 = k < kOversample ? previous_points_[k] : points[0];
    const double after = k + 1 < kOversample ? previous_points_[k + 1] : points[k + 1 - kOversample];
    const double peak = refine(before, v0, after);
    if (peak > q) {
      q = peak;
    }
    if (k == kOversample) {
      previous_end_peak_ = peak;
    }
  }
  for (uint32_t j = 0; j < kOversample; ++j) {
    previous_points_[j] = points[j];
  }
  // q covers [m, m + 1] and previous_q_ covers [m - 1, m], m = c' - 1 = k - kDetectorDelay.
  const double p = q > previous_q_ ? q : previous_q_;
  previous_q_ = q;
  return p;
}

void Limiter::init(double sample_rate, double target_dbtp) {
  detector_.init();
  hold_front_ = 0;
  hold_count_ = 0;
  release_prev_ = 1.0;
  for (double& v : smooth_ring_) {
    v = 1.0;
  }
  smooth_pos_ = 0;
  smooth_sum_ = static_cast<double>(kSmoothWindow);
  for (double& v : required_ring_) {
    v = 1.0;
  }
  required_pos_ = 0;
  for (double& v : delay_) {
    v = 0.0;
  }
  delay_pos_ = 0;
  step_ = 0;
  ceiling_ = std::pow(10.0, target_dbtp / 20.0);
  release_coefficient_ = 1.0 - std::exp(-1.0 / (kReleaseMs * 0.001 * sample_rate));
  active_frames_ = 0;
  min_gain_ = 1.0;
}

void Limiter::process(const double* in, float* out, uint32_t frames) {
  // Re-add the smoothing window from scratch once per block, so the running sum cannot drift.
  double exact_sum = 0.0;
  for (double v : smooth_ring_) {
    exact_sum += v;
  }
  smooth_sum_ = exact_sum;

  for (uint32_t i = 0; i < frames; ++i) {
    const double x = in[i];
    const double p = detector_.push(x);
    const double required = p > ceiling_ ? ceiling_ / p : 1.0;

    // Hold: minimum of the last L + 1 + 2G required gains. Expire first, then append, so the
    // deque never holds more than the window.
    if (hold_count_ > 0 && hold_[hold_front_].step + kHoldWindow <= step_) {
      hold_front_ = hold_front_ + 1 == kHoldWindow ? 0 : hold_front_ + 1;
      --hold_count_;
    }
    while (hold_count_ > 0) {
      const uint32_t back = (hold_front_ + hold_count_ - 1) % kHoldWindow;
      if (hold_[back].gain < required) {
        break;
      }
      --hold_count_;
    }
    hold_[(hold_front_ + hold_count_) % kHoldWindow] = HoldEntry{step_, required};
    ++hold_count_;
    const double held = hold_[hold_front_].gain;

    // Release.
    const double released = release_prev_ + (1.0 - release_prev_) * release_coefficient_;
    const double envelope = held < released ? held : released;
    release_prev_ = envelope;

    // Smooth over steps k - L .. k.
    smooth_sum_ += envelope - smooth_ring_[smooth_pos_];
    smooth_ring_[smooth_pos_] = envelope;
    smooth_pos_ = smooth_pos_ + 1 == kSmoothWindow ? 0 : smooth_pos_ + 1;
    double gain = smooth_sum_ / static_cast<double>(kSmoothWindow);

    // r_{k-L-G}: the ring holds steps k - L - G .. k after this write.
    required_ring_[required_pos_] = required;
    required_pos_ = required_pos_ + 1 == kRequiredDelay ? 0 : required_pos_ + 1;
    const double centre_required = required_ring_[required_pos_];
    if (gain > centre_required) {
      gain = centre_required;
    }

    // x[m_k - L - G] = x[k - kLatency].
    const double delayed = delay_[delay_pos_];
    delay_[delay_pos_] = x;
    delay_pos_ = delay_pos_ + 1 == kLatency ? 0 : delay_pos_ + 1;
    out[i] = static_cast<float>(delayed * gain);

    if (gain < 1.0 - 1e-9) {
      ++active_frames_;
    }
    if (gain < min_gain_) {
      min_gain_ = gain;
    }
    ++step_;
  }
}

}  // namespace pls
