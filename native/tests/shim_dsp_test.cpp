// shim_dsp_test.cpp: the shim's signal path against what it promises (prompts 2.5-2.6). No device.
//
// Links the shim's objects and drives the chain through the public ABI (pls_render_offline), or a
// piece of it directly where that is sharper:
//
//   abi          versions, the stats layout, defaults, argument checks, a full queue queues nothing
//   queue        the SPSC queue under a real producer thread: nothing lost, nothing reordered
//   frames       frames_rendered is exact across split blocks; render errors zero the block
//   highpass     the response at chosen frequencies, measured through the engine tap, against
//                the generated coefficients and the decided bounds
//   ramps        session gain and heartbeat curves; device-anchor observations never step and
//                their correction is bounded to 1 ms per second of stream time
//   beats        a beat's first non-zero sample leaves on its anchored output frame; late and
//                full beats are dropped and counted; complete voices never overlap
//   detector     the true-peak detector against a sine's exact peak, and against the reference
//   limiter      adversarial input through the whole chain never exceeds -1.0 dBTP by the
//                reference meter; well below the ceiling the output is the input, delayed
//   non-finite   a NaN or an infinity from render fails its block, which is zeroed whole: the
//                output stays finite, under the ceiling, and the beat through it plays
//
// The reference meter is independent of limiter.cpp and stricter than it: double precision, a
// half-band stage of 4096 taps each side (the limiter's has 1024), then 8x of the 96 kHz stream
// (16x of 48 kHz), with parabolic refinement. It is itself checked against sines and against a
// brute-force windowed sinc at the worst interval it reports.

#include <algorithm>
#include <atomic>
#include <cmath>
#include <complex>
#include <cstdarg>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include <emmintrin.h>
#include <xmmintrin.h>

#include "highpass_coefficients.h"
#include "limiter.h"
#include "prism_live_shim.h"
#include "process.h"
#include "spsc.h"

namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr double kRate = 48000.0;

int g_failures = 0;
int g_checks = 0;

__attribute__((format(gnu_printf, 2, 3)))
void check(bool ok, const char* format, ...) {
  ++g_checks;
  va_list args;
  va_start(args, format);
  std::printf(ok ? "  ok    " : "  FAIL  ");
  std::vprintf(format, args);
  std::printf("\n");
  va_end(args);
  if (!ok) {
    ++g_failures;
  }
}

double db(double linear) { return 20.0 * std::log10(linear); }

// --- Reference true-peak meter ----------------------------------------------------------------

double bessel_i0(double x) {
  const double quarter_x2 = 0.25 * x * x;
  double term = 1.0;
  double sum = 1.0;
  for (int k = 1; k < 2000; ++k) {
    term *= quarter_x2 / (static_cast<double>(k) * static_cast<double>(k));
    sum += term;
    if (term < 1e-18 * sum) {
      break;
    }
  }
  return sum;
}

// sinc(tau) under a Kaiser window reaching zero at |tau| = half; i0_beta = bessel_i0(beta).
double kaiser_sinc(double tau, double half, double beta, double i0_beta) {
  const double u = tau / half;
  const double inside = 1.0 - u * u;
  if (inside <= 0.0) {
    return 0.0;
  }
  const double window = bessel_i0(beta * std::sqrt(inside)) / i0_beta;
  return (tau == 0.0 ? 1.0 : std::sin(kPi * tau) / (kPi * tau)) * window;
}

// Sum of a[i] * b[i], n a multiple of 4, in four lanes so SSE2 carries it.
double dot4(const double* a, const double* b, int n) {
  __m128d s0 = _mm_setzero_pd();
  __m128d s1 = _mm_setzero_pd();
  for (int i = 0; i < n; i += 4) {
    s0 = _mm_add_pd(s0, _mm_mul_pd(_mm_loadu_pd(a + i), _mm_loadu_pd(b + i)));
    s1 = _mm_add_pd(s1, _mm_mul_pd(_mm_loadu_pd(a + i + 2), _mm_loadu_pd(b + i + 2)));
  }
  double lanes[4];
  _mm_storeu_pd(lanes, s0);
  _mm_storeu_pd(lanes + 2, s1);
  return lanes[0] + lanes[1] + lanes[2] + lanes[3];
}

double refine(double before, double v0, double after) {
  const double sign = v0 < 0.0 ? -1.0 : 1.0;
  const double a0 = sign * v0;
  const double am = sign * before;
  const double ap = sign * after;
  double peak = a0;
  if (a0 >= am && a0 >= ap) {
    const double curvature = 2.0 * a0 - am - ap;
    if (curvature > 0.0) {
      peak += (ap - am) * (ap - am) / (8.0 * curvature);
    }
  }
  return peak;
}

class ReferenceMeter {
 public:
  static constexpr int kHalfband = 4096;  // taps each side
  static constexpr double kHalfbandBeta = 14.0;
  static constexpr int kFine = 32;  // taps each side, on the 96 kHz stream
  static constexpr double kFineBeta = 10.0;
  static constexpr int kFinePhases = 8;  // 16x of 48 kHz
  static constexpr int kPointsPerSample = 2 * kFinePhases;

  ReferenceMeter() : halfband_(2 * kHalfband), fine_(kFinePhases * 2 * kFine) {
    double sum = 0.0;
    const double i0_halfband = bessel_i0(kHalfbandBeta);
    const double i0_fine = bessel_i0(kFineBeta);
    for (int i = 0; i < 2 * kHalfband; ++i) {
      halfband_[i] =
          kaiser_sinc(0.5 - (i - (kHalfband - 1)), kHalfband, kHalfbandBeta, i0_halfband);
      sum += halfband_[i];
    }
    for (double& v : halfband_) {
      v /= sum;
    }
    for (int j = 1; j < kFinePhases; ++j) {
      double* row = &fine_[j * 2 * kFine];
      double row_sum = 0.0;
      for (int t = 0; t < 2 * kFine; ++t) {
        row[t] = kaiser_sinc(static_cast<double>(j) / kFinePhases - (t - (kFine - 1)), kFine,
                             kFineBeta, i0_fine);
        row_sum += row[t];
      }
      for (int t = 0; t < 2 * kFine; ++t) {
        row[t] /= row_sum;
      }
    }
  }

  // q[n]: the peak of the reconstruction of x (zero outside it) over [n, n + 1], both ends.
  std::vector<double> interval_peaks(const std::vector<double>& x) const {
    const size_t n_samples = x.size();
    std::vector<double> padded(n_samples + 2 * kHalfband + 2, 0.0);
    std::copy(x.begin(), x.end(), padded.begin() + kHalfband);
    const size_t n_stream = 2 * n_samples;
    std::vector<double> stream(n_stream + 2 * kFine + 2, 0.0);
    for (size_t n = 0; n < n_samples; ++n) {
      stream[kFine + 2 * n] = x[n];
      stream[kFine + 2 * n + 1] = dot4(halfband_.data(), &padded[n + 1], 2 * kHalfband);
    }
    std::vector<double> q(n_samples, 0.0);
    const size_t n_points = n_stream * kFinePhases;
    double previous = 0.0;
    double current = 0.0;
    auto point = [&](size_t index) {
      if (index >= n_points) {
        return 0.0;
      }
      const size_t c = index / kFinePhases;
      const int j = static_cast<int>(index % kFinePhases);
      if (j == 0) {
        return stream[kFine + c];
      }
      return dot4(&fine_[j * 2 * kFine], &stream[c + 1], 2 * kFine);
    };
    current = point(0);
    for (size_t index = 0; index < n_points; ++index) {
      const double next = point(index + 1);
      const double peak = refine(previous, current, next);
      const size_t n = index / kPointsPerSample;
      q[n] = std::max(q[n], peak);
      if (index % kPointsPerSample == 0 && n > 0) {
        q[n - 1] = std::max(q[n - 1], peak);
      }
      previous = current;
      current = next;
    }
    return q;
  }

