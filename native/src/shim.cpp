// shim.cpp: the pls_* functions (control thread) and the output device (miniaudio).
//
// Everything the audio thread will touch is allocated in pls_open and freed in pls_close. The
// control calls validate on the calling thread and hand the audio thread a POD command through the
// lock-free queue; they never block and never touch the audio thread's state directly. The device
// callback only reads a lock-free timing mailbox and forwards to pls::render_device (process.cpp).
// A non-audio timing thread makes the IAudioClock calls. The notification callback only counts a
// stop pls_stop did not ask for, in atomics.

#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <new>
#include <thread>

#include "miniaudio.h"
#include "prism_live_shim.h"
#include "process.h"

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <audioclient.h>
#include <objbase.h>
#endif

#ifndef PLS_SOURCE_HASH
#error "PLS_SOURCE_HASH must be defined by the build (native/CMakeLists.txt)"
#endif

struct pls_shim {
  pls::Chain chain;
  pls::Command* command_storage = nullptr;
  pls::BeatSlot* beat_storage = nullptr;
  pls::HeartbeatOnset* onset_storage = nullptr;
  ma_device device;
  // Control thread only.
  bool device_initialized = false;
  uint64_t runs = 0;
  uint64_t control_generation = 0;
  std::atomic<bool> started{false};
  // The number of the stream the shim expects to be playing: set once ma_device_start has
  // succeeded, 0 from the moment pls_stop begins. Read on miniaudio's thread and in pls_get_stats.
  std::atomic<uint64_t> stream_run{0};
  // The last run whose unrequested stop was counted, so a stop seen twice is counted once.
  mutable std::atomic<uint64_t> lost_run{0};
  mutable std::atomic<uint64_t> device_unrequested_stops{0};
#ifdef _WIN32
  uint64_t stream_start_frame = 0;
  std::thread clock_thread;
  std::atomic<bool> clock_thread_stop{true};
  std::atomic<bool> clock_thread_ready{false};
  std::atomic<bool> clock_thread_failed{false};
  // A sequence lock made only from always-lock-free atomics. Odd means the timing thread is
  // publishing. Even/2 is the attempt number. The callback makes at most three read attempts.
  std::atomic<uint64_t> clock_mailbox_sequence{0};
  std::atomic<bool> clock_mailbox_valid{false};
  std::atomic<int64_t> clock_mailbox_qpc_ns{0};
  std::atomic<int64_t> clock_mailbox_output_frame{0};
  std::atomic<int64_t> clock_mailbox_polled_ns{0};
#endif
};

