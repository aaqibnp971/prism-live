// ES-module facade for the build-free clock synchroniser.
//
// The classic implementation also exposes `globalThis.PrismClockSync`, allowing pages opened
// directly from disk to use the same arithmetic without a bundler or local HTTP server.

import "./clock_sync_core.js";

const api = globalThis.PrismClockSync;
if (!api) throw new Error("clock_sync_core.js did not initialise");

export const {
  ClockSync,
  WINDOW,
  PING_INTERVAL_MS,
  RTT_FLOOR_MS,
  MAX_RTT_MS,
} = api;