  static double peak(const std::vector<double>& q, size_t* where) {
    const auto it = std::max_element(q.begin(), q.end());
    *where = static_cast<size_t>(it - q.begin());
    return *it;
  }

 private:
  std::vector<double> halfband_;
  std::vector<double> fine_;
};

// Brute force: the largest |reconstruction| over [n - 1, n + 1] from one long windowed sinc,
// 512 points per sample. For checking the reference, not for use on long signals.
double brute_force_peak(const std::vector<double>& x, size_t n) {
  constexpr int kHalf = 8192;
  constexpr double kBeta = 14.0;
  const double i0_beta = bessel_i0(kBeta);
  double best = 0.0;
  for (int step = 0; step <= 1024; ++step) {
    const double t = static_cast<double>(n) - 1.0 + step / 512.0;
    const long centre = static_cast<long>(std::floor(t));
    double sum = 0.0;
    for (long i = std::max(0L, centre - kHalf + 1);
         i <= std::min(static_cast<long>(x.size()) - 1, centre + kHalf); ++i) {
      sum += x[static_cast<size_t>(i)] *
             kaiser_sinc(t - static_cast<double>(i), kHalf, kBeta, i0_beta);
    }
    best = std::max(best, std::fabs(sum));
  }
  return best;
}

// --- Driving the chain ------------------------------------------------------------------------

// A render function that plays a prepared buffer, then silence.
struct Playback {
  std::vector<float> samples;
  size_t position = 0;
  uint64_t frames_asked = 0;
  uint32_t largest_block = 0;
  bool fail = false;
};

int32_t play(void* core, float* out, uint32_t frames) {
  auto* playback = static_cast<Playback*>(core);
  for (uint32_t i = 0; i < frames; ++i) {
    out[i] = playback->position < playback->samples.size()
                 ? playback->samples[playback->position++]
                 : 0.0f;
  }
  playback->frames_asked += frames;
  playback->largest_block = std::max(playback->largest_block, frames);
  return playback->fail ? 7 : 0;
}

pls_shim* open_shim(Playback* playback, double trim_db = 0.0, uint32_t max_block = 480,
                    uint32_t command_capacity = 256, uint32_t beat_capacity = 64) {
  pls_config config = pls_config_default();
  config.engine_trim_db = trim_db;
  config.max_block_frames = max_block;
  config.command_capacity = command_capacity;
  config.beat_capacity = beat_capacity;
  pls_shim* shim = nullptr;
  if (pls_open(&config, play, playback, &shim) != PLS_OK) {
    std::printf("  FAIL  pls_open\n");
    ++g_failures;
    return nullptr;
  }
  return shim;
}

// Renders `frames` frames in calls of `chunk`, returning the output (and the tap if asked).
std::vector<float> render(pls_shim* shim, size_t frames, uint32_t chunk,
                          std::vector<float>* tap = nullptr) {
  std::vector<float> out(frames, 0.0f);
  if (tap != nullptr) {
    tap->assign(frames, 0.0f);
  }
  size_t done = 0;
  while (done < frames) {
    const uint32_t n = static_cast<uint32_t>(std::min<size_t>(chunk, frames - done));
    if (pls_render_offline(shim, &out[done], tap != nullptr ? &(*tap)[done] : nullptr, n) !=
        PLS_OK) {
      std::printf("  FAIL  pls_render_offline\n");
      ++g_failures;
      break;
    }
    done += n;
  }
  return out;
}

pls_stats stats_of(pls_shim* shim) {
  pls_stats stats{};
  (void)pls_get_stats(shim, &stats);
  return stats;
}

// --- abi --------------------------------------------------------------------------------------

void test_abi() {
  std::printf("abi\n");
  check(pls_abi_version() == PLS_ABI_VERSION && PLS_ABI_VERSION == 3, "pls_abi_version is 3");
  check(sizeof(pls_stats) == 128 && offsetof(pls_stats, limiter_min_gain) == 48 &&
            offsetof(pls_stats, device_unrequested_stops) == 56 &&
            offsetof(pls_stats, device_clock_samples) == 64 &&
            offsetof(pls_stats, heartbeat_onset_measurements) == 80 &&
            offsetof(pls_stats, heartbeat_onset_error_abs_max_ms) == 112 &&
            offsetof(pls_stats, heartbeat_onset_telemetry_dropped) == 120,
        "pls_stats: ABI 3 timing fields appended at offsets 64 through 120, 128 bytes in all");
  check(sizeof(pls_heartbeat_onset) == 16 && offsetof(pls_heartbeat_onset, error_ms) == 8,
        "pls_heartbeat_onset is two adjacent doubles");
  const std::string hash = pls_source_hash();
  const bool hex = hash.size() == 64 && std::all_of(hash.begin(), hash.end(), [](char c) {
                     return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
                   });
  check(hex, "pls_source_hash is 64 lowercase hex digits: %s", hash.c_str());
  check(pls_latency_frames() == pls::kLatency, "pls_latency_frames() = %u",
        static_cast<unsigned>(pls_latency_frames()));

  const pls_config d = pls_config_default();
  check(d.sample_rate == 48000 && d.max_block_frames == 2048 && d.command_capacity == 256 &&
            d.beat_capacity == 64 && d.engine_trim_db == -6.0,
        "pls_config_default: 48000, 2048 frames, 256 commands, 64 beats, -6 dB trim");

  Playback playback;
  pls_shim* shim = reinterpret_cast<pls_shim*>(&playback);
  auto open_with = [&](pls_config config, pls_render_fn fn) {
    shim = reinterpret_cast<pls_shim*>(&playback);
    const int32_t result = pls_open(&config, fn, &playback, &shim);
    if (result == PLS_OK) {
      pls_close(shim);
    }
    return result == PLS_OK || shim == nullptr ? result : -1;
  };
  pls_config bad = d;
  bad.sample_rate = 44100;
  check(open_with(bad, play) == PLS_ERROR_SAMPLE_RATE, "44.1 kHz config: PLS_ERROR_SAMPLE_RATE");
  bool all_invalid = open_with(d, nullptr) == PLS_ERROR_INVALID_ARGUMENT;
  for (const auto& edit : std::vector<void (*)(pls_config&)>{
           [](pls_config& c) { c.max_block_frames = 0; },
           [](pls_config& c) { c.command_capacity = 0; },
           [](pls_config& c) { c.command_capacity = 3; },
           [](pls_config& c) { c.command_capacity = 1u << 21; },
           [](pls_config& c) { c.beat_capacity = 0; },
           [](pls_config& c) { c.engine_trim_db = 0.5; },
           [](pls_config& c) { c.engine_trim_db = std::nan(""); },
           [](pls_config& c) { c.engine_trim_db = -INFINITY; }}) {
    pls_config c = d;
    edit(c);
    all_invalid = all_invalid && open_with(c, play) == PLS_ERROR_INVALID_ARGUMENT;
  }
  check(all_invalid, "bad configs and a null render function: PLS_ERROR_INVALID_ARGUMENT, "
                     "out_shim NULL");
  check(pls_open(&d, play, &playback, nullptr) == PLS_ERROR_INVALID_ARGUMENT &&
            pls_open(nullptr, play, &playback, &shim) == PLS_ERROR_INVALID_ARGUMENT,
        "null config or out_shim: PLS_ERROR_INVALID_ARGUMENT");
  pls_close(nullptr);

  shim = open_shim(&playback, 0.0, 480, 4, 4);
  const double nan = std::nan("");
  const bool setters_refuse =
      pls_set_session_gain(shim, -0.01, 0) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_set_session_gain(shim, 1.01, 0) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_set_session_gain(shim, nan, 0) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_set_session_gain(shim, 0.5, -1) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_set_session_gain(shim, 0.5, INFINITY) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_set_heartbeat_level(shim, 0.1, 0) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_set_heartbeat_level(shim, nan, 0) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_set_heartbeat_level(shim, INFINITY, 0) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_set_heartbeat_level(shim, -6, nan) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_push_beat(shim, 1000, 249.9, PLS_BEAT_OK) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_push_beat(shim, 1000, 2500.1, PLS_BEAT_OK) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_push_beat(shim, 1000, nan, PLS_BEAT_OK) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_push_beat(shim, nan, 800, PLS_BEAT_OK) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_push_beat(shim, INFINITY, 800, PLS_BEAT_OK) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_push_beat(shim, 1000, 800, 2) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_push_beat(shim, 1000, 800, -1) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_set_time_origin_ns(shim, -1) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_set_clock_anchor(shim, nan, 0) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_set_clock_anchor(shim, 0, (1ull << 62) + 1) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_set_session_gain(nullptr, 0.5, 0) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_get_stats(shim, nullptr) == PLS_ERROR_INVALID_ARGUMENT &&
      pls_drain_heartbeat_onsets(nullptr, nullptr, 1) == 0 &&
      pls_render_offline(shim, nullptr, nullptr, 1) == PLS_ERROR_INVALID_ARGUMENT;
  check(setters_refuse, "out-of-range, NaN and null arguments: PLS_ERROR_INVALID_ARGUMENT");
  check(pls_set_clock_anchor(shim, 0, 1ull << 62) == PLS_OK &&
            pls_set_heartbeat_level(shim, -INFINITY, 0) == PLS_OK &&
            pls_push_beat(shim, 1000, 250, PLS_BEAT_INTERPOLATED) == PLS_OK &&
            pls_push_beat(shim, 1000, 2500, PLS_BEAT_OK) == PLS_OK,
        "edge values accepted: anchor frame 2^62, level -inf, rr 250 and 2500");
  // The queue (4 slots) now holds exactly those four. A fifth command is refused and not queued.
  float scratch[4];
  check(pls_push_beat(shim, 5000, 800, PLS_BEAT_OK) == PLS_ERROR_QUEUE_FULL,
        "a full queue returns PLS_ERROR_QUEUE_FULL");
  (void)pls_render_offline(shim, scratch, nullptr, 4);
  pls_stats stats = stats_of(shim);
  // The two queued beats sit far in the future of the anchor; the refused one never arrived.
  check(stats.beats_dropped_late == 0 && stats.beats_dropped_full == 0 && stats.beats_played == 0,
        "the refused beat was never queued (late %llu, full %llu, played %llu)",
        static_cast<unsigned long long>(stats.beats_dropped_late),
        static_cast<unsigned long long>(stats.beats_dropped_full),
        static_cast<unsigned long long>(stats.beats_played));
  check(pls_push_beat(shim, 5000, 800, PLS_BEAT_OK) == PLS_OK,
        "after a block drains the queue, commands are accepted again");
  check(pls_render_offline(shim, scratch, nullptr, 0) == PLS_OK && pls_stop(shim) == PLS_OK &&
            pls_stop(shim) == PLS_OK,
        "zero frames renders nothing; pls_stop on a stopped shim is PLS_OK, twice");

  const unsigned int before = _mm_getcsr();
  (void)pls_render_offline(shim, scratch, nullptr, 4);
  check(_mm_getcsr() == before, "the caller's MXCSR is restored after a render");
  pls_close(shim);
}

