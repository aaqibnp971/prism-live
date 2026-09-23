/* Prompt 3.4: the portable STATE -> SEVEN TOKENS contract. No DOM, clock or rendering.
 *
 * Unity must port this mapping, not reverse-engineer screenshots. Authority comes from the host:
 * effective = .5 + (psv - .5) * authority. LOAD's .20 ceiling is ALREADY in authority; never apply
 * it again. Regulate ranges run from low arousal/load to high, not from elapsed start to end.
 * Baseline alone uses confidence directly, because learning is visible while authority is zero.
 * Pulse amplitude uses the authored 62..95 bpm endpoints, then tapers to ZERO at 120 bpm;
 * no event, no pulse. Unknown HR has zero amplitude, never an invented reference rate.
 *
 * Horizon y is measured FROM THE TOP. Light centre is always (.50,.40), so y < .40 would put
 * the horizon above the light. Production segment values stay >= .44.
 */
(function expose(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.PrismFieldMapping = api;
})(globalThis, function buildMapping() {
  "use strict";
  const ACTIVE_SEGMENTS = Object.freeze(["baseline", "load", "regulate", "resolve"]);
  const KEYS = Object.freeze(["fog", "light", "hue", "sat", "horizon", "pulse", "motion"]);
  const DIMENSIONS = ["arousal", "valence", "cognitive_load", "readiness"];
  const DISSOLVE_MS = 10_000;
  const BASES = Object.freeze({
    baseline: Object.freeze({ fog: .38, light: .45, hue: 208, sat: .12, horizon: .44, pulse: .06, motion: .008 }),
    load: Object.freeze({ fog: .20, light: .70, hue: 214, sat: .16, horizon: .50, pulse: .10, motion: .010 }),
    regulate: Object.freeze({ fog: .34, light: .95, hue: 46, sat: .30, horizon: .56, pulse: .16, motion: .030 }),
    resolve: Object.freeze({ fog: .44, light: .50, hue: 30, sat: .14, horizon: .58, pulse: .08, motion: .006 }),
  });
  const finite = Number.isFinite;
  const unit = (n) => finite(n) && n >= 0 && n <= 1;
  const nonnegative = (n) => finite(n) && n >= 0;
  const clamp = (n) => Math.max(0, Math.min(1, n));
  const mix = (a, b, x) => a + (b - a) * clamp(x);
  const effective = (value, authority) => .5 + (value - .5) * authority;
  const baselineConfidence = (s) => clamp((s.confidence.arousal + s.confidence.cognitive_load + s.confidence.readiness) / 3 / .393);
  const visualRateGain = (bpm) => finite(bpm) && bpm > 0 ? clamp((120 - bpm) / 25) : 0;
  // Shared by state mapping and the live beat guard. Apply the taper ONCE, including resolve.
  // Audio is independent: this is only the optional visual reinforcement of each heartbeat.
  function pulseAmplitude(s, bpm = s.hr_bpm) {
    if (!ACTIVE_SEGMENTS.includes(s.segment)) return 0;
    const rate = clamp((bpm - 62) / 33);
    const gain = visualRateGain(bpm);
    if (gain === 0) return 0;
    if (s.segment === "resolve") {
      const remaining = s.segment_nominal_ms - s.segment_elapsed_ms;
      return remaining <= 0 ? 0 : .08 * gain *
        Math.cos(Math.PI / 2 * clamp((3_000 - remaining) / 3_000));
    }
    const [lo, hi] = { baseline: [.06, .10], load: [.10, .16], regulate: [.08, .16] }[s.segment];
    return mix(lo, hi, rate) * gain;
  }

  // Validate every consumed contract input before changing a visible field. No authority is
  // inferred from confidence, and valence cannot acquire authority via a malformed message.
  function validState(s) {
    if (!s || s.type !== "state" || s.v !== 1 || !/^S-\d{8}-\d{4}$/.test(s.session) ||
        !Number.isSafeInteger(s.seq) || s.seq < 1 || !nonnegative(s.t_engine) ||
        !["idle", ...ACTIVE_SEGMENTS, "reset"].includes(s.segment) ||
        !nonnegative(s.segment_elapsed_ms) || !nonnegative(s.segment_nominal_ms)) return false;
    if (s.segment === "idle" ? s.t_session !== null || s.segment_elapsed_ms !== 0 || s.segment_nominal_ms !== 0 : !nonnegative(s.t_session)) return false;
    if (![s.psv, s.confidence, s.authority].every((d) => d && DIMENSIONS.every((k) => unit(d[k])))) return false;
    return s.confidence.valence === 0 && s.authority.valence === 0 &&
      (s.hr_bpm === null || (finite(s.hr_bpm) && s.hr_bpm > 0));
  }

  function mapState(s, resolveEntry = null) {
    if (!validState(s)) throw new TypeError("Invalid field state");
    if (!ACTIVE_SEGMENTS.includes(s.segment)) return null;
    const a = effective(s.psv.arousal, s.authority.arousal);
    const c = effective(s.psv.cognitive_load, s.authority.cognitive_load);
    let result;
    if (s.segment === "baseline") {
      const b = baselineConfidence(s);
      result = { ...BASES.baseline, fog: mix(.38, .26, b), light: mix(.45, .62, b),
        horizon: mix(.44, .50, b) };
    } else if (s.segment === "load") {
      result = { fog: mix(.20, .30, c), light: mix(.70, 1.05, a), hue: mix(214, 202, a),
        sat: mix(.16, .30, a), horizon: .50, motion: mix(.010, .004, c) };
    } else if (s.segment === "regulate") {
      result = { fog: mix(.58, .34, a), light: mix(.38, .95, a), hue: mix(24, 46, a),
        sat: mix(.14, .30, c), horizon: mix(.44, .56, c), motion: mix(.008, .030, a) };
    } else {
      result = { ...BASES.resolve };
      // The host tapers authority in resolve. Hold entry offsets, scaled by its observed ratio,
      // rather than recomputing a ceiling or inventing a local segment timer. A mid-resolve join
      // has no entry history: use the fixed resolve palette; do not manufacture what was missed.
      if (resolveEntry) {
        for (const k of ["hue", "light", "fog", "motion", "sat"]) {
          const d = k === "sat" ? "cognitive_load" : "arousal";
          const initial = resolveEntry.authority[d];
          const ratio = initial > 0 ? clamp(s.authority[d] / initial) : 0;
          result[k] = mix(BASES.resolve[k], resolveEntry.tokens[k], ratio);
        }
      }
      const remaining = s.segment_nominal_ms - s.segment_elapsed_ms;
      result.horizon = mix(.44, .58, (20_000 - remaining) / 8_000);
    }
    result.pulse = pulseAmplitude(s);
    return Object.freeze(result);
  }

  class FieldState {
    constructor() { this.reset(); }
    reset() {
      this.state = null;
      this.tokens = null;
      this.previousTokens = null;
      this.resolveEntry = null;
    }
    onState(s) {
      if (!validState(s)) return false;
      const old = this.state;
      if (old?.session === s.session && (s.seq <= old.seq || s.t_engine < old.t_engine)) return false;
      const sameSession = old?.session === s.session;
      const entered = !sameSession || old.segment !== s.segment;
      if (entered) {
        this.previousTokens = sameSession && ACTIVE_SEGMENTS.includes(old.segment) ? this.tokens : null;
        this.resolveEntry = s.segment === "resolve" && old?.segment === "regulate" && sameSession ?
          { tokens: this.tokens, authority: { ...s.authority } } : null;
      }
      this.tokens = mapState(s, this.resolveEntry);
      this.state = { ...s, psv: { ...s.psv }, confidence: { ...s.confidence }, authority: { ...s.authority } };
      if (!this.tokens) this.previousTokens = null;
      return true;
    }
    layers() {
      if (!this.tokens) return [];
      // Never mix seven hue values between segments: dissolve two complete rendered worlds.
      // Weight is HOST elapsed, not requestAnimationFrame time. No local segment transitions.
      const weight = clamp(this.state.segment_elapsed_ms / DISSOLVE_MS);
      if (!this.previousTokens || weight >= 1) return [{ tokens: this.tokens, weight: 1 }];
      // Pulse amplitude belongs to the CURRENT segment, including resolve's final fade.
      const from = { ...this.previousTokens, pulse: this.tokens.pulse };
      return [{ tokens: from, weight: 1 - weight }, { tokens: this.tokens, weight }];
    }
  }
  return Object.freeze({ ACTIVE_SEGMENTS, BASES, KEYS, DISSOLVE_MS, effective, baselineConfidence, visualRateGain, pulseAmplitude, validState, mapState, FieldState });
});