namespace {

constexpr uint32_t kMaxBlockFrames = 1u << 16;
constexpr uint32_t kMaxCommandCapacity = 1u << 20;
constexpr uint32_t kMaxBeatCapacity = 1u << 16;
// An anchor frame far past any session (3 million years at 48 kHz), low enough that the audio
// thread's frame arithmetic can never overflow int64.
constexpr uint64_t kMaxAnchorFrame = 1ull << 62;

#ifdef _WIN32
// CD63314F-3FBA-4A1B-812C-EF96358728E7, __uuidof(IAudioClock), kept local so the DLL does not
// import a GUID symbol from another library.
constexpr IID kIidAudioClock = {0xcd63314f,
                                0x3fba,
                                0x4a1b,
                                {0x81, 0x2c, 0xef, 0x96, 0x35, 0x87, 0x28, 0xe7}};
constexpr int64_t kClockPollMs = 5;
constexpr int64_t kClockSampleMaxAgeNs = 100000000;  // 100 ms: ten nominal device periods

static_assert(std::atomic<int64_t>::is_always_lock_free,
              "clock mailbox int64 atomics must be lock-free");
static_assert(std::atomic<bool>::is_always_lock_free,
              "clock mailbox bool atomics must be lock-free");

int64_t qpc_ns(int64_t frequency) {
  LARGE_INTEGER now;
  QueryPerformanceCounter(&now);
  return (now.QuadPart / frequency) * 1000000000LL +
         (now.QuadPart % frequency) * 1000000000LL / frequency;
}

void publish_clock_attempt(pls_shim* shim, bool valid, int64_t correlated_qpc_ns,
                           int64_t output_frame) {
  // Sequentially consistent operations make this a conventional sequence lock even though each
  // payload word is a separate atomic: a reader cannot accept a mixture across the odd version.
  shim->clock_mailbox_sequence.fetch_add(1u);  // odd
  shim->clock_mailbox_valid.store(valid);
  shim->clock_mailbox_qpc_ns.store(correlated_qpc_ns);
  shim->clock_mailbox_output_frame.store(output_frame);
  shim->clock_mailbox_polled_ns.store(qpc_ns(shim->chain.qpc_frequency));
  shim->clock_mailbox_sequence.fetch_add(1u);  // even
}

void poll_clock_once(pls_shim* shim, IAudioClock* audio_clock, UINT64 audio_clock_frequency) {
  UINT64 position = 0;
  UINT64 qpc_100ns = 0;
  bool valid = audio_clock->GetPosition(&position, &qpc_100ns) == S_OK && qpc_100ns != 0 &&
               qpc_100ns <= static_cast<UINT64>(INT64_MAX / 100);
  int64_t output_frame = 0;
  if (valid) {
    // miniaudio resets IAudioClient after every clean stop; a newly opened client also starts at
    // zero. Position is therefore this run's progress in the units returned by GetFrequency.
    const long double device_frames =
        static_cast<long double>(position) * PLS_SAMPLE_RATE /
        static_cast<long double>(audio_clock_frequency);
    valid = device_frames <= static_cast<long double>(INT64_MAX - shim->stream_start_frame);
    if (valid) {
      output_frame = static_cast<int64_t>(shim->stream_start_frame) +
                     static_cast<int64_t>(std::llround(device_frames));
    }
  }
  publish_clock_attempt(shim, valid, valid ? static_cast<int64_t>(qpc_100ns * 100u) : 0,
                        output_frame);
}

void clock_thread_main(pls_shim* shim) {
  const HRESULT apartment = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
  if (FAILED(apartment)) {
    shim->clock_thread_failed.store(true, std::memory_order_release);
    shim->clock_thread_ready.store(true, std::memory_order_release);
    return;
  }
  auto* audio_client = reinterpret_cast<IAudioClient*>(shim->device.wasapi.pAudioClientPlayback);
  IAudioClock* audio_clock = nullptr;
  UINT64 audio_clock_frequency = 0;
  const bool acquired =
      audio_client != nullptr &&
      SUCCEEDED(audio_client->GetService(kIidAudioClock,
                                         reinterpret_cast<void**>(&audio_clock))) &&
      audio_clock != nullptr && SUCCEEDED(audio_clock->GetFrequency(&audio_clock_frequency)) &&
      audio_clock_frequency != 0;
  if (!acquired) {
    if (audio_clock != nullptr) {
      audio_clock->Release();
    }
    shim->clock_thread_failed.store(true, std::memory_order_release);
    shim->clock_thread_ready.store(true, std::memory_order_release);
    CoUninitialize();
    return;
  }
  // GetService, every position read and Release stay on this non-audio COM thread. Microsoft
  // specifically requires an IAudioClock to be released on the thread that obtained it.
  poll_clock_once(shim, audio_clock, audio_clock_frequency);
  shim->clock_thread_ready.store(true, std::memory_order_release);
  while (!shim->clock_thread_stop.load(std::memory_order_acquire)) {
    std::this_thread::sleep_for(std::chrono::milliseconds(kClockPollMs));
    if (!shim->clock_thread_stop.load(std::memory_order_acquire)) {
      poll_clock_once(shim, audio_clock, audio_clock_frequency);
    }
  }
  audio_clock->Release();
  CoUninitialize();
}

bool start_clock_thread(pls_shim* shim) {
  shim->clock_thread_stop.store(false, std::memory_order_release);
  shim->clock_thread_ready.store(false, std::memory_order_relaxed);
  shim->clock_thread_failed.store(false, std::memory_order_relaxed);
  try {
    shim->clock_thread = std::thread(clock_thread_main, shim);
  } catch (...) {
    shim->clock_thread_stop.store(true, std::memory_order_release);
    return false;
  }
  while (!shim->clock_thread_ready.load(std::memory_order_acquire)) {
    std::this_thread::yield();
  }
  if (shim->clock_thread_failed.load(std::memory_order_acquire)) {
    shim->clock_thread.join();
    shim->clock_thread_stop.store(true, std::memory_order_release);
    return false;
  }
  return true;
}

void stop_clock_thread(pls_shim* shim) {
  shim->clock_thread_stop.store(true, std::memory_order_release);
  if (shim->clock_thread.joinable()) {
    shim->clock_thread.join();
  }
}

pls::DeviceClockSample read_clock_mailbox(pls_shim* shim) {
  pls::DeviceClockSample sample;
  for (int tries = 0; tries < 3; ++tries) {
    const uint64_t before = shim->clock_mailbox_sequence.load();
    if (before == 0 || (before & 1u) != 0) {
      continue;
    }
    const bool valid = shim->clock_mailbox_valid.load();
    const int64_t correlated_qpc_ns = shim->clock_mailbox_qpc_ns.load();
    const int64_t output_frame = shim->clock_mailbox_output_frame.load();
    const int64_t polled_ns = shim->clock_mailbox_polled_ns.load();
    const uint64_t after = shim->clock_mailbox_sequence.load();
    if (before != after || (after & 1u) != 0) {
      continue;
    }
    const int64_t age_ns = qpc_ns(shim->chain.qpc_frequency) - polled_ns;
    sample.present = true;
    sample.valid = valid && age_ns >= 0 && age_ns <= kClockSampleMaxAgeNs;
    sample.attempt = after / 2u;
    sample.qpc_ns = correlated_qpc_ns;
    sample.output_frame = output_frame;
    return sample;
  }
  return sample;
}
#endif

void free_shim(pls_shim* shim) {
  if (shim == nullptr) {
    return;
  }
  delete[] shim->chain.engine_buffer;
  delete[] shim->chain.mix_buffer;
  delete[] shim->chain.voice_buffer;
  delete[] shim->chain.device_buffer;
  delete[] shim->command_storage;
  delete[] shim->beat_storage;
  delete[] shim->onset_storage;
  delete shim;
}

int32_t push(pls_shim* shim, const pls::Command& command) {
  return shim->chain.commands.try_push(command) ? PLS_OK : PLS_ERROR_QUEUE_FULL;
}

void on_device_data(ma_device* device, void* output, const void* input, ma_uint32 frames) {
  (void)input;
  auto* shim = static_cast<pls_shim*>(device->pUserData);
  pls::DeviceClockSample sample;
#ifdef _WIN32
  sample = read_clock_mailbox(shim);
#endif
  pls::render_device(shim->chain, static_cast<float*>(output), device->playback.channels, frames,
                     &sample);
}

// Any thread. Counts the unrequested stop of stream `run` once, however often it is seen.
void count_unrequested_stop(const pls_shim* shim, uint64_t run) {
  uint64_t counted = shim->lost_run.load();
  while (counted < run) {
    if (shim->lost_run.compare_exchange_weak(counted, run)) {
      shim->device_unrequested_stops.fetch_add(1);
      return;
    }
  }
}

// miniaudio's device thread. A stop while the shim still expects the stream is one pls_stop did not
// ask for: the endpoint went away, or stalled. Atomics only.
void on_device_notification(const ma_device_notification* notification) {
  if (notification->type != ma_device_notification_type_stopped) {
    return;
  }
  const auto* shim = static_cast<const pls_shim*>(notification->pDevice->pUserData);
  const uint64_t run = shim->stream_run.load();
  if (run != 0) {
    count_unrequested_stop(shim, run);
  }
}

// Any thread. miniaudio posts no stopped notification when its own stop of the stream fails, which
// is what an unplugged endpoint does, so the device's state is read too. The run is read again
// after it: a stream pls_stop or pls_start changed in between is not counted.
void check_stream(const pls_shim* shim) {
  const uint64_t run = shim->stream_run.load();
  if (run == 0 || ma_device_get_state(&shim->device) != ma_device_state_stopped) {
    return;
  }
  if (shim->stream_run.load() == run) {
    count_unrequested_stop(shim, run);
  }
}

// Control thread. Opens the default output device, refusing one that would resample.
int32_t open_device(pls_shim* shim) {
  ma_device_config config = ma_device_config_init(ma_device_type_playback);
  config.playback.format = ma_format_f32;
  config.playback.channels = 0;  // the device's own
  config.sampleRate = PLS_SAMPLE_RATE;
  config.periodSizeInMilliseconds = 10;
  config.dataCallback = on_device_data;
  config.notificationCallback = on_device_notification;
  config.pUserData = shim;
  config.noPreSilencedOutputBuffer = MA_TRUE;
  config.noClip = MA_TRUE;
  // Without this, WASAPI shared mode converts 48 kHz to the mix rate itself and miniaudio reports
  // 48 kHz regardless. With it, the internal rate is the endpoint's, and anything but 48 kHz is
  // refused below: no resampling anywhere.
  config.wasapi.noAutoConvertSRC = MA_TRUE;
  // Never follow a default-device change. miniaudio would reopen the stream on the new endpoint by
  // itself, through its resampler if that endpoint is not at 48 kHz, with the period and the time
  // anchor left stale. The stream stays on the endpoint that passed the check below; if that
  // endpoint goes away, the stream stops, and the next pls_start opens the device afresh.
  config.wasapi.noAutoStreamRouting = MA_TRUE;
  if (ma_device_init(nullptr, &config, &shim->device) != MA_SUCCESS) {
    return PLS_ERROR_DEVICE;
  }
  if (shim->device.playback.internalSampleRate != PLS_SAMPLE_RATE) {
    ma_device_uninit(&shim->device);
    return PLS_ERROR_SAMPLE_RATE;
  }
  shim->chain.device_period_frames = shim->device.playback.internalPeriodSizeInFrames;
  shim->device_initialized = true;
  return PLS_OK;
}

// Control thread, with the stream stopped.
void close_device(pls_shim* shim) {
  if (shim->device_initialized) {
#ifdef _WIN32
    stop_clock_thread(shim);
#endif
    // The timing thread has released its IAudioClock. uninit now quiesces/joins the callback and
    // releases miniaudio's audio client, including on failed-stop and unexpected-state paths.
    ma_device_uninit(&shim->device);
    shim->device_initialized = false;
  }
}

}  // namespace