// --- queue ------------------------------------------------------------------------------------

void test_queue() {
  std::printf("queue\n");
  constexpr uint32_t kCapacity = 8;
  pls::Command storage[kCapacity];
  pls::SpscQueue<pls::Command> queue;
  queue.attach(storage, kCapacity);
  pls::Command command{};
  uint32_t accepted = 0;
  for (uint32_t i = 0; i < kCapacity + 3; ++i) {
    command.frame = i;
    accepted += queue.try_push(command) ? 1u : 0u;
  }
  bool in_order = true;
  for (uint32_t i = 0; i < kCapacity; ++i) {
    in_order = in_order && queue.try_pop(command) && command.frame == i;
  }
  check(accepted == kCapacity && in_order && !queue.try_pop(command),
        "all %u slots usable, the next push refused, popped in order, then empty", kCapacity);

  constexpr uint64_t kItems = 2000000;
  queue.attach(storage, kCapacity);
  std::atomic<uint64_t> refused{0};
  std::thread producer([&] {
    pls::Command item{};
    for (uint64_t i = 0; i < kItems;) {
      item.frame = i;
      item.value = static_cast<double>(i) * 0.5;
      if (queue.try_push(item)) {
        ++i;
      } else {
        refused.fetch_add(1, std::memory_order_relaxed);
        std::this_thread::yield();
      }
    }
  });
  uint64_t expected = 0;
  bool intact = true;
  while (expected < kItems) {
    pls::Command item{};
    if (queue.try_pop(item)) {
      intact = intact && item.frame == expected && item.value == static_cast<double>(expected) * 0.5;
      ++expected;
    }
  }
  producer.join();
  check(intact && !queue.try_pop(command),
        "%llu items across two threads through 8 slots: none lost, reordered or torn (%llu full "
        "pushes retried)",
        static_cast<unsigned long long>(kItems), static_cast<unsigned long long>(refused.load()));
}

// --- frames -----------------------------------------------------------------------------------

void test_frames() {
  std::printf("frames\n");
  Playback playback;
  pls_shim* shim = open_shim(&playback, 0.0, 256);
  const uint32_t sizes[] = {1, 255, 256, 257, 1000, 4096, 17};
  uint64_t total = 0;
  std::vector<float> out(4096);
  for (int round = 0; round < 50; ++round) {
    for (uint32_t size : sizes) {
      (void)pls_render_offline(shim, out.data(), nullptr, size);
      total += size;
    }
  }
  const pls_stats stats = stats_of(shim);
  check(stats.device_unrequested_stops == 0, "no device, no unrequested stop");
  check(pls_frames_rendered(shim) == total && stats.frames_rendered == total &&
            playback.frames_asked == total,
        "frames_rendered = %llu, exactly the frames asked for and passed to render",
        static_cast<unsigned long long>(total));
  check(playback.largest_block == 256, "blocks larger than max_block_frames are split (largest %u)",
        playback.largest_block);

  playback.samples.assign(1000, 0.5f);
  playback.position = 0;
  playback.fail = true;
  std::vector<float> tap;
  (void)render(shim, 1000, 1000, &tap);
  const bool zeroed = std::all_of(tap.begin(), tap.end(), [](float v) { return v == 0.0f; });
  check(stats_of(shim).render_errors == 4 && zeroed,
        "a non-zero render result is counted per block (4 blocks) and the block is zeroed");
  pls_close(shim);
}

// --- highpass ---------------------------------------------------------------------------------

double design_response_db(double hz) {
  const std::complex<double> z1 = std::polar(1.0, -2.0 * kPi * hz / kRate);
  const std::complex<double> z2 = z1 * z1;
  std::complex<double> h = 1.0;
  for (const auto& c : pls::kHighpassCoefficients) {
    h *= (c[0] + c[1] * z1 + c[2] * z2) / (1.0 + c[3] * z1 + c[4] * z2);
  }
  return db(std::abs(h));
}

