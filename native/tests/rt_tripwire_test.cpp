// rt_tripwire_test.cpp: nothing on the audio thread allocates (CLAUDE.md hard rule 4, prompt 2.5).
//
// Links the shim's own objects and counts every heap allocation while armed: global operator new
// and delete (plain, nothrow and aligned) are replaced here, and malloc, calloc, realloc, free and
// the CRT's _aligned_malloc family are wrapped at link time (-Wl,--wrap). Armed around thousands
// of pls_render_offline blocks, and the device callback's path (pls::render_device, stereo and
// 5.1, no device opened), while a producer thread plays the control thread, pushing beats, gains,
// levels, anchors and time origins through the public ABI, and while the render function feeds
// the chain signals loud enough to keep the limiter engaged. Zero allocations from either thread,
// or the test fails.
//
// What it cannot see: allocations made inside a system DLL (ucrtbase, kernel32) on the shim's
// behalf. The audio path calls none that allocate (math functions and QueryPerformanceCounter
// only); tests/test_shim.py scans the sources for the calls that would.
//
// A self-check first proves the counters see malloc and operator new at all, so a wiring mistake
// cannot pass as a clean run. The first line printed is `source <pls_source_hash()>`.

#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <new>
#include <thread>

#include "prism_live_shim.h"
#include "process.h"

extern "C" {
void* __real_malloc(size_t size);
void* __real_calloc(size_t count, size_t size);
void* __real_realloc(void* pointer, size_t size);
void __real_free(void* pointer);
void* __real__aligned_malloc(size_t size, size_t alignment);
void* __real__aligned_realloc(void* pointer, size_t size, size_t alignment);
void __real__aligned_free(void* pointer);
}

namespace {

std::atomic<bool> g_armed{false};
std::atomic<uint64_t> g_allocations{0};
std::atomic<uint64_t> g_frees{0};

inline void note_allocation() {
  if (g_armed.load(std::memory_order_relaxed)) {
    g_allocations.fetch_add(1, std::memory_order_relaxed);
  }
}

inline void note_free(void* pointer) {
  if (pointer != nullptr && g_armed.load(std::memory_order_relaxed)) {
    g_frees.fetch_add(1, std::memory_order_relaxed);
  }
}

}  // namespace

extern "C" {

void* __wrap_malloc(size_t size) {
  note_allocation();
  return __real_malloc(size);
}

void* __wrap_calloc(size_t count, size_t size) {
  note_allocation();
  return __real_calloc(count, size);
}

void* __wrap_realloc(void* pointer, size_t size) {
  note_allocation();
  return __real_realloc(pointer, size);
}

void __wrap_free(void* pointer) {
  note_free(pointer);
  __real_free(pointer);
}

void* __wrap__aligned_malloc(size_t size, size_t alignment) {
  note_allocation();
  return __real__aligned_malloc(size, alignment);
}

void* __wrap__aligned_realloc(void* pointer, size_t size, size_t alignment) {
  note_allocation();
  return __real__aligned_realloc(pointer, size, alignment);
}

void __wrap__aligned_free(void* pointer) {
  note_free(pointer);
  __real__aligned_free(pointer);
}

}  // extern "C"

void* operator new(size_t size) {
  note_allocation();
  void* p = __real_malloc(size == 0 ? 1 : size);
  if (p == nullptr) {
    throw std::bad_alloc();
  }
  return p;
}

void* operator new[](size_t size) { return operator new(size); }

void* operator new(size_t size, const std::nothrow_t&) noexcept {
  note_allocation();
  return __real_malloc(size == 0 ? 1 : size);
}

void* operator new[](size_t size, const std::nothrow_t& tag) noexcept {
  return operator new(size, tag);
}

void operator delete(void* pointer) noexcept {
  note_free(pointer);
  __real_free(pointer);
}

void operator delete[](void* pointer) noexcept { operator delete(pointer); }
void operator delete(void* pointer, size_t) noexcept { operator delete(pointer); }
void operator delete[](void* pointer, size_t) noexcept { operator delete(pointer); }
void operator delete(void* pointer, const std::nothrow_t&) noexcept { operator delete(pointer); }
void operator delete[](void* pointer, const std::nothrow_t&) noexcept {
  operator delete(pointer);
}

void* operator new(size_t size, std::align_val_t alignment) {
  note_allocation();
  void* p = __real__aligned_malloc(size == 0 ? 1 : size, static_cast<size_t>(alignment));
  if (p == nullptr) {
    throw std::bad_alloc();
  }
  return p;
}

void* operator new[](size_t size, std::align_val_t alignment) {
  return operator new(size, alignment);
}

void* operator new(size_t size, std::align_val_t alignment, const std::nothrow_t&) noexcept {
  note_allocation();
  return __real__aligned_malloc(size == 0 ? 1 : size, static_cast<size_t>(alignment));
}

void* operator new[](size_t size, std::align_val_t alignment, const std::nothrow_t& tag) noexcept {
  return operator new(size, alignment, tag);
}