pls::Chain& pls::chain_of(pls_shim* shim) { return shim->chain; }

int32_t pls_abi_version(void) { return PLS_ABI_VERSION; }

const char* pls_source_hash(void) { return PLS_SOURCE_HASH; }

uint32_t pls_latency_frames(void) { return pls::kLatency; }

pls_config pls_config_default(void) {
  pls_config config;
  config.sample_rate = PLS_SAMPLE_RATE;
  config.max_block_frames = 2048;
  config.command_capacity = 256;
  config.beat_capacity = 64;
  config.engine_trim_db = -6.0;
  return config;
}

int32_t pls_open(const pls_config* config, pls_render_fn render, void* core,
                 pls_shim** out_shim) {
  if (out_shim == nullptr) {
    return PLS_ERROR_INVALID_ARGUMENT;
  }
  *out_shim = nullptr;
  if (config == nullptr || render == nullptr) {
    return PLS_ERROR_INVALID_ARGUMENT;
  }
  if (config->sample_rate != PLS_SAMPLE_RATE) {
    return PLS_ERROR_SAMPLE_RATE;
  }
  const uint32_t capacity = config->command_capacity;
  if (config->max_block_frames == 0 || config->max_block_frames > kMaxBlockFrames ||
      capacity == 0 || capacity > kMaxCommandCapacity || (capacity & (capacity - 1u)) != 0 ||
      config->beat_capacity == 0 || config->beat_capacity > kMaxBeatCapacity ||
      !std::isfinite(config->engine_trim_db) || config->engine_trim_db > 0.0) {
    return PLS_ERROR_INVALID_ARGUMENT;
  }

  pls_shim* shim = new (std::nothrow) pls_shim();
  if (shim == nullptr) {
    return PLS_ERROR_OUT_OF_MEMORY;
  }
  const uint32_t block = config->max_block_frames;
  pls::Chain& chain = shim->chain;
  chain.engine_buffer = new (std::nothrow) float[block]();
  chain.mix_buffer = new (std::nothrow) double[block]();
  chain.voice_buffer = new (std::nothrow) double[block]();
  chain.device_buffer = new (std::nothrow) float[block]();
  shim->command_storage = new (std::nothrow) pls::Command[capacity]();
  shim->beat_storage = new (std::nothrow) pls::BeatSlot[config->beat_capacity]();
  shim->onset_storage = new (std::nothrow) pls::HeartbeatOnset[capacity]();
  if (chain.engine_buffer == nullptr || chain.mix_buffer == nullptr ||
      chain.voice_buffer == nullptr || chain.device_buffer == nullptr ||
      shim->command_storage == nullptr || shim->beat_storage == nullptr ||
      shim->onset_storage == nullptr) {
    free_shim(shim);
    return PLS_ERROR_OUT_OF_MEMORY;
  }

  chain.render = render;
  chain.core = core;
  chain.max_block_frames = block;
  chain.engine_trim = std::pow(10.0, config->engine_trim_db / 20.0);
#ifdef _WIN32
  LARGE_INTEGER frequency;
  QueryPerformanceFrequency(&frequency);
  chain.qpc_frequency = frequency.QuadPart;
#else
  chain.qpc_frequency = 1;
#endif
  chain.commands.attach(shim->command_storage, capacity);
  chain.onset_telemetry.attach(shim->onset_storage, capacity);
  chain.highpass.reset();
  chain.session_gain.reset(0.0);
  chain.heartbeat_level.reset_db(-INFINITY);
  chain.heartbeat.attach(shim->beat_storage, config->beat_capacity, PLS_SAMPLE_RATE);
  chain.limiter.init(PLS_SAMPLE_RATE, pls::kTargetDbtp);

  *out_shim = shim;
  return PLS_OK;
}