// Amplitude of the hz component of x[first..], by least squares on sin and cos.
double sine_amplitude(const std::vector<float>& x, size_t first, double hz) {
  double ss = 0.0;
  double sc = 0.0;
  double cc = 0.0;
  double xs = 0.0;
  double xc = 0.0;
  for (size_t i = first; i < x.size(); ++i) {
    const double w = 2.0 * kPi * hz * static_cast<double>(i) / kRate;
    const double s = std::sin(w);
    const double c = std::cos(w);
    ss += s * s;
    sc += s * c;
    cc += c * c;
    xs += x[i] * s;
    xc += x[i] * c;
  }
  const double det = ss * cc - sc * sc;
  const double a = (xs * cc - xc * sc) / det;
  const double b = (xc * ss - xs * sc) / det;
  return std::hypot(a, b);
}

void test_highpass() {
  std::printf("highpass\n");
  double worst_stop = -1000.0;
  for (int i = 0; i <= 6100; ++i) {
    worst_stop = std::max(worst_stop, design_response_db(1.0 + 0.01 * i));
  }
  worst_stop = std::max(worst_stop, design_response_db(62.0));
  double worst_pass = 0.0;
  double worst_gain = -1000.0;
  for (double hz = 69.35; hz <= 24000.0; hz *= 1.0005) {
    const double r = design_response_db(hz);
    worst_gain = std::max(worst_gain, r);
    if (hz <= 20000.0) {
      worst_pass = std::min(worst_pass, r);
    }
  }
  check(worst_stop <= -30.0, "coefficients: 1 to 62 Hz at most %.4f dB (<= -30)", worst_stop);
  check(worst_pass >= -1.0, "coefficients: 69.35 Hz to 20 kHz at least %.4f dB (>= -1)",
        worst_pass);
  check(std::pow(10.0, worst_gain / 20.0) <= 1.0 + 1e-9, "coefficients: never above unity (%.2e dB)",
        worst_gain);

  // Measured through the chain: the decided bounds, each widened by what a 2 s least-squares fit
  // on float samples can misread (0.005 dB). The coefficients above meet the bounds strictly.
  constexpr double kFitAllowanceDb = 0.005;
  struct Probe {
    double hz;
    double lo_db;
    double hi_db;
  };
  const Probe probes[] = {{20.0, -200, -30},  {36.0, -200, -30},   {44.0, -200, -30},
                          {50.0, -200, -30},  {55.0, -200, -30},   {62.0, -200, -30},
                          {69.35, -1.0, 0.0}, {73.4, -1.0, 0.0},   {100.0, -1.0, 0.0},
                          {146.8, -1.0, 0.0}, {1000.0, -1.0, 0.0}, {10000.0, -1.0, 0.0}};
  for (const Probe& probe : probes) {
    Playback playback;
    constexpr size_t kFrames = 5 * 48000;
    playback.samples.resize(kFrames);
    constexpr double kAmplitude = 0.5;
    for (size_t i = 0; i < kFrames; ++i) {
      playback.samples[i] = static_cast<float>(
          kAmplitude * std::sin(2.0 * kPi * probe.hz * static_cast<double>(i) / kRate));
    }
    pls_shim* shim = open_shim(&playback, 0.0, 2048);
    std::vector<float> tap;
    (void)render(shim, kFrames, 2048, &tap);
    pls_close(shim);
    // 3 s to settle (the sharpest section rings for tens of ms), then 2 s measured.
    const double measured = db(sine_amplitude(tap, 3 * 48000, probe.hz) / kAmplitude);
    const double expected = design_response_db(probe.hz);
    check(measured >= probe.lo_db - kFitAllowanceDb && measured <= probe.hi_db + kFitAllowanceDb &&
              std::fabs(measured - expected) <= kFitAllowanceDb,
          "%8.2f Hz through the tap: %9.4f dB (coefficients %9.4f dB)", probe.hz, measured,
          expected);
  }
}

// --- ramps ------------------------------------------------------------------------------------

void test_ramps() {
  std::printf("ramps\n");
  check(pls::ms_to_frames(50.0) == 2400 && pls::ms_to_frames(0.03125) == 2 &&
            pls::ms_to_frames(10.0104) == 480 && pls::ms_to_frames(10.0105) == 481 &&
            pls::ms_to_frames(0.0) == 0,
        "ramp lengths round to whole frames (1.5 -> 2, 480.4992 -> 480, 480.504 -> 481)");

  pls::LinearRamp ramp;
  ramp.reset(0.25);
  ramp.set(0.8, 3);
  const double a = ramp.next();
  const double b = ramp.next();
  const double c = ramp.next();
  const double d = ramp.next();
  check(a == 0.25 + (0.8 - 0.25) * (1.0 / 3.0) && b == 0.25 + (0.8 - 0.25) * (2.0 / 3.0) &&
            c == 0.8 && d == 0.8,
        "LinearRamp: frame i of N is start + (target - start) i / N; frame N is the target");
  ramp.set(0.1, 0);
  check(ramp.next() == 0.1, "LinearRamp: 0 frames steps");

  pls::HeartbeatRamp heartbeat;
  heartbeat.reset_db(-12.0);
  heartbeat.set_db(-INFINITY, 4);
  const double start = std::pow(10.0, -12.0 / 20.0);
  const double h1 = heartbeat.next();
  const double h2 = heartbeat.next();
  const double h3 = heartbeat.next();
  const double h4 = heartbeat.next();
  check(std::fabs(h1 - start * std::cos(kPi / 8.0)) < 1e-15 &&
            std::fabs(h2 - start / std::sqrt(2.0)) < 1e-15 &&
            std::fabs(h3 - start * std::cos(3.0 * kPi / 8.0)) < 1e-15 && h4 == 0.0 &&
            heartbeat.next() == 0.0,
        "HeartbeatRamp: fade to silence is cosine equal-power and frame N is digital zero");
  heartbeat.reset_db(-18.0);
  heartbeat.set_db(-13.0, 4);
  const double db1 = db(heartbeat.next());
  const double db2 = db(heartbeat.next());
  const double db3 = db(heartbeat.next());
  const double db4 = db(heartbeat.next());
  check(std::fabs(db1 - -16.75) < 1e-12 && std::fabs(db2 - -15.5) < 1e-12 &&
            std::fabs(db3 - -14.25) < 1e-12 && std::fabs(db4 - -13.0) < 1e-12,
        "HeartbeatRamp: audible targets move linearly in dB and land exactly");

  pls::ClockMapper clock;
  clock.reset(1000.0, 48000, 0);
  const int64_t before_observation = clock.frame_for(1500.0);
  const double observed_error = clock.observe(1005.0, 48000, 0);
  const int64_t after_observation = clock.frame_for(1500.0);
  clock.advance(48000);
  const double after_one_second = clock.anchor_ms();
  clock.advance(96000);
  check(observed_error == 5.0 && before_observation == 72000 &&
            after_observation == before_observation && after_one_second == 1001.0 &&
            clock.anchor_ms() == 1002.0 && clock.total_slew_ms() == 2.0,
        "ClockMapper: a +5 ms observation never steps, then slews exactly 1 ms per stream second");
  (void)clock.observe(clock.time_for(96000) - 4.0, 96000, 96000);
  clock.advance(144000);
  check(clock.anchor_ms() == 1001.0 && clock.total_slew_ms() == 1.0,
        "ClockMapper: negative correction is bounded to -1 ms per stream second too");

  // Through the chain. A sine well under the ceiling, so the limiter's gain is exactly 1 and
  // out[n + latency] must equal float(tap[n] * trim * gain[n]) bit for bit.
  constexpr double kTrimDb = -6.0;
  Playback playback;
  constexpr size_t kFrames = 48000 / 4;  // 250 ms
  playback.samples.resize(kFrames);
  for (size_t i = 0; i < kFrames; ++i) {
    playback.samples[i] =
        static_cast<float>(0.3 * std::sin(2.0 * kPi * 1000.0 * static_cast<double>(i) / kRate));
  }
  pls_shim* shim = open_shim(&playback, kTrimDb, 480);
  // The factor the shim computed at open, so a compile-time pow cannot differ from it by an ulp.
  const double trim = pls::chain_of(shim).engine_trim;
  const size_t latency = pls_latency_frames();
  std::vector<float> out(kFrames + latency);
  std::vector<float> tap(kFrames + latency);
  // Gain 0 -> 1 over 50 ms, taken at frame 0.
  (void)pls_set_session_gain(shim, 1.0, 50.0);
  (void)pls_render_offline(shim, out.data(), tap.data(), 4000);
  // 1 -> 0.25 over 10.0105 ms (481 frames), taken at frame 4000.
  (void)pls_set_session_gain(shim, 0.25, 10.0105);
  (void)pls_render_offline(shim, out.data() + 4000, tap.data() + 4000, 3000);
  // Mid-ramp retarget from wherever it is: -> 0.75 over 1000 frames, taken at frame 7000, then a
  // step to 0.5 at frame 7500.
  (void)pls_set_session_gain(shim, 0.75, 1000.0 / 48.0);
  (void)pls_render_offline(shim, out.data() + 7000, tap.data() + 7000, 500);
  (void)pls_set_session_gain(shim, 0.5, 0.0);
  (void)pls_render_offline(shim, out.data() + 7500, tap.data() + 7500,
                           static_cast<uint32_t>(kFrames + latency - 7500));

  std::vector<double> gain(kFrames);
  for (size_t n = 0; n < kFrames; ++n) {
    if (n < 2400) {
      gain[n] = static_cast<double>(n + 1) / 2400.0;
    } else if (n < 4000) {
      gain[n] = 1.0;
    } else if (n < 4481) {
      gain[n] = 1.0 + (0.25 - 1.0) * (static_cast<double>(n - 4000 + 1) / 481.0);
    } else if (n < 7000) {
      gain[n] = 0.25;
    } else if (n < 7500) {
      gain[n] = 0.25 + (0.75 - 0.25) * (static_cast<double>(n - 7000 + 1) / 1000.0);
    } else {
      gain[n] = 0.5;
    }
  }
  size_t mismatches = 0;
  size_t first_mismatch = 0;
  for (size_t n = 0; n < kFrames; ++n) {
    const float expected = static_cast<float>(static_cast<double>(tap[n]) * (trim * gain[n]));
    if (out[n + latency] != expected) {
      if (mismatches++ == 0) {
        first_mismatch = n;
      }
    }
  }
  check(mismatches == 0,
        "out[n + latency] == tap[n] * trim * gain[n] for all %llu frames: ramps land exactly on "
        "frames 2399, 4480 and after a retarget (%llu mismatches, first at %llu)",
        static_cast<unsigned long long>(kFrames), static_cast<unsigned long long>(mismatches),
        static_cast<unsigned long long>(first_mismatch));
  const pls_stats stats = stats_of(shim);
  check(stats.limiter_active_frames == 0 && stats.limiter_min_gain == 1.0,
        "the limiter never engaged during the ramp test");
  pls_close(shim);
}