void operator delete(void* pointer, std::align_val_t) noexcept {
  note_free(pointer);
  __real__aligned_free(pointer);
}

void operator delete[](void* pointer, std::align_val_t alignment) noexcept {
  operator delete(pointer, alignment);
}
void operator delete(void* pointer, size_t, std::align_val_t alignment) noexcept {
  operator delete(pointer, alignment);
}
void operator delete[](void* pointer, size_t, std::align_val_t alignment) noexcept {
  operator delete(pointer, alignment);
}
void operator delete(void* pointer, std::align_val_t alignment, const std::nothrow_t&) noexcept {
  operator delete(pointer, alignment);
}
void operator delete[](void* pointer, std::align_val_t alignment,
                       const std::nothrow_t&) noexcept {
  operator delete(pointer, alignment);
}

namespace {

int g_failures = 0;

void expect(bool condition, const char* what) {
  if (!condition) {
    std::printf("FAIL: %s\n", what);
    ++g_failures;
  }
}

// A render function that keeps the limiter busy: +6 dBFS noise, full-scale squares, a phased
// fs/4 sine and isolated impulses, cycling every half second. No state but a counter and a PRNG.
struct LoudSource {
  uint64_t frame = 0;
  uint64_t rng = 0x9E3779B97F4A7C15ull;
};

int32_t loud_render(void* core, float* out, uint32_t frames) {
  auto* source = static_cast<LoudSource*>(core);
  for (uint32_t i = 0; i < frames; ++i) {
    const uint64_t f = source->frame++;
    const uint64_t section = (f / 24000u) % 4u;
    float value = 0.0f;
    if (section == 0) {
      source->rng ^= source->rng << 13;
      source->rng ^= source->rng >> 7;
      source->rng ^= source->rng << 17;
      value = static_cast<float>((static_cast<double>(source->rng >> 11) / 9007199254740992.0) * 4.0 -
                                 2.0);
    } else if (section == 1) {
      value = ((f / 37u) % 2u) == 0 ? 1.0f : -1.0f;
    } else if (section == 2) {
      value = static_cast<float>(2.0 * std::sin(1.5707963267948966 * static_cast<double>(f) +
                                                0.7853981633974483));
    } else {
      value = (f % 997u) == 0 ? 4.0f : 0.0f;
    }
    out[i] = value;
  }
  // Every 100th call fails, to exercise the render-error path.
  return (source->frame / 480u) % 100u == 7u ? 1 : 0;
}

struct Producer {
  pls_shim* shim = nullptr;
  std::atomic<bool> ready{false};
  std::atomic<bool> stop{false};
  std::atomic<uint64_t> pushed{0};
  std::atomic<uint64_t> refused{0};
};

void produce(Producer* producer) {
  pls_shim* shim = producer->shim;
  producer->ready.store(true);
  uint64_t n = 0;
  uint64_t last_frames = 0;
  while (!producer->stop.load(std::memory_order_relaxed)) {
    const uint64_t frames = pls_frames_rendered(shim);
    if (frames == last_frames) {
      std::this_thread::yield();
      continue;
    }
    last_frames = frames;
    const double now_ms = static_cast<double>(frames) / 48.0;
    int32_t results[6];
    results[0] = pls_push_beat(shim, now_ms + 350.0, 400.0 + static_cast<double>(n % 7) * 50.0,
                               static_cast<int32_t>(n % 2));
    results[1] = pls_push_beat(shim, now_ms - 50.0, 800.0, PLS_BEAT_OK);  // late
    results[2] = pls_set_session_gain(shim, (n % 3) == 0 ? 1.0 : 0.25, static_cast<double>(n % 50));
    results[3] = pls_set_heartbeat_level(shim, (n % 4) == 0 ? -INFINITY : -9.0,
                                         static_cast<double>(n % 30));
    results[4] = (n % 64) == 0 ? pls_set_clock_anchor(shim, now_ms, frames) : PLS_OK;
    results[5] = (n % 97) == 0 ? pls_set_time_origin_ns(shim, 0) : PLS_OK;
    for (int32_t r : results) {
      if (r == PLS_OK) {
        producer->pushed.fetch_add(1, std::memory_order_relaxed);
      } else {
        producer->refused.fetch_add(1, std::memory_order_relaxed);
      }
    }
    // Rejected on the control thread, never queued.
    (void)pls_push_beat(shim, now_ms + 400.0, 100.0, PLS_BEAT_OK);
    pls_stats stats;
    (void)pls_get_stats(shim, &stats);
    ++n;
  }
}

}  // namespace

