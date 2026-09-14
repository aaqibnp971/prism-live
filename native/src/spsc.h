// spsc.h: the command queue between the control thread and the audio thread.
//
// Wait-free, single producer and single consumer. The control thread is the only producer (every
// pls_set_* and pls_push_beat call), the audio thread the only consumer. The storage is handed in,
// sized at pls_open, so pushing and popping never touch the heap. Each index is written by one
// side only; the count is head - tail, so every one of the `capacity` slots is usable.

#ifndef PLS_SPSC_H
#define PLS_SPSC_H

#include <atomic>
#include <cstdint>
#include <type_traits>

namespace pls {

template <typename T>
class SpscQueue {
  // Items are copied in and out as plain bytes, and the indices must never take a lock.
  static constexpr bool kTriviallyCopyable = std::is_trivially_copyable<T>::value;
  static constexpr bool kLockFree = std::atomic<uint64_t>::is_always_lock_free;

 public:
  // Control thread, before the audio thread exists. capacity is a power of two.
  void attach(T* storage, uint32_t capacity) {
    static_assert(kTriviallyCopyable, "queue items must be trivially copyable");
    static_assert(kLockFree, "std::atomic<uint64_t> must be lock-free");
    storage_ = storage;
    capacity_ = capacity;
    mask_ = capacity - 1u;
    head_.store(0, std::memory_order_relaxed);
    tail_.store(0, std::memory_order_relaxed);
  }

  // Producer. False, and nothing queued, when full.
  bool try_push(const T& item) {
    const uint64_t head = head_.load(std::memory_order_relaxed);
    const uint64_t tail = tail_.load(std::memory_order_acquire);
    if (head - tail >= capacity_) {
      return false;
    }
    storage_[head & mask_] = item;
    head_.store(head + 1u, std::memory_order_release);
    return true;
  }

  // Consumer. False when empty.
  bool try_pop(T& out) {
    const uint64_t tail = tail_.load(std::memory_order_relaxed);
    const uint64_t head = head_.load(std::memory_order_acquire);
    if (head == tail) {
      return false;
    }
    out = storage_[tail & mask_];
    tail_.store(tail + 1u, std::memory_order_release);
    return true;
  }

 private:
  std::atomic<uint64_t> head_{0};
  std::atomic<uint64_t> tail_{0};
  T* storage_ = nullptr;
  uint64_t capacity_ = 0;
  uint64_t mask_ = 0;
};

}  // namespace pls

#endif  // PLS_SPSC_H