// --- beats ------------------------------------------------------------------------------------

size_t first_nonzero(const std::vector<float>& x, size_t from, size_t to) {
  for (size_t i = from; i < std::min(to, x.size()); ++i) {
    if (x[i] != 0.0f) {
      return i;
    }
  }
  return SIZE_MAX;
}

void test_beats() {
  std::printf("beats\n");
  const int64_t latency = pls_latency_frames();
  constexpr double kLevelDbfs = -12.0;
  const double level = std::pow(10.0, kLevelDbfs / 20.0);

  Playback silence;
  pls_shim* shim = open_shim(&silence, 0.0, 480);
  (void)pls_set_heartbeat_level(shim, kLevelDbfs, 0.0);
  (void)pls_set_clock_anchor(shim, 1000.0, 48000);
  // A: t 1500 ms -> output frame 48000 + 24000. rr 800 -> decay 220 ms, 10560 frames.
  // B: t 2000.03125 ms -> 48000 + round(48001.5) = 96002 (a half rounds away from zero).
  //    rr 300 -> decay 165 ms, 7920 frames.
  (void)pls_push_beat(shim, 1500.0, 800.0, PLS_BEAT_OK);
  (void)pls_push_beat(shim, 2000.03125, 300.0, PLS_BEAT_INTERPOLATED);
  std::vector<float> out = render(shim, 120000, 480);

  constexpr int64_t kA = 72000;
  constexpr int64_t kB = 96002;
  constexpr int64_t kAttack = 384;
  constexpr int64_t kTail = 960;
  constexpr int64_t kALength = kAttack + 10560 + kTail;
  constexpr int64_t kBLength = kAttack + 7920 + kTail;
  const size_t onset_a = first_nonzero(out, 0, 200000);
  check(onset_a == static_cast<size_t>(kA + 1) && out[kA] == 0.0f,
        "beat A: silent through output frame %lld, first non-zero sample at %llu (sin(0) = 0 at the "
        "onset itself)",
        static_cast<long long>(kA), static_cast<unsigned long long>(onset_a));
  const size_t onset_b = first_nonzero(out, kA + kALength, 200000);
  check(onset_b == static_cast<size_t>(kB + 1) && out[kB] == 0.0f && out[kB - 1] == 0.0f,
        "beat B: first non-zero sample at %llu, anchored frame %lld",
        static_cast<unsigned long long>(onset_b), static_cast<long long>(kB));
  double peak_a = 0.0;
  double peak_b = 0.0;
  for (int64_t i = kA; i < kA + kALength; ++i) {
    peak_a = std::max(peak_a, std::fabs(static_cast<double>(out[i])));
  }
  for (int64_t i = kB; i < kB + kBLength; ++i) {
    peak_b = std::max(peak_b, std::fabs(static_cast<double>(out[i])));
  }
  const bool gap_is_silent =
      std::all_of(out.begin() + kA + kALength, out.begin() + kB,
                  [](float sample) { return sample == 0.0f; });
  check(std::fabs(db(peak_a / level)) < 0.001 && std::fabs(db(peak_b / level)) < 0.4,
        "filtered voices honour the -12 dBFS peak target (long %+.4f dB, short %+.4f dB)",
        db(peak_a), db(peak_b));
  check(gap_is_silent && out[kA + kALength] == 0.0f && out[kB + kBLength] == 0.0f,
        "complete filtered voices, including their 20 ms tails, do not overlap");
  pls_stats stats = stats_of(shim);
  check(stats.beats_played == 2 && stats.beats_dropped_late == 0 &&
            stats.heartbeat_onset_measurements == 0,
        "2 beats played, none dropped, and offline rendering claims no device-onset measurement");

  // 120000 input frames rendered. The next block starts at input frame 120000, so a beat whose
  // input onset (output frame - latency) is 119999 is late, and one at 120000 is just in time.
  const double frame_ms = 1.0 / 48.0;
  const double late_t = 1000.0 + static_cast<double>(120000 + latency - 1 - 48000) * frame_ms;
  const double on_time_t = late_t + frame_ms;
  (void)pls_push_beat(shim, late_t, 800.0, PLS_BEAT_OK);
  (void)pls_push_beat(shim, on_time_t, 800.0, PLS_BEAT_OK);
  std::vector<float> tail = render(shim, 20000, 480);
  stats = stats_of(shim);
  const size_t onset_c = first_nonzero(tail, 0, tail.size());
  check(stats.beats_dropped_late == 1 && stats.beats_played == 3 &&
            onset_c == static_cast<size_t>(latency + 1),
        "a beat one frame late is dropped and counted; one on time plays at its frame "
        "(late %llu, played %llu)",
        static_cast<unsigned long long>(stats.beats_dropped_late),
        static_cast<unsigned long long>(stats.beats_played));
  pls_close(shim);

  // rr_ms closes the interval before a beat; it says nothing about how soon the next event may
  // arrive. A long-RR voice followed at the minimum legal 250 ms spacing must therefore fit too.
  Playback boundary;
  shim = open_shim(&boundary, 0.0, 480);
  (void)pls_set_heartbeat_level(shim, kLevelDbfs, 0.0);
  (void)pls_set_clock_anchor(shim, 0.0, 0);
  (void)pls_push_beat(shim, 1000.0, 800.0, PLS_BEAT_OK);
  (void)pls_push_beat(shim, 1250.0, 250.0, PLS_BEAT_OK);
  out = render(shim, 80000, 480);
  constexpr int64_t kFirst = 48000;
  constexpr int64_t kNext = 60000;
  constexpr int64_t kLongestSupport = 384 + 10560 + 960;  // 248 ms
  const bool boundary_gap =
      std::all_of(out.begin() + kFirst + kLongestSupport, out.begin() + kNext,
                  [](float sample) { return sample == 0.0f; });
  const size_t boundary_second = first_nonzero(out, kNext, out.size());
  check(boundary_gap && boundary_second == static_cast<size_t>(kNext + 1),
        "a long-RR voice ends after 248 ms before a next legal beat 250 ms later");
  pls_close(shim);

  // No anchor: dropped as late. No free slot: dropped as full.
  Playback quiet;
  shim = open_shim(&quiet, 0.0, 480, 256, 2);
  (void)pls_push_beat(shim, 5000.0, 800.0, PLS_BEAT_OK);
  float scratch[16];
  (void)pls_render_offline(shim, scratch, nullptr, 16);
  check(stats_of(shim).beats_dropped_late == 1, "a beat before any anchor is dropped as late");
  (void)pls_set_clock_anchor(shim, 0.0, 0);
  for (int i = 0; i < 3; ++i) {
    (void)pls_push_beat(shim, 5000.0 + 1000.0 * i, 800.0, PLS_BEAT_OK);
  }
  (void)pls_render_offline(shim, scratch, nullptr, 16);
  check(stats_of(shim).beats_dropped_full == 1, "with 2 slots, a third future beat is dropped as full");
  pls_close(shim);
}