int32_t pls_start(pls_shim* shim) {
  if (shim == nullptr) {
    return PLS_ERROR_INVALID_ARGUMENT;
  }
  if (shim->started.load()) {
    return PLS_ERROR_INVALID_STATE;
  }
  // A device kept from an earlier run is trusted only if it is plainly stopped.
  if (shim->device_initialized && ma_device_get_state(&shim->device) != ma_device_state_stopped) {
    close_device(shim);
  }
  // A kept device whose endpoint went away while stopped fails to start: it is closed and the
  // default device opened afresh, once, through the 48 kHz check again.
  bool kept = shim->device_initialized;
  for (;;) {
    if (!shim->device_initialized) {
      const int32_t opened = open_device(shim);
      if (opened != PLS_OK) {
        return opened;
      }
    }
    // The callback can run before ma_device_start returns. Invalidate the last run's mailbox and
    // record this run's lifetime start frame first; early callbacks use the documented QPC+period
    // fallback until the timing thread publishes its first IAudioClock reading.
#ifdef _WIN32
    shim->stream_start_frame = shim->chain.counters.frames_rendered.load(std::memory_order_relaxed);
    publish_clock_attempt(shim, false, 0, 0);
#endif
    shim->started.store(true);
    if (ma_device_start(&shim->device) == MA_SUCCESS) {
      shim->stream_run.store(++shim->runs);
#ifdef _WIN32
      if (!start_clock_thread(shim)) {
        shim->stream_run.store(0);
        (void)ma_device_stop(&shim->device);
        shim->started.store(false);
        close_device(shim);
        return PLS_ERROR_DEVICE;
      }
#endif
      return PLS_OK;
    }
    shim->started.store(false);
    close_device(shim);
    if (!kept) {
      return PLS_ERROR_DEVICE;
    }
    kept = false;
  }
}

