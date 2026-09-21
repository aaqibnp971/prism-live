/*
 * Prism Live spectator state and trace reveal, prompts 3.3 / 3.5.
 *
 * This file contains no DOM, timers, fixture data or fallback animation. It turns only validated
 * contract messages into a view. In particular, idle never exposes the previous visitor's live
 * readings: it is either a cold waiting screen or the explicitly held, completed trace.
 */

(function exposeSpectator(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.PrismSpectator = api;
})(typeof globalThis === "undefined" ? this : globalThis, function buildSpectator() {
  "use strict";

  const VERSION = 1;
  const BUILD = "3.5.0";
  const STATE_STALE_MS = 2_500;
  const FULL_RESET_MS = 20_000;
  const REVEAL_REMAINING_MS = 20_000;
  const DIMENSIONS = Object.freeze(["arousal", "valence", "cognitive_load", "readiness"]);
  const SEGMENTS = Object.freeze(["idle", "baseline", "load", "regulate", "resolve", "reset"]);
  const TRACE_SEGMENTS = new Set(["baseline", "load", "regulate", "resolve"]);
  const QUALITIES = new Set(["ok", "interpolated", "rejected"]);
  const SESSION = /^S-\d{8}-\d{4}$/;

  const SEGMENT_COPY = Object.freeze({
    baseline: Object.freeze({ title: "YOUR BASELINE", verb: "LEARNING YOUR RHYTHM" }),
    load: Object.freeze({ title: "YOUR ATTENTION", verb: "FOLLOWING THE MOVING HALF" }),
    regulate: Object.freeze({ title: "YOUR RESPONSE", verb: "TAKING THINGS AWAY" }),
    resolve: Object.freeze({ title: "YOUR TRACE", verb: "DRAWING YOUR FOUR MINUTES" }),
  });

  class SpectatorModel {
    constructor() {
      this.state = null;
      this.trace = [];
      this.traceSession = null;
      this.heldTrace = null;
      this.heldSession = null;
      this.heldSummary = null;
      this.boundaries = [];
      this.restingBpm = null;
      this.authorityHistory = Object.fromEntries(DIMENSIONS.map((key) => [key, 0]));
      this.partialHistory = false;
      this.lastBeatSeq = 0;
      this.stateSeqSession = null;
      this.lastStateSeq = 0;
    }

    onState(message) {
      if (!validState(message)) return false;
      if (this.stateSeqSession === message.session && message.seq <= this.lastStateSeq) return false;

      const previous = this.state;
      const enteringBaseline =
        message.segment === "baseline" &&
        (previous?.segment !== "baseline" || previous.session !== message.session);

      if (enteringBaseline) {
        // The host, not a client timer, declares the next visitor. This is the one point where the
        // trace held through idle is cleared (prompt 3.5).
        this.heldTrace = null;
        this.heldSession = null;
        this.heldSummary = null;
        this.#startTrace(message.session);
      } else if (TRACE_SEGMENTS.has(message.segment) && this.traceSession !== message.session) {
        // A screen opened mid-session has no past samples to invent; it starts with the next beat.
        this.#startTrace(message.session);
      }

      if (this.traceSession === message.session && (TRACE_SEGMENTS.has(message.segment) || message.segment === "reset")) {
        if (this.boundaries.length === 0) {
          this.partialHistory = message.segment !== "baseline" || message.segment_elapsed_ms > 0;
        }
        if (this.boundaries.at(-1)?.segment !== message.segment) {
          // Classify by scheduled play time against HOST boundaries, not packet arrival or a
          // guessed nominal timeline. A delayed boundary can correct a beat's classification.
          this.boundaries.push(Object.freeze({
            segment: message.segment,
            start: message.t_engine - message.segment_elapsed_ms,
          }));
        }
        if (TRACE_SEGMENTS.has(message.segment)) {
          this.restingBpm = message.hr_base; // Null clears it; never substitute first/average HR.
          for (const key of DIMENSIONS) {
            this.authorityHistory[key] = Math.max(this.authorityHistory[key], message.authority[key]);
          }
        }
      }

      this.state = copyState(message);
      this.stateSeqSession = message.session;
      this.lastStateSeq = message.seq;

      if (
        message.segment === "reset" &&
        message.segment_nominal_ms === FULL_RESET_MS &&
        this.traceSession === message.session &&
        this.trace.length > 0 &&
        this.heldSession !== message.session
      ) {
        this.heldTrace = copyTrace(this.trace);
        this.heldSession = message.session;
        this.heldSummary = this.#summary();
      }
      return true;
    }

    onBeat(message) {
      if (!validBeat(message) || message.quality === "rejected") return false;
      if (
        this.state === null ||
        message.session !== this.state.session ||
        message.session !== this.traceSession ||
        !TRACE_SEGMENTS.has(this.state.segment) ||
        message.seq <= this.lastBeatSeq
      ) {
        return false;
      }
      this.lastBeatSeq = message.seq;
      this.trace.push(
        Object.freeze({
          tPlay: message.t_play,
          bpm: message.hr_bpm,
          quality: message.quality,
          seq: message.seq,
        }),
      );
      // Retain the entire host session, not a rolling window that loses "sat down at".
      return true;
    }

    markInterrupted() {
      // No reconstruction after a reconnect. This flag appears only when a valid state resumes
      // rendering; the lost-feed screen itself stays frozen. Completed held summaries stay fixed.
      if (this.state && TRACE_SEGMENTS.has(this.state.segment)) this.partialHistory = true;
    }

    view() {
      const state = this.state;
      if (state === null) return waitingView();

      if (state.segment === "idle") {
        if (this.heldTrace?.length) {
          return passiveView("idle-trace", state, this.heldTrace, this.heldSession, this.heldSummary);
        }
        return passiveView("idle-cold", state, [], null);
      }

      if (state.segment === "reset") {
        if (this.heldTrace?.length) {
          return passiveView("trace-hold", state, this.heldTrace, this.heldSession, this.heldSummary);
        }
        return passiveView("resetting", state, [], null);
      }

      const copy = SEGMENT_COPY[state.segment];
      return Object.freeze({
        kind: "active",
        session: state.session,
        segment: state.segment,
        sessionElapsedMs: state.t_session,
        title: copy.title,
        verb: copy.verb,
        progress: segmentProgress(state),
        readings: Object.freeze({
          heartBpm: state.hr_bpm,
          restingBpm: state.hr_base,
          contact: state.signal.contact,
          acceptedFraction: state.signal.rr_accepted_pct,
          baselineQuality: state.signal.baseline_quality,
          dimensions: dimensionViews(state),
        }),
        trace: copyTrace(this.trace),
        reveal: state.segment === "resolve" && state.segment_nominal_ms > 0 &&
          state.segment_nominal_ms - state.segment_elapsed_ms <= REVEAL_REMAINING_MS,
        summary: this.#summary(),
      });
    }

    #summary() {
      // Numbers describe the plotted, non-rejected beats, not the 2 s state HR or drop_bpm.
      // A high baseline/regulate reading must NEVER inflate the task's peak.
      const segmentAt = (t) => this.boundaries.filter((b) => b.start <= t)
        .sort((a, b) => a.start - b.start).at(-1)?.segment;
      const load = this.trace.filter((sample) => segmentAt(sample.tPlay) === "load");
      const resolve = this.trace.filter((sample) => segmentAt(sample.tPlay) === "resolve");
      const first = this.trace[0];
      // If the screen joined after baseline, the person's first reading is unknown.
      const satDownAt = first && segmentAt(first.tPlay) === "baseline" ? shownBpm(first.bpm) : null;
      const peakedAt = load.length ? shownBpm(Math.max(...load.map((sample) => sample.bpm))) : null;
      const leftAt = resolve.length ? shownBpm(resolve.at(-1).bpm) : null;
      return Object.freeze({
        satDownAt, peakedAt, leftAt,
        // Subtract the SAME one-decimal numbers on screen. No threshold, clamp or verdict.
        difference: peakedAt === null || leftAt === null ? null :
          (Math.round(peakedAt * 10) - Math.round(leftAt * 10)) / 10,
        restingBpm: this.restingBpm,
        authority: Object.freeze({ ...this.authorityHistory }),
        partialHistory: this.partialHistory,
      });
    }

    #startTrace(session) {
      this.trace = [];
      this.traceSession = session;
      this.lastBeatSeq = 0;
      this.boundaries = [];
      this.restingBpm = null;
      this.authorityHistory = Object.fromEntries(DIMENSIONS.map((key) => [key, 0]));
      this.partialHistory = false;
    }
  }

  function waitingView() {
    return Object.freeze({
      kind: "waiting",
      session: null,
      segment: null,
      title: "WAITING FOR LIVE DATA",
      verb: "",
      progress: null,
      readings: null,
      trace: Object.freeze([]),
      traceSession: null,
    });
  }

  function passiveView(kind, state, trace, traceSession, summary = null) {
    return Object.freeze({
      kind,
      session: state.session,
      segment: state.segment,
      title: kind === "idle-cold" ? "READY FOR THE NEXT SESSION" : "YOUR FOUR MINUTES",
      verb: kind === "resetting" ? "RESETTING FOR THE NEXT SESSION" : "",
      progress: null,
      readings: null,
      trace: copyTrace(trace),
      traceSession,
      summary,
    });
  }

  function segmentProgress(state) {
    if (state.segment === "idle" || !(state.segment_nominal_ms > 0)) return null;
    const elapsedMs = state.segment_elapsed_ms;
    const nominalMs = state.segment_nominal_ms;
    return Object.freeze({
      elapsedMs,
      nominalMs,
      fraction: clamp(elapsedMs / nominalMs, 0, 1),
      overrunMs: Math.max(0, elapsedMs - nominalMs),
    });
  }

  function dimensionViews(state) {
    const labels = {
      arousal: "YOUR AROUSAL",
      valence: "YOUR VALENCE",
      cognitive_load: "YOUR COGNITIVE LOAD",
      readiness: "YOUR READINESS",
    };
    return Object.freeze(
      DIMENSIONS.map((key) =>
        Object.freeze({
          key,
          label: labels[key],
          // Valence is not readable from a pulse. Even if a future host accidentally moves the
          // neutral placeholder, the spectator must not turn that into a claim about the person.
          value: key === "valence" ? 0.5 : state.psv[key],
          confidence: key === "valence" ? 0 : state.confidence[key],
          authority: key === "valence" ? 0 : state.authority[key],
          status: key === "valence" ? "NOT READABLE" : "LIVE READING",
        }),
      ),
    );
  }

  function shownBpm(value) {
    return Math.round(value * 10) / 10;
  }

  function traceScale(samples, restingBpm = null) {
    const values = samples
      .map((sample) => (typeof sample === "number" ? sample : sample?.bpm))
      .filter((value) => finite(value) && value > 0);
    if (finite(restingBpm) && restingBpm > 0) values.push(restingBpm);
    if (values.length === 0) return null;

    let low = Math.min(...values);
    let high = Math.max(...values);
    if (high - low < 8) {
      const middle = (low + high) / 2;
      low = middle - 4;
      high = middle + 4;
    }
    const padding = Math.max(2, (high - low) * 0.12);
    let minimum = Math.floor((low - padding) / 5) * 5;
    let maximum = Math.ceil((high + padding) / 5) * 5;
    if (maximum - minimum < 10) {
      minimum -= 5;
      maximum += 5;
    }
    return Object.freeze({ minimum, maximum });
  }

  // Visual uncertainty, not a statistical interval or a new confidence estimate. Keep the v3
  // hatch convention (half-width .45 * (1-confidence)), using the host's actual confidence.
  // Zero confidence has no reading fill/marker; valence can never look like a measured value.
  function readingTreatment(dimension) {
    const readable = dimension.key !== "valence" && dimension.confidence > 0;
    const midpoint = readable ? dimension.value : 0.5;
    const halfWidth = (1 - dimension.confidence) * 0.45;
    const left = clamp(midpoint - halfWidth, 0, 1);
    const right = clamp(midpoint + halfWidth, 0, 1);
    return Object.freeze({ readable, fill: readable ? dimension.value : 0, left, width: right - left });
  }

  function tracePoints(samples, width, height, inset = 12, restingBpm = null) {
    const scale = traceScale(samples, restingBpm);
    if (samples.length === 0 || scale === null || !(width > 2 * inset) || !(height > 2 * inset)) return Object.freeze([]);
    const first = samples[0].tPlay;
    const last = samples.at(-1).tPlay;
    const duration = Math.max(1, last - first);
    const bpmSpan = scale.maximum - scale.minimum;
    return Object.freeze(
      samples.map((sample, index) => {
        const along = last === first ? index / Math.max(1, samples.length - 1) : (sample.tPlay - first) / duration;
        return Object.freeze({
          x: inset + clamp(along, 0, 1) * (width - 2 * inset),
          y:
            inset +
            (1 - clamp((sample.bpm - scale.minimum) / bpmSpan, 0, 1)) *
              (height - 2 * inset),
          quality: sample.quality,
        });
      }),
    );
  }

  function referenceY(samples, restingBpm, height, inset = 12) {
    if (!(finite(restingBpm) && restingBpm > 0) || !(height > 2 * inset)) return null;
    const scale = traceScale(samples, restingBpm);
    return inset + (1 - (restingBpm - scale.minimum) / (scale.maximum - scale.minimum)) * (height - 2 * inset);
  }

  function linkState(socketOpen, hasState, lastStateAt, now) {
    // Before the first state, lastStateAt is the connection-open time. Afterwards it is the
    // arrival time of the last verified state. Either kind of silence gets the same short limit.
    if (!socketOpen) return hasState ? "lost" : "waiting";
    if (finite(lastStateAt) && now - lastStateAt > STATE_STALE_MS) return "lost";
    if (socketOpen && hasState) return "live";
    return "waiting";
  }

  function validState(message) {
    return (
      baseMessage(message, "state") &&
      whole(message.seq) &&
      finite(message.t_engine) &&
      SEGMENTS.includes(message.segment) &&
      (message.segment === "idle"
        ? message.t_session === null
        : finiteNonNegative(message.t_session)) &&
      finiteNonNegative(message.segment_elapsed_ms) &&
      finiteNonNegative(message.segment_nominal_ms) &&
      (message.segment !== "idle" ||
        (message.segment_elapsed_ms === 0 && message.segment_nominal_ms === 0)) &&
      validDimensions(message.psv) &&
      validDimensions(message.confidence) &&
      validDimensions(message.authority) &&
      message.confidence.valence === 0 &&
      message.authority.valence === 0 &&
      nullablePositive(message.hr_bpm) &&
      nullablePositive(message.hr_base) &&
      validSignal(message.signal)
    );
  }

  function validBeat(message) {
    return (
      baseMessage(message, "beat") &&
      whole(message.seq) &&
      finiteNonNegative(message.t_play) &&
      finite(message.rr_ms) &&
      message.rr_ms > 0 &&
      finite(message.hr_bpm) &&
      message.hr_bpm > 0 &&
      QUALITIES.has(message.quality)
    );
  }

  function validPong(message) {
    return (
      message !== null &&
      typeof message === "object" &&
      message.type === "clock" &&
      message.v === VERSION &&
      message.role === "pong" &&
      finiteNonNegative(message.t_client_sent) &&
      finiteNonNegative(message.t_engine)
    );
  }

  function baseMessage(message, type) {
    return (
      message !== null &&
      typeof message === "object" &&
      message.type === type &&
      message.v === VERSION &&
      typeof message.session === "string" &&
      SESSION.test(message.session)
    );
  }

  function validDimensions(value) {
    return (
      value !== null &&
      typeof value === "object" &&
      DIMENSIONS.every((key) => finite(value[key]) && value[key] >= 0 && value[key] <= 1)
    );
  }

  function validSignal(signal) {
    return (
      signal !== null &&
      typeof signal === "object" &&
      typeof signal.contact === "boolean" &&
      finite(signal.rr_accepted_pct) &&
      signal.rr_accepted_pct >= 0 &&
      signal.rr_accepted_pct <= 1 &&
      finite(signal.baseline_quality) &&
      signal.baseline_quality >= 0 &&
      signal.baseline_quality <= 1
    );
  }

  function copyState(message) {
    return Object.freeze({
      ...message,
      psv: Object.freeze({ ...message.psv }),
      confidence: Object.freeze({ ...message.confidence }),
      authority: Object.freeze({ ...message.authority }),
      signal: Object.freeze({ ...message.signal }),
    });
  }

  function copyTrace(trace) {
    return Object.freeze(trace.map((sample) => Object.freeze({ ...sample })));
  }

  function nullablePositive(value) {
    return value === null || (finite(value) && value > 0);
  }

  function finiteNonNegative(value) {
    return finite(value) && value >= 0;
  }

  function whole(value) {
    return Number.isSafeInteger(value) && value >= 0;
  }

  function finite(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  return Object.freeze({
    VERSION,
    BUILD,
    STATE_STALE_MS,
    FULL_RESET_MS,
    REVEAL_REMAINING_MS,
    DIMENSIONS,
    SEGMENT_COPY,
    SpectatorModel,
    dimensionViews,
    readingTreatment,
    linkState,
    segmentProgress,
    tracePoints,
    traceScale,
    referenceY,
    validBeat,
    validPong,
    validState,
  });
});