void test_reanchor() {
  std::printf("re-anchor\n");
  Playback silence;
  pls_shim* shim = open_shim(&silence, 0.0, 480);
  (void)pls_set_heartbeat_level(shim, -12.0, 0.0);

  // Run 1: establish the device map and place one future beat in a slot.
  (void)pls_set_time_origin_ns(shim, 0);
  (void)pls_push_beat(shim, 2000.0, 800.0, PLS_BEAT_OK);
  pls::DeviceClockSample first{true, true, 1, 1000000000LL, 0};
  std::vector<float> first_block(480);
  pls::render_device(pls::chain_of(shim), first_block.data(), 1, 480, &first);

  // One more old-run beat is still in the FIFO when run 2's origin arrives. Both the occupied
  // slot and that queued beat must be invalidated; only the beat tagged after the origin may play.
  (void)pls_push_beat(shim, 2100.0, 800.0, PLS_BEAT_OK);
  (void)pls_set_time_origin_ns(shim, 0);
  (void)pls_push_beat(shim, 1600.0, 800.0, PLS_BEAT_OK);
  pls::DeviceClockSample second{true, true, 2, 1500000000LL, 480};
  std::vector<float> output(6000);
  pls::render_device(pls::chain_of(shim), output.data(), 1,
                     static_cast<uint32_t>(output.size()), &second);

  const pls_stats stats = stats_of(shim);
  const size_t onset = first_nonzero(output, 0, output.size());
  pls_heartbeat_onset measurement{};
  const uint32_t drained = pls_drain_heartbeat_onsets(shim, &measurement, 1);
  check(stats.beats_dropped_late == 2 && stats.beats_played == 1 && onset == 4801,
        "a re-anchor drops one placed and one queued old-run beat; the new-run beat plays");
  check(stats.device_clock_samples == 2 && stats.heartbeat_onset_measurements == 1 &&
            stats.heartbeat_onset_telemetry_dropped == 0 && drained == 1 &&
            measurement.t_play_ms == 1600.0 && std::fabs(measurement.error_ms) < 1e-12,
        "each measured onset retains its t_play and error for control-thread logging");

  // The previous valid relationship must not be reused after the poller reports a failed read.
  (void)pls_push_beat(shim, 1700.0, 250.0, PLS_BEAT_OK);
  pls::DeviceClockSample failed{true, false, 3, 0, 0};
  std::vector<float> after_failure(4000);
  pls::render_device(pls::chain_of(shim), after_failure.data(), 1,
                     static_cast<uint32_t>(after_failure.size()), &failed);
  const pls_stats failed_stats = stats_of(shim);
  check(failed_stats.beats_played == 2 && failed_stats.device_clock_failures == 1 &&
            failed_stats.heartbeat_onset_measurements == 1 &&
            pls_drain_heartbeat_onsets(shim, &measurement, 1) == 0,
        "a failed current clock read suppresses onset measurement instead of reusing stale data");
  pls_close(shim);
}

// --- detector ---------------------------------------------------------------------------------

void test_detector(const ReferenceMeter& meter) {
  std::printf("detector\n");
  // Sines, whose peak over any interval is known exactly. Phases put crests between samples.
  const double frequencies[] = {50.0,    440.0,   1000.0,  3001.0,  7919.0,  12e3,
                                15000.0, 18000.0, 19000.0, 20000.0, 21000.0, 22000.0};
  for (double hz : frequencies) {
    auto* detector = new pls::PeakDetector();
    detector->init();
    const double w = 2.0 * kPi * hz / kRate;
    const double phase = 0.37;
    constexpr int64_t kSteps = 24000;
    double worst = 0.0;
    for (int64_t k = 0; k < kSteps; ++k) {
      const double p = detector->push(std::sin(w * static_cast<double>(k) + phase));
      const int64_t m = k - pls::kDetectorDelay;
      if (m < 4096 || m > kSteps - 4096) {
        continue;  // the sine's own start is not a sine
      }
      // Exact peak over [m - 1, m + 1]: 1 if a crest falls inside, else the larger end.
      const double a = w * static_cast<double>(m - 1) + phase;
      const double b = w * static_cast<double>(m + 1) + phase;
      const double crest = std::ceil((a - kPi / 2.0) / kPi) * kPi + kPi / 2.0;
      const double exact = crest <= b ? 1.0 : std::max(std::fabs(std::sin(a)), std::fabs(std::sin(b)));
      worst = std::min(worst, db(p / exact));
    }
    delete detector;
    if (hz <= 20000.0) {
      check(worst > -0.02, "sine %7.0f Hz: detector at most %.4f dB under the exact peak", hz,
            worst);
    } else {
      std::printf("  info  sine %7.0f Hz: detector at most %.4f dB under the exact peak\n", hz,
                  worst);
    }
  }

  // The reference meter itself, on a sine: its peak must be the amplitude.
  {
    std::vector<double> x(48000);
    for (size_t i = 0; i < x.size(); ++i) {
      x[i] = 0.7 * std::sin(2.0 * kPi * 19000.0 * static_cast<double>(i) / kRate + 0.37);
    }
    std::vector<double> q = meter.interval_peaks(x);
    double hi = 0.0;
    for (size_t n = x.size() / 4; n < 3 * x.size() / 4; ++n) {
      hi = std::max(hi, q[n]);
    }
    check(std::fabs(db(hi / 0.7)) < 0.001,
          "reference meter on a 19 kHz sine: peak %.5f dB of its amplitude", db(hi / 0.7));
  }

  // White noise: detector against the reference, where the reference is within 3 dB of its top.
  std::mt19937_64 rng(2025);
  std::uniform_real_distribution<double> uniform(-1.0, 1.0);
  std::vector<double> x(10 * 48000);
  for (double& v : x) {
    v = 2.0 * uniform(rng);
  }
  const std::vector<double> q = meter.interval_peaks(x);
  const double top = *std::max_element(q.begin(), q.end());
  auto* detector = new pls::PeakDetector();
  detector->init();
  double worst = 0.0;
  size_t counted = 0;
  for (size_t k = 0; k < x.size(); ++k) {
    const double p = detector->push(x[k]);
    if (k < pls::kDetectorDelay + 1) {
      continue;
    }
    const size_t m = k - pls::kDetectorDelay;
    const double reference = std::max(q[m - 1], q[m]);
    if (reference >= top * 0.708) {
      worst = std::min(worst, db(p / reference));
      ++counted;
    }
  }
  delete detector;
  std::printf("  info  +-2 white noise, 10 s: detector at most %.3f dB under the reference over "
              "the %llu intervals within 3 dB of the top\n",
              worst, static_cast<unsigned long long>(counted));
  check(worst > -0.3, "white noise: the detector's worst miss (%.3f dB) is inside the limiter's "
                      "margin (target %.1f dBTP, ceiling -1.0)",
        worst, pls::kTargetDbtp);
}