int main() {
  // First line, for tests/test_shim.py: the sources this executable was built from, so a stale
  // build fails there instead of passing for code it never ran.
  std::printf("source %s\n", pls_source_hash());

  // Self-check: the counters see both routes to the heap. Called through volatile pointers so the
  // compiler cannot remove the pairs.
  {
    void* (*volatile malloc_fn)(size_t) = std::malloc;
    void (*volatile free_fn)(void*) = std::free;
    g_armed.store(true);
    void* a = malloc_fn(64);
    free_fn(a);
    int* b = new int(7);
    volatile int keep = *b;
    (void)keep;
    delete b;
    struct Wide {
      alignas(64) double value;
    };
    Wide* c = new Wide{1.0};
    volatile double keep_wide = c->value;
    (void)keep_wide;
    delete c;
    g_armed.store(false);
    expect(g_allocations.load() >= 3,
           "self-check: the tripwire sees malloc, operator new and aligned operator new");
    expect(g_frees.load() >= 3,
           "self-check: the tripwire sees free, operator delete and aligned operator delete");
    std::printf("self-check: %llu allocations, %llu frees seen while armed\n",
                static_cast<unsigned long long>(g_allocations.load()),
                static_cast<unsigned long long>(g_frees.load()));
    g_allocations.store(0);
    g_frees.store(0);
  }

  LoudSource source;
  pls_config config = pls_config_default();
  config.engine_trim_db = 0.0;
  config.max_block_frames = 512;
  config.command_capacity = 64;
  config.beat_capacity = 8;
  pls_shim* shim = nullptr;
  if (pls_open(&config, loud_render, &source, &shim) != PLS_OK) {
    std::printf("FAIL: pls_open\n");
    return 1;
  }
  (void)pls_set_clock_anchor(shim, 0.0, 0);
  (void)pls_set_session_gain(shim, 1.0, 0.0);
  (void)pls_set_heartbeat_level(shim, -9.0, 0.0);

  static float out[4096];
  static float tap[4096];
  static float interleaved[4096 * 6];
  pls::Chain& chain = pls::chain_of(shim);
  // As pls_start leaves it after the device reports its period.
  chain.device_period_frames = 480;

  Producer producer;
  producer.shim = shim;
  std::thread thread(produce, &producer);
  while (!producer.ready.load()) {
    std::this_thread::yield();
  }

  // Block sizes vary, some above max_block_frames so the split path runs too.
  constexpr uint32_t kSizes[] = {480, 441, 512, 1024, 97, 2048, 1};
  constexpr int kBlocks = 6000;
  uint64_t frames = 0;
  int render_results = 0;
  uint64_t device_calls = 0;
  g_armed.store(true);
  for (int b = 0; b < kBlocks; ++b) {
    const uint32_t size = kSizes[b % 7];
    if (b % 5 == 4) {
      // The device callback's path, as miniaudio would call it, on the same audio thread.
      pls::render_device(chain, interleaved, (b % 2) == 0 ? 2u : 6u, size);
      ++device_calls;
    } else {
      render_results |= pls_render_offline(shim, out, (b % 3) == 0 ? tap : nullptr, size);
    }
    frames += size;
  }
  g_armed.store(false);
  const uint64_t allocations = g_allocations.load();
  const uint64_t frees = g_frees.load();

  producer.stop.store(true);
  thread.join();

  pls_stats stats;
  (void)pls_get_stats(shim, &stats);
  std::printf("rendered %llu frames in %d blocks (%.1f s), %llu through the device path\n",
              static_cast<unsigned long long>(frames), kBlocks,
              static_cast<double>(frames) / 48000.0,
              static_cast<unsigned long long>(device_calls));
  std::printf("producer: %llu commands queued, %llu refused (queue full)\n",
              static_cast<unsigned long long>(producer.pushed.load()),
              static_cast<unsigned long long>(producer.refused.load()));
  std::printf("stats: render_errors %llu, beats_played %llu, dropped_late %llu, dropped_full %llu, "
              "limiter_active_frames %llu, limiter_min_gain %.4f\n",
              static_cast<unsigned long long>(stats.render_errors),
              static_cast<unsigned long long>(stats.beats_played),
              static_cast<unsigned long long>(stats.beats_dropped_late),
              static_cast<unsigned long long>(stats.beats_dropped_full),
              static_cast<unsigned long long>(stats.limiter_active_frames), stats.limiter_min_gain);
  std::printf("armed: %llu allocations, %llu frees\n", static_cast<unsigned long long>(allocations),
              static_cast<unsigned long long>(frees));

  expect(render_results == PLS_OK, "pls_render_offline returned PLS_OK throughout");
  expect(stats.frames_rendered == frames, "frames_rendered equals the frames asked for");
  expect(producer.pushed.load() > 1000, "the producer queued commands while armed");
  expect(stats.beats_played > 10, "beats played while armed");
  expect(stats.beats_dropped_late > 10, "late beats were dropped while armed");
  expect(stats.render_errors > 0, "the render-error path ran");
  expect(stats.limiter_min_gain < 0.5, "the limiter was engaged");
  expect(allocations == 0, "no allocation while armed");
  expect(frees == 0, "no free while armed");

  pls_close(shim);
  if (g_failures != 0) {
    std::printf("%d failure(s)\n", g_failures);
    return 1;
  }
  std::printf("PASS\n");
  return 0;
}