int32_t pls_stop(pls_shim* shim) {
  if (shim == nullptr) {
    return PLS_ERROR_INVALID_ARGUMENT;
  }
  if (!shim->started.load()) {
    return PLS_OK;
  }
  check_stream(shim);
  shim->stream_run.store(0);
  if (ma_device_get_state(&shim->device) == ma_device_state_stopped) {
    // miniaudio already stopped the stream on its own (counted above). Release the device, so the
    // next pls_start opens it afresh and checks it again.
    close_device(shim);
    shim->started.store(false);
    return PLS_OK;
  }
  // ma_device_stop returns once the callback has finished its last run.
  const ma_result result = ma_device_stop(&shim->device);
#ifdef _WIN32
  stop_clock_thread(shim);
#endif
  shim->started.store(false);
  return result == MA_SUCCESS ? PLS_OK : PLS_ERROR_DEVICE;
}

void pls_close(pls_shim* shim) {
  if (shim == nullptr) {
    return;
  }
  pls_stop(shim);
  close_device(shim);
  free_shim(shim);
}

int32_t pls_set_session_gain(pls_shim* shim, double target, double ramp_ms) {
  if (shim == nullptr || !(target >= 0.0 && target <= 1.0) ||
      !(ramp_ms >= 0.0 && std::isfinite(ramp_ms))) {
    return PLS_ERROR_INVALID_ARGUMENT;
  }
  pls::Command command{};
  command.kind = pls::CommandKind::kSessionGain;
  command.value = target;
  command.ms = ramp_ms;
  return push(shim, command);
}