// --- limiter ----------------------------------------------------------------------------------

struct Adversary {
  const char* name;
  std::vector<float> samples;
  bool guaranteed;
};

std::vector<Adversary> adversaries() {
  std::mt19937_64 rng(1234);
  std::uniform_real_distribution<double> uniform(-1.0, 1.0);
  std::normal_distribution<double> gauss(0.0, 1.0);
  const size_t s = 48000;
  std::vector<Adversary> list;
  auto add = [&](const char* name, size_t frames, bool guaranteed, auto fill) {
    Adversary a{name, std::vector<float>(frames, 0.0f), guaranteed};
    for (size_t i = 0; i < frames; ++i) {
      a.samples[i] = static_cast<float>(fill(i));
    }
    list.push_back(std::move(a));
  };
  add("+6 dBFS uniform white noise, 10 s", 10 * s, true, [&](size_t) { return 2.0 * uniform(rng); });
  add("gaussian noise, sigma +6 dBFS, 5 s", 5 * s, true, [&](size_t) { return 2.0 * gauss(rng); });
  {
    double g = 1.0;
    add("noise, level jumping +-18 dB every 97 frames", 5 * s, true, [&](size_t i) {
      if (i % 97 == 0) {
        g = std::pow(10.0, 18.0 * uniform(rng) / 20.0);
      }
      return g * uniform(rng);
    });
  }
  add("+12 dBFS noise bursts of 50 ms after 450 ms silence", 5 * s, true,
      [&](size_t i) { return (i % 24000) < 2400 && i >= 24000 ? 4.0 * uniform(rng) : 0.0; });
  add("full-scale square waves: 997 Hz, 1 kHz, 100 Hz", 6 * s, true, [&](size_t i) {
    const double t = static_cast<double>(i) / kRate;
    if (i < 2 * s) {
      return std::sin(2.0 * kPi * 997.0 * t) >= 0.0 ? 1.0 : -1.0;
    }
    if (i < 4 * s) {
      return ((i / 24) % 2) == 0 ? 1.0 : -1.0;
    }
    return ((i / 240) % 2) == 0 ? 1.0 : -1.0;
  });
  add("+6 dBFS sine at fs/4, phased for the largest inter-sample peak", 3 * s, true,
      [&](size_t i) { return 2.0 * std::sin(kPi / 2.0 * static_cast<double>(i) + kPi / 4.0); });
  add("+12 dBFS impulses and doublets", 4 * s, true, [&](size_t i) {
    if (i % 997 == 0) {
      return 4.0;
    }
    if (i % 1511 == 0) {
      return 4.0;
    }
    if (i % 1511 == 1) {
      return -4.0;
    }
    return 0.0;
  });
  add("1 kHz bursts at +10 dBFS starting on a crest, after silence", 4 * s, true, [&](size_t i) {
    const size_t in_cycle = i % 24000;
    return i >= s / 4 && in_cycle < 4800
               ? 3.0 * std::cos(2.0 * kPi * 1000.0 * static_cast<double>(in_cycle) / kRate)
               : 0.0;
  });
  add("+6 dBFS chirp 20 Hz to 20 kHz", 4 * s, true, [&](size_t i) {
    const double t = static_cast<double>(i) / kRate;
    return 2.0 * std::sin(2.0 * kPi * (20.0 + (20000.0 - 20.0) * t / 8.0) * t);
  });
  add("two tones at 21 and 23 kHz", 3 * s, true, [&](size_t i) {
    const double t = static_cast<double>(i) / kRate;
    return 1.2 * std::sin(2.0 * kPi * 21000.0 * t) + 1.2 * std::sin(2.0 * kPi * 23000.0 * t + 1.0);
  });
  add("sign(sinc) sequences of 33 samples (+8 dB inter-sample peaks)", 2 * s, true, [&](size_t i) {
    const long offset = static_cast<long>(i % 3000) - 1500;
    if (i < 3000 || offset < -16 || offset > 16) {
      return 0.0;
    }
    const double t = 0.5 - static_cast<double>(offset);
    return std::sin(kPi * t) / (kPi * t) > 0.0 ? 1.0 : -1.0;
  });
  add("gated alternating +-1 (energy at exactly fs/2)", 2 * s, false,
      [&](size_t i) { return i >= 4800 && i < 91200 ? ((i % 2) == 0 ? 1.0 : -1.0) : 0.0; });
  return list;
}

