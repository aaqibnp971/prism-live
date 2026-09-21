// The client side of the clock exchange (docs/message-contract-v1.md §2, `clock`).
//
// This is the build-free implementation used by classic browser pages. clock_sync.js is the
// ES-module wrapper around this same object, so both forms execute one copy of the arithmetic.

(function exposeClockSync(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.PrismClockSync = api;
})(typeof globalThis === "undefined" ? this : globalThis, function buildClockSync() {
  "use strict";

  const WINDOW = 9;
  const PING_INTERVAL_MS = 2000;
  const RTT_FLOOR_MS = 2;
  const MAX_RTT_MS = 5000;

  class ClockSync {
    #offsets = [];
    #rtts = [];

    ping(tClientNow) {
      return { type: "clock", v: 1, role: "ping", t_client_sent: tClientNow };
    }

    // Take one pong, received at tClientNow on this client's clock. True if it was used.
    onPong(pong, tClientNow) {
      const rtt = tClientNow - pong.t_client_sent;
      if (!(rtt >= 0 && rtt <= MAX_RTT_MS)) return false;
      const limit = this.#rtts.length
        ? 2 * Math.max(median(this.#rtts), RTT_FLOOR_MS)
        : Infinity;
      keepLast(this.#rtts, rtt);
      if (rtt > limit) return false;
      keepLast(this.#offsets, pong.t_engine + rtt / 2 - tClientNow);
      return true;
    }

    get ready() {
      return this.#offsets.length > 0;
    }

    get samples() {
      return this.#offsets.length;
    }

    // T_engine minus this client's clock, in ms. null until the first pong is used.
    get offset() {
      return this.#offsets.length ? median(this.#offsets) : null;
    }

    // A laptop time, such as t_play, on this client's clock.
    toLocal(tEngine) {
      return tEngine - this.#requireOffset();
    }

    toEngine(tLocal) {
      return tLocal + this.#requireOffset();
    }

    #requireOffset() {
      if (!this.#offsets.length) throw new Error("no clock sample yet: ping, and wait for a pong");
      return median(this.#offsets);
    }
  }

  function keepLast(values, value) {
    values.push(value);
    if (values.length > WINDOW) values.shift();
  }

  function median(values) {
    const sorted = [...values].sort((a, b) => a - b);
    const mid = sorted.length >> 1;
    return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
  }

  return Object.freeze({ ClockSync, WINDOW, PING_INTERVAL_MS, RTT_FLOOR_MS, MAX_RTT_MS });
});