int32_t pls_set_heartbeat_level(pls_shim* shim, double target_dbfs, double ramp_ms) {
  if (shim == nullptr || std::isnan(target_dbfs) || target_dbfs > 0.0 ||
      !(ramp_ms >= 0.0 && std::isfinite(ramp_ms))) {
    return PLS_ERROR_INVALID_ARGUMENT;
  }
  pls::Command command{};
  command.kind = pls::CommandKind::kHeartbeatLevel;
  command.value = target_dbfs;
  command.ms = ramp_ms;
  return push(shim, command);
}

int32_t pls_push_beat(pls_shim* shim, double t_play_ms, double rr_ms, int32_t quality) {
  if (shim == nullptr || !std::isfinite(t_play_ms) || !(rr_ms >= 250.0 && rr_ms <= 2500.0) ||
      (quality != PLS_BEAT_OK && quality != PLS_BEAT_INTERPOLATED)) {
    return PLS_ERROR_INVALID_ARGUMENT;
  }
  pls::Command command{};
  command.kind = pls::CommandKind::kBeat;
  command.quality = quality;
  command.value = t_play_ms;
  command.ms = rr_ms;
  command.generation = shim->control_generation;
  return push(shim, command);
}

int32_t pls_set_time_origin_ns(pls_shim* shim, int64_t perf_counter_origin_ns) {
  // perf_counter_ns() is never negative on Windows, and a non-negative origin keeps the audio
  // thread's subtraction from overflowing.
  if (shim == nullptr || perf_counter_origin_ns < 0) {
    return PLS_ERROR_INVALID_ARGUMENT;
  }
  pls::Command command{};
  command.kind = pls::CommandKind::kTimeOrigin;
  command.origin_ns = perf_counter_origin_ns;
  command.generation = shim->control_generation + 1u;
  const int32_t result = push(shim, command);
  if (result == PLS_OK) {
    shim->control_generation = command.generation;
  }
  return result;
}

