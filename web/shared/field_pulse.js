/* Scheduled visual heartbeat. This is NOT the musical pulse stem.
 * No timer invents a beat. Clock conversion is supplied by the connection's ClockSync.
 * A 90 ms raised-cosine rise, 180 ms fall, zero outside the 270 ms support. The renderer owns
 * the <=.22 LINEAR luminance rail; this module owns scheduling, non-addition and rate defence.
 */
(function expose(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.PrismFieldPulse = api;
})(globalThis, function buildPulse() {
  "use strict";
  const RISE_MS = 90;
  const FALL_MS = 180;
  const DURATION_MS = RISE_MS + FALL_MS;
  const MAX_ONSET_LATENESS_MS = 45; // Drop a frame-starved onset; never start a fresh late rise.
  const MIN_INTERVAL_MS = 60_000 / 120; // At/below this interval visuals are silent, not decimated.
  const MAX_QUEUE = 64;
  function envelope(age) {
    if (!Number.isFinite(age) || age <= 0 || age >= DURATION_MS) return 0;
    if (age < RISE_MS) return .5 - .5 * Math.cos(Math.PI * age / RISE_MS);
    return .5 + .5 * Math.cos(Math.PI * (age - RISE_MS) / FALL_MS);
  }
  function validBeat(m) {
    return !!m && m.type === "beat" && m.v === 1 && /^S-\d{8}-\d{4}$/.test(m.session) &&
      Number.isSafeInteger(m.seq) && m.seq >= 1 && Number.isFinite(m.t_play) && m.t_play >= 0 &&
      Number.isFinite(m.rr_ms) && m.rr_ms > 0 && Number.isFinite(m.hr_bpm) && m.hr_bpm > 0 &&
      ["ok", "interpolated", "rejected"].includes(m.quality);
  }
  class BeatPulse {
    constructor() { this.reset(); }
    reset(session = null) {
      this.session = session;
      this.lastSeq = 0;
      this.stats = { late: 0, rejected: 0, duplicate: 0, rate_limited: 0, overflow: 0 };
      this.clear();
    }
    clear() {
      this.queue = [];
      this.active = null;
      this.lastPlanned = null;
    }
    schedule(m, localPlay, receivedAt) {
      if (!validBeat(m) || m.session !== this.session || !Number.isFinite(localPlay) || !Number.isFinite(receivedAt)) return false;
      if (m.seq <= this.lastSeq) { this.stats.duplicate++; return false; }
      this.lastSeq = m.seq;
      if (m.quality === "rejected") { this.stats.rejected++; return false; }
      if (localPlay < receivedAt) { this.stats.late++; return false; }
      const interval = this.lastPlanned === null ? Infinity : localPlay - this.lastPlanned;
      if (interval <= 0) { this.stats.rate_limited++; return false; }
      // Keep EVERY eligible future candidate, even if suppressed. Measuring from the last
      // visible beat instead would invent a slower, every-other-beat flash at high rates.
      this.lastPlanned = localPlay;
      const bpm = Math.max(m.hr_bpm, 60_000 / m.rr_ms, 60_000 / interval);
      if (bpm >= 120 || interval <= MIN_INTERVAL_MS) {
        this.stats.rate_limited++; return false;
      }
      if (this.queue.length >= MAX_QUEUE) { this.stats.overflow++; return false; }
      this.queue.push({ time: localPlay, bpm });
      return true;
    }
    value(now) {
      if (!Number.isFinite(now)) return 0;
      while (this.queue.length && this.queue[0].time <= now) {
        const beat = this.queue.shift();
        if (now - beat.time > MAX_ONSET_LATENESS_MS) { this.stats.late++; continue; }
        this.active = beat;
      }
      if (!this.active) return 0;
      const age = now - this.active.time;
      if (age >= DURATION_MS) { this.active = null; return 0; }
      return envelope(age); // One bounded envelope, NEVER sum beat amplitudes.
    }
    activeRate() { return this.active?.bpm ?? null; }
  }
  return Object.freeze({ RISE_MS, FALL_MS, DURATION_MS, MAX_ONSET_LATENESS_MS, MIN_INTERVAL_MS, MAX_QUEUE, envelope, validBeat, BeatPulse });
});