void test_limiter(const ReferenceMeter& meter) {
  std::printf("limiter (trim 0 dB, session gain 1, heartbeat 0 dBFS every 400 ms)\n");
  const size_t latency = pls_latency_frames();
  double worst_guaranteed = -100.0;
  std::vector<double> first_output;
  for (Adversary& adversary : adversaries()) {
    Playback playback;
    playback.samples = adversary.samples;
    const size_t frames = playback.samples.size() + latency + 4800;
    pls_shim* shim = open_shim(&playback, 0.0, 480, 256, 64);
    (void)pls_set_session_gain(shim, 1.0, 0.0);
    (void)pls_set_heartbeat_level(shim, 0.0, 0.0);
    (void)pls_set_clock_anchor(shim, 0.0, 0);
    for (double t = 400.0; t < static_cast<double>(frames) / 48.0 && t < 400.0 * 64; t += 400.0) {
      (void)pls_push_beat(shim, t, 400.0, PLS_BEAT_OK);
    }
    const std::vector<float> out = render(shim, frames, 480);
    const pls_stats stats = stats_of(shim);
    pls_close(shim);

    const std::vector<double> y(out.begin(), out.end());
    const std::vector<double> q = meter.interval_peaks(y);
    size_t where = 0;
    const double peak_db = db(ReferenceMeter::peak(q, &where));
    if (first_output.empty()) {
      first_output = y;
    }
    if (adversary.guaranteed) {
      worst_guaranteed = std::max(worst_guaranteed, peak_db);
      check(peak_db <= PLS_CEILING_DBTP,
            "%-62s %+.3f dBTP (min gain %.3f)", adversary.name, peak_db, stats.limiter_min_gain);
    } else {
      std::printf("  info  %-62s %+.3f dBTP: outside the guarantee (limiter.h)\n", adversary.name,
                  peak_db);
    }
  }
  std::printf("  info  worst guaranteed case %+.3f dBTP: %.3f dB under the ceiling\n",
              worst_guaranteed, PLS_CEILING_DBTP - worst_guaranteed);

  // The reference against brute force, on the first 2 s of the first case's output, at the worst
  // interval it reports there.
  {
    const std::vector<double> y(first_output.begin(), first_output.begin() + 2 * 48000);
    const std::vector<double> q = meter.interval_peaks(y);
    size_t where = 0;
    (void)ReferenceMeter::peak(q, &where);
    const double reference = std::max(q[where], where > 0 ? q[where - 1] : 0.0);
    const double brute = brute_force_peak(y, where);
    check(std::fabs(db(brute / reference)) < 0.05,
          "reference meter against a 16384-tap sinc at 512 points per sample, on its worst "
          "interval: %+.4f vs %+.4f dBTP",
          db(reference), db(brute));
  }

  // Transparency: well below the ceiling the limiter is a pure delay, before and after it works.
  Playback playback;
  std::mt19937_64 rng(99);
  std::uniform_real_distribution<double> uniform(-1.0, 1.0);
  constexpr size_t kFrames = 6 * 48000;
  playback.samples.resize(kFrames);
  for (size_t i = 0; i < kFrames; ++i) {
    const bool burst = i >= 2 * 48000 && i < 2 * 48000 + 4800;
    playback.samples[i] = static_cast<float>((burst ? 3.0 : 0.1) * uniform(rng));
  }
  pls_shim* shim = open_shim(&playback, 0.0, 480);
  (void)pls_set_session_gain(shim, 1.0, 0.0);
  std::vector<float> tap;
  const std::vector<float> out = render(shim, kFrames + latency, 480, &tap);
  const pls_stats stats = stats_of(shim);
  pls_close(shim);
  double before = 0.0;
  double after = 0.0;
  for (size_t n = 0; n < kFrames; ++n) {
    const double diff = std::fabs(static_cast<double>(out[n + latency]) - tap[n]);
    if (n < 2 * 48000 - pls::kLookahead - pls::kGuard - pls::kDetectorDelay - 16) {
      before = std::max(before, diff);
    } else if (n >= 4 * 48000) {
      after = std::max(after, diff);
    }
  }
  check(before <= 1e-6, "-20 dBFS noise before a burst: out[n + %llu] = in[n] to %.1e",
        static_cast<unsigned long long>(latency), before);
  check(after <= 1e-6, "the same, 1.9 s after a +10 dBFS burst released: to %.1e", after);
  check(stats.limiter_active_frames > 0 && stats.limiter_min_gain < 0.5,
        "the burst engaged it (%llu frames, min gain %.3f)",
        static_cast<unsigned long long>(stats.limiter_active_frames), stats.limiter_min_gain);
}

// --- non-finite -------------------------------------------------------------------------------

void test_non_finite(const ReferenceMeter& meter) {
  std::printf("non-finite (a NaN in one block, +inf in a later one, a beat through both)\n");
  const size_t latency = pls_latency_frames();
  // Silence, then from input frame 44000 a +1.6 dBFS 997 Hz tone that keeps the limiter engaged.
  // The NaN is in block 48000, the infinity in block 48960, a clean block between them.
  constexpr size_t kFrames = 72000;
  constexpr size_t kNextClean = 49440;
  std::vector<float> tone(kFrames, 0.0f);
  for (size_t i = 44000; i < kFrames; ++i) {
    tone[i] = static_cast<float>(1.2 * std::sin(2.0 * kPi * 997.0 * static_cast<double>(i) / kRate));
  }
  std::vector<float> broken = tone;
  broken[48100] = std::nanf("");
  broken[49000] = INFINITY;
  // The same source with both blocks zeroed: what the chain must render in their place.
  std::vector<float> zeroed = tone;
  std::fill(zeroed.begin() + 48000, zeroed.begin() + 48480, 0.0f);
  std::fill(zeroed.begin() + 48960, zeroed.begin() + 49440, 0.0f);

  // A beat at t 930 ms leaves the limiter on output frame 44640. Its voice starts in the mix at
  // input frame 44640 - latency = 43001 and, at rr 800 ms, lasts 11904 frames including its
  // filter tail: through both blocks.
  constexpr size_t kBeatFrame = 44640;
  auto run = [&](const std::vector<float>& samples, pls_stats* stats) {
    Playback playback;
    playback.samples = samples;
    pls_shim* shim = open_shim(&playback, 0.0, 480);
    (void)pls_set_session_gain(shim, 1.0, 0.0);
    (void)pls_set_heartbeat_level(shim, 0.0, 0.0);
    (void)pls_set_clock_anchor(shim, 0.0, 0);
    (void)pls_push_beat(shim, 930.0, 800.0, PLS_BEAT_OK);
    std::vector<float> out = render(shim, kFrames + latency, 480);
    *stats = stats_of(shim);
    pls_close(shim);
    return out;
  };
  pls_stats stats{};
  pls_stats zeroed_stats{};
  const std::vector<float> out = run(broken, &stats);
  const std::vector<float> reference = run(zeroed, &zeroed_stats);

  const bool finite =
      std::all_of(out.begin(), out.end(), [](float v) { return std::isfinite(v); });
  check(finite, "every output sample is finite");
  check(stats.render_errors == 2 && zeroed_stats.render_errors == 0,
        "render_errors counts both blocks (%llu)",
        static_cast<unsigned long long>(stats.render_errors));
  size_t mismatches = 0;
  for (size_t n = 0; n < out.size(); ++n) {
    mismatches += out[n] != reference[n] ? 1u : 0u;
  }
  check(mismatches == 0,
        "the output equals the same source with both blocks zeroed, bit for bit (%llu mismatches): "
        "each block was zeroed whole, before the high-pass",
        static_cast<unsigned long long>(mismatches));
  double recovered = 0.0;
  for (size_t n = kNextClean + latency; n < kNextClean + latency + 480; ++n) {
    recovered = std::max(recovered, static_cast<double>(std::fabs(out[n])));
  }
  check(recovered > 0.5 && mismatches == 0,
        "the next clean block (input %llu) leaves the limiter %llu frames later at up to %.3f",
        static_cast<unsigned long long>(kNextClean), static_cast<unsigned long long>(latency),
        recovered);
  const size_t onset = first_nonzero(out, 0, out.size());
  check(onset == kBeatFrame + 1 && stats.beats_played == 1 && stats.beats_dropped_late == 0,
        "the beat is still there: first non-zero sample at %llu, anchored frame %llu",
        static_cast<unsigned long long>(onset), static_cast<unsigned long long>(kBeatFrame));
  if (finite) {
    const std::vector<double> y(out.begin(), out.end());
    size_t where = 0;
    const double peak_db = db(ReferenceMeter::peak(meter.interval_peaks(y), &where));
    check(peak_db <= PLS_CEILING_DBTP, "true peak %+.3f dBTP (min gain %.3f)", peak_db,
          stats.limiter_min_gain);
  } else {
    check(false, "true peak: not measured, the output is not finite");
  }
}

}  // namespace

int main() {
  test_abi();
  test_queue();
  test_frames();
  test_highpass();
  test_ramps();
  test_beats();
  test_reanchor();
  const ReferenceMeter meter;
  test_detector(meter);
  test_limiter(meter);
  test_non_finite(meter);
  std::printf("%d checks, %d failed\n", g_checks, g_failures);
  return g_failures == 0 ? 0 : 1;
}