int32_t pls_set_clock_anchor(pls_shim* shim, double t_engine_ms, uint64_t output_frame) {
  if (shim == nullptr || !std::isfinite(t_engine_ms) || output_frame > kMaxAnchorFrame) {
    return PLS_ERROR_INVALID_ARGUMENT;
  }
  pls::Command command{};
  command.kind = pls::CommandKind::kClockAnchor;
  command.value = t_engine_ms;
  command.frame = output_frame;
  return push(shim, command);
}

uint64_t pls_frames_rendered(const pls_shim* shim) {
  if (shim == nullptr) {
    return 0;
  }
  return shim->chain.counters.frames_rendered.load(std::memory_order_relaxed);
}

int32_t pls_get_stats(const pls_shim* shim, pls_stats* out_stats) {
  if (shim == nullptr || out_stats == nullptr) {
    return PLS_ERROR_INVALID_ARGUMENT;
  }
  check_stream(shim);
  const pls::Counters& counters = shim->chain.counters;
  out_stats->frames_rendered = counters.frames_rendered.load(std::memory_order_relaxed);
  out_stats->render_errors = counters.render_errors.load(std::memory_order_relaxed);
  out_stats->beats_played = counters.beats_played.load(std::memory_order_relaxed);
  out_stats->beats_dropped_late = counters.beats_dropped_late.load(std::memory_order_relaxed);
  out_stats->beats_dropped_full = counters.beats_dropped_full.load(std::memory_order_relaxed);
  out_stats->limiter_active_frames =
      counters.limiter_active_frames.load(std::memory_order_relaxed);
  out_stats->limiter_min_gain = counters.limiter_min_gain.load(std::memory_order_relaxed);
  out_stats->device_unrequested_stops = shim->device_unrequested_stops.load();
  out_stats->device_clock_samples = counters.device_clock_samples.load(std::memory_order_acquire);
  out_stats->device_clock_failures =
      counters.device_clock_failures.load(std::memory_order_relaxed);
  out_stats->heartbeat_onset_measurements =
      counters.heartbeat_onset_measurements.load(std::memory_order_acquire);
  out_stats->device_anchor_error_ms =
      counters.device_anchor_error_ms.load(std::memory_order_relaxed);
  out_stats->device_anchor_slew_ms =
      counters.device_anchor_slew_ms.load(std::memory_order_relaxed);
  out_stats->heartbeat_onset_error_last_ms =
      counters.heartbeat_onset_error_last_ms.load(std::memory_order_relaxed);
  out_stats->heartbeat_onset_error_abs_max_ms =
      counters.heartbeat_onset_error_abs_max_ms.load(std::memory_order_relaxed);
  out_stats->heartbeat_onset_telemetry_dropped =
      counters.heartbeat_onset_telemetry_dropped.load(std::memory_order_relaxed);
  return PLS_OK;
}

uint32_t pls_drain_heartbeat_onsets(pls_shim* shim, pls_heartbeat_onset* out_measurements,
                                   uint32_t capacity) {
  if (shim == nullptr || out_measurements == nullptr || capacity == 0) {
    return 0;
  }
  uint32_t count = 0;
  pls::HeartbeatOnset measurement{};
  while (count < capacity && shim->chain.onset_telemetry.try_pop(measurement)) {
    out_measurements[count].t_play_ms = measurement.t_play_ms;
    out_measurements[count].error_ms = measurement.error_ms;
    ++count;
  }
  return count;
}

int32_t pls_render_offline(pls_shim* shim, float* out_frames, float* engine_tap,
                           uint32_t frame_count) {
  if (shim == nullptr || out_frames == nullptr) {
    return PLS_ERROR_INVALID_ARGUMENT;
  }
  if (shim->started.load()) {
    return PLS_ERROR_INVALID_STATE;
  }
  pls::render_offline(shim->chain, out_frames, engine_tap, frame_count);
  return PLS_OK;
}
