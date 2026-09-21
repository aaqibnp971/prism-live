"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const Spectator = require("../web/spectator/model.js");

const SESSION_ONE = "S-20260920-0001";
const SESSION_TWO = "S-20260920-0002";
const ZERO = { arousal: 0, valence: 0, cognitive_load: 0, readiness: 0 };

function state(segment, options = {}) {
  const idle = segment === "idle";
  return {
    type: "state",
    v: 1,
    session: options.session || SESSION_ONE,
    seq: options.seq || 1,
    t_engine: options.tEngine || 10_000,
    t_session: idle ? null : (options.tSession ?? 1_000),
    segment,
    segment_elapsed_ms: idle ? 0 : (options.elapsed ?? 1_000),
    segment_nominal_ms: idle ? 0 : (options.nominal ?? 45_000),
    psv: options.psv || { arousal: 0.61, valence: 0.5, cognitive_load: 0.44, readiness: 0.55 },
    confidence: options.confidence || { arousal: 0.88, valence: 0, cognitive_load: 0.71, readiness: 0.63 },
    authority: options.authority || { arousal: 0.2, valence: 0, cognitive_load: 0.2, readiness: 0 },
    hr_bpm: options.hrBpm === undefined ? 82.4 : options.hrBpm,
    hr_base: options.hrBase === undefined ? 68.2 : options.hrBase,
    signal: options.signal || { contact: true, rr_accepted_pct: 0.94, baseline_quality: 0.81 },
  };
}

function beat(seq, bpm, options = {}) {
  return {
    type: "beat",
    v: 1,
    session: options.session || SESSION_ONE,
    seq,
    t_play: options.tPlay || 10_000 + seq * 800,
    rr_ms: 60_000 / bpm,
    hr_bpm: bpm,
    quality: options.quality || "ok",
  };
}

test("cold idle is a waiting system with no readings, trace or progress", () => {
  const model = new Spectator.SpectatorModel();
  assert.equal(model.onState(state("idle", { psv: { ...ZERO, valence: 0.5 }, confidence: ZERO, authority: ZERO })), true);
  const view = model.view();
  assert.equal(view.kind, "idle-cold");
  assert.equal(view.title, "READY FOR THE NEXT SESSION");
  assert.equal(view.readings, null);
  assert.equal(view.progress, null);
  assert.deepEqual(view.trace, []);
  assert.equal(model.onBeat(beat(1, 75)), false);
});

test("segment progress comes directly from host elapsed and nominal fields", () => {
  const model = new Spectator.SpectatorModel();
  assert.equal(model.onState(state("regulate", { elapsed: 90_000, nominal: 75_000 })), true);
  const view = model.view();
  assert.equal(view.verb, "TAKING THINGS AWAY");
  assert.deepEqual(view.progress, {
    elapsedMs: 90_000,
    nominalMs: 75_000,
    fraction: 1,
    overrunMs: 15_000,
  });
});

test("valence is explicitly unreadable with exact zero confidence and authority", () => {
  const model = new Spectator.SpectatorModel();
  const message = state("load");
  message.psv.valence = 0.91; // A host bug must never become a claim the screen cannot support.
  assert.equal(model.onState(message), true);
  const valence = model.view().readings.dimensions.find((dimension) => dimension.key === "valence");
  assert.deepEqual(valence, {
    key: "valence",
    label: "YOUR VALENCE",
    value: 0.5,
    confidence: 0,
    authority: 0,
    status: "NOT READABLE",
  });
  assert.equal(
    Spectator.validState(
      state("load", { confidence: { arousal: 0.8, valence: 0.01, cognitive_load: 0.7, readiness: 0.5 } }),
    ),
    false,
  );
});

test("t_session is null only in idle, matching the frozen contract", () => {
  const model = new Spectator.SpectatorModel();
  const activeWithoutClock = state("baseline");
  activeWithoutClock.t_session = null;
  assert.equal(model.onState(activeWithoutClock), false);

  const idleWithClock = state("idle");
  idleWithClock.t_session = 0;
  assert.equal(model.onState(idleWithClock), false);
});

test("the trace takes exactly one sample from each rendered non-rejected beat", () => {
  const model = new Spectator.SpectatorModel();
  assert.equal(model.onState(state("baseline", { hrBpm: 70 })), true);
  assert.equal(model.onState(state("baseline", { seq: 2, hrBpm: 95, elapsed: 2_000 })), true);
  assert.equal(model.view().trace.length, 0); // state cadence never samples the trace
  assert.equal(model.onBeat(beat(1, 70)), true);
  assert.equal(model.onBeat(beat(2, 71, { quality: "interpolated" })), true);
  assert.equal(model.onBeat(beat(3, 220, { quality: "rejected" })), false);
  assert.equal(model.onBeat(beat(4, 72)), true);
  assert.deepEqual(model.view().trace.map((sample) => sample.bpm), [70, 71, 72]);
});

test("heart-rate scaling follows the data instead of a 50 to 155 rail", () => {
  const high = Spectator.traceScale([{ bpm: 178 }, { bpm: 205 }]);
  assert.ok(high.minimum < 178);
  assert.ok(high.maximum > 205);
  const low = Spectator.traceScale([{ bpm: 34 }, { bpm: 39 }]);
  assert.ok(low.minimum < 34);
  assert.ok(low.maximum < 50);
});

test("a completed trace survives reset and idle, then clears at the next baseline", () => {
  const model = new Spectator.SpectatorModel();
  model.onState(state("baseline"));
  model.onBeat(beat(1, 68));
  model.onBeat(beat(2, 92));
  model.onState(state("resolve", { seq: 2, elapsed: 44_000, nominal: 45_000 }));
  model.onState(state("reset", { seq: 3, elapsed: 0, nominal: 20_000 }));
  let view = model.view();
  assert.equal(view.kind, "trace-hold");
  assert.equal(view.readings, null);
  assert.equal(view.progress, null);
  assert.deepEqual(view.trace.map((sample) => sample.bpm), [68, 92]);

  model.onState(state("idle", { session: SESSION_TWO, seq: 1 }));
  view = model.view();
  assert.equal(view.kind, "idle-trace");
  assert.equal(view.title, "YOUR FOUR MINUTES");
  assert.equal(view.readings, null);
  assert.deepEqual(view.trace.map((sample) => sample.bpm), [68, 92]);

  model.onState(state("baseline", { session: SESSION_TWO, seq: 2 }));
  view = model.view();
  assert.equal(view.kind, "active");
  assert.deepEqual(view.trace, []);
});

test("a short stop reset does not present a partial run as the last session", () => {
  const model = new Spectator.SpectatorModel();
  model.onState(state("baseline"));
  model.onBeat(beat(1, 68));
  model.onState(state("reset", { seq: 2, elapsed: 0, nominal: 3_000 }));
  assert.equal(model.view().kind, "resetting");
  model.onState(state("idle", { session: SESSION_TWO, seq: 1 }));
  assert.equal(model.view().kind, "idle-cold");
});

test("state silence becomes a lost feed just after 2.5 seconds", () => {
  assert.equal(Spectator.linkState(true, true, 1_000, 3_500), "live");
  assert.equal(Spectator.linkState(true, true, 1_000, 3_501), "lost");
  assert.equal(Spectator.linkState(false, true, 1_000, 1_100), "lost");
  assert.equal(Spectator.linkState(true, false, 1_000, 3_500), "waiting");
  assert.equal(Spectator.linkState(true, false, 1_000, 3_501), "lost");
  assert.equal(Spectator.linkState(false, false, null, 99_000), "waiting");
});

test("trace geometry uses scheduled play time and the computed scale", () => {
  const samples = [
    { tPlay: 1_000, bpm: 60, quality: "ok" },
    { tPlay: 2_000, bpm: 90, quality: "ok" },
    { tPlay: 4_000, bpm: 75, quality: "ok" },
  ];
  const points = Spectator.tracePoints(samples, 1_000, 200);
  assert.equal(points.length, 3);
  assert.ok(points[0].x < points[1].x && points[1].x < points[2].x);
  assert.ok(points[1].y < points[2].y && points[2].y < points[0].y);
});

test("every trace point, including a new minimum and the endpoint, stays inside plot bounds", () => {
  // Regression: the old CSS let the whole SVG spill over its card even with sound data scaling.
  // Browser coverage checks the actual card; this checks the full range before clipping.
  for (const bpms of [[82, 78, 34], [178, 205, 240], [68, 68, 68], [1, 250, 2]]) {
    const samples = bpms.map((bpm, index) => ({ bpm, tPlay: 1000 + index * 800, quality: "ok" }));
    const scale = Spectator.traceScale(samples);
    assert.ok(scale.minimum <= Math.min(...bpms));
    assert.ok(scale.maximum >= Math.max(...bpms));
    for (const [width, height] of [[1000, 210], [1000, 280], [300, 85]]) {
      for (const point of Spectator.tracePoints(samples, width, height)) {
        assert.ok(point.x >= 12 && point.x <= width - 12);
        assert.ok(point.y >= 12 && point.y <= height - 12);
      }
    }
  }
});

test("reading hatches use host confidence while authority is passed through unchanged", () => {
  const model = new Spectator.SpectatorModel();
  model.onState(state("regulate", {
    confidence: { arousal: 0.4, valence: 0, cognitive_load: 0, readiness: 0.6 },
    authority: { arousal: 0.137, valence: 0, cognitive_load: 0, readiness: 0.271 },
  }));
  const dims = model.view().readings.dimensions;
  assert.equal(dims[0].authority, 0.137);
  assert.equal(dims[3].authority, 0.271);
  const arousal = Spectator.readingTreatment(dims[0]);
  assert.equal(arousal.fill, dims[0].value);
  assert.ok(Math.abs(arousal.width - 0.54) < 1e-9);
  for (const dim of [dims[1], dims[2]]) {
    const unknown = Spectator.readingTreatment(dim);
    assert.equal(unknown.fill, 0);
    assert.equal(unknown.readable, false);
    assert.ok(Math.abs(unknown.width - 0.9) < 1e-9);
  }
});

function revealRun() {
  const model = new Spectator.SpectatorModel();
  model.onState(state("baseline", { tEngine: 10_000, elapsed: 0, hrBase: null, authority: ZERO }));
  model.onBeat(beat(1, 160.14, { tPlay: 11_000 }));
  model.onBeat(beat(2, 70, { tPlay: 12_000 }));
  model.onState(state("load", { seq: 2, tEngine: 66_000, elapsed: 0, nominal: 75_000 }));
  model.onBeat(beat(3, 108.24, { tPlay: 67_000 }));
  model.onBeat(beat(4, 110.06, { tPlay: 68_000 }));
  model.onBeat(beat(5, 240, { tPlay: 69_000, quality: "rejected" }));
  model.onState(state("regulate", {
    seq: 3, tEngine: 141_000, elapsed: 0, nominal: 75_000,
    authority: { arousal: 0.437, valence: 0, cognitive_load: 0.313, readiness: 0.127 },
  }));
  model.onBeat(beat(6, 180, { tPlay: 142_000 })); // Not a load peak.
  model.onState(state("resolve", { seq: 4, tEngine: 216_000, elapsed: 0, authority: ZERO }));
  model.onBeat(beat(7, 82.18, { tPlay: 217_000 }));
  return model;
}

test("the reveal follows host resolve remaining time, including a different nominal duration", () => {
  const model = revealRun();
  model.onState(state("resolve", { seq: 5, tEngine: 240_999, elapsed: 24_999 }));
  assert.equal(model.view().reveal, false);
  model.onState(state("resolve", { seq: 6, tEngine: 241_000, elapsed: 25_000 }));
  assert.equal(model.view().reveal, true);
  const shortened = new Spectator.SpectatorModel();
  shortened.onState(state("resolve", { nominal: 35_000, elapsed: 14_999 }));
  assert.equal(shortened.view().reveal, false);
  shortened.onState(state("resolve", { seq: 2, nominal: 35_000, elapsed: 15_000 }));
  assert.equal(shortened.view().reveal, true);
  const extendedRegulate = new Spectator.SpectatorModel();
  extendedRegulate.onState(state("regulate", { nominal: 75_000, elapsed: 105_000 }));
  assert.equal(extendedRegulate.view().reveal, false);
});

test("peak is load-only; first includes settling; endpoint and N use the same displayed beats", () => {
  const model = revealRun();
  let summary = model.view().summary;
  assert.equal(summary.satDownAt, 160.1);
  assert.equal(summary.peakedAt, 110.1); // Neither the 160 baseline nor 180 regulate spike.
  assert.equal(summary.leftAt, 82.2);
  assert.equal(summary.difference, 27.9);
  assert.deepEqual(summary.authority, { arousal: 0.437, valence: 0, cognitive_load: 0.313, readiness: 0.127 });
  model.onBeat(beat(8, 80.14, { tPlay: 242_000 }));
  const misleadingState = state("resolve", { seq: 5, tEngine: 243_000, elapsed: 27_000, hrBpm: 240 });
  misleadingState.drop_bpm = 999;
  model.onState(misleadingState);
  summary = model.view().summary;
  assert.equal(summary.leftAt, 80.1);
  assert.equal(summary.difference, 30);
  assert.equal(Object.hasOwn(summary, "outcome"), false);
  assert.equal(Object.hasOwn(summary, "close"), false);
  model.onBeat(beat(9, 115.16, { tPlay: 244_000 }));
  assert.equal(model.view().summary.difference, -5.1); // No "fall" clamp or verdict.
});

test("delayed host boundaries classify beats by t_play, not arrival or the last segment seen", () => {
  const model = new Spectator.SpectatorModel();
  model.onState(state("baseline", { tEngine: 1_000, elapsed: 0, hrBase: null }));
  model.onBeat(beat(1, 140, { tPlay: 2_000 }));
  model.onBeat(beat(2, 100, { tPlay: 10_500 })); // Rendered before a delayed load state arrived.
  assert.equal(model.view().summary.peakedAt, null);
  model.onState(state("load", { seq: 2, tEngine: 11_000, elapsed: 1_000 }));
  assert.equal(model.view().summary.peakedAt, 100);
  model.onBeat(beat(3, 180, { tPlay: 12_000 }));
  model.onState(state("regulate", { seq: 3, tEngine: 12_500, elapsed: 500 }));
  assert.equal(model.view().summary.peakedAt, 100); // Exact boundary belongs to regulate.
});

test("reset and a new idle session freeze numbers, history and resting line until baseline", () => {
  const model = revealRun();
  const expected = model.view().summary;
  model.onState(state("reset", { seq: 5, tEngine: 261_000, elapsed: 0, nominal: 20_000, hrBase: null, authority: ZERO }));
  const held = model.view();
  assert.deepEqual(held.summary, expected);
  assert.equal(model.onBeat(beat(8, 200, { tPlay: 262_000 })), false);
  model.onState(state("reset", { seq: 6, tEngine: 270_000, elapsed: 9_000, nominal: 20_000, hrBpm: 200, hrBase: null }));
  model.onState(state("idle", { session: SESSION_TWO, hrBase: null, hrBpm: null, authority: ZERO }));
  assert.equal(model.view().kind, "idle-trace");
  assert.deepEqual(model.view().summary, expected);
  assert.deepEqual(model.view().trace, held.trace);
  model.onState(state("baseline", { session: SESSION_TWO, seq: 2, elapsed: 0, hrBase: null, authority: ZERO }));
  assert.equal(model.view().summary.satDownAt, null);
  assert.equal(model.view().summary.peakedAt, null);
  assert.equal(model.view().summary.leftAt, null);
  assert.equal(model.view().summary.restingBpm, null);
  assert.deepEqual(model.view().summary.authority, ZERO);
  assert.deepEqual(model.view().trace, []);
});

test("missing load or resolve beats never produce an invented endpoint or N", () => {
  const model = new Spectator.SpectatorModel();
  model.onState(state("regulate", { elapsed: 30_000 }));
  model.onBeat(beat(1, 82));
  model.onState(state("resolve", { seq: 2, tEngine: 100_000, elapsed: 25_000 }));
  let summary = model.view().summary;
  assert.equal(summary.partialHistory, true);
  assert.equal(summary.satDownAt, null);
  assert.equal(summary.peakedAt, null);
  assert.equal(summary.leftAt, null);
  assert.equal(summary.difference, null);
  model.onBeat(beat(2, 75, { tPlay: 101_000 }));
  summary = model.view().summary;
  assert.equal(summary.leftAt, 75);
  assert.equal(summary.difference, null);
});

test("all beats survive a long session, including the first reading beyond 1024 samples", () => {
  const model = new Spectator.SpectatorModel();
  model.onState(state("baseline", { tEngine: 1_000, elapsed: 0 }));
  for (let i = 1; i <= 1200; i += 1) model.onBeat(beat(i, i === 1 ? 150 : 72, { tPlay: 1_000 + i * 240 }));
  assert.equal(model.view().trace.length, 1200);
  assert.equal(model.view().summary.satDownAt, 150);
});

test("resting reference uses only hr_base, extends autoscale and vanishes on null", () => {
  const samples = [{ bpm: 100, tPlay: 1000 }, { bpm: 120, tPlay: 2000 }];
  for (const resting of [34, 180]) {
    const scale = Spectator.traceScale(samples, resting);
    assert.ok(scale.minimum < Math.min(resting, 100));
    assert.ok(scale.maximum > Math.max(resting, 120));
    for (const height of [210, 420]) {
      const y = Spectator.referenceY(samples, resting, height);
      const point = Spectator.tracePoints([{ bpm: resting, tPlay: 1500 }, ...samples], 1000, height, 12, resting)[0];
      assert.equal(y, point.y);
      assert.ok(y >= 12 && y <= height - 12);
    }
  }
  assert.equal(Spectator.referenceY(samples, null, 420), null);
  const model = revealRun();
  model.onState(state("resolve", { seq: 5, tEngine: 241_000, elapsed: 25_000, hrBase: null }));
  assert.equal(model.view().summary.restingBpm, null);
  model.onState(state("reset", { seq: 6, tEngine: 261_000, elapsed: 0, nominal: 20_000, hrBase: null }));
  model.onState(state("idle", { session: SESSION_TWO, hrBase: null }));
  assert.equal(model.view().summary.restingBpm, null);
});

test("an interrupted active trace is partial, without rewriting a completed held summary", () => {
  const model = revealRun();
  assert.equal(model.view().summary.partialHistory, false);
  model.markInterrupted();
  assert.equal(model.view().summary.partialHistory, true);
  model.onState(state("reset", { seq: 5, tEngine: 261_000, elapsed: 0, nominal: 20_000 }));
  const held = model.view().summary;
  model.markInterrupted();
  assert.deepEqual(model.view().summary, held);
  model.onState(state("idle", { session: SESSION_TWO }));
  model.onState(state("baseline", { session: SESSION_TWO, seq: 2, elapsed: 0 }));
  assert.equal(model.view().summary.partialHistory, false);
});

test("a stop during the reveal is not a completed session to retain through idle", () => {
  const model = revealRun();
  model.onState(state("resolve", { seq: 5, tEngine: 241_000, elapsed: 25_000 }));
  assert.equal(model.view().reveal, true);
  model.onState(state("reset", { seq: 6, tEngine: 242_000, elapsed: 0, nominal: 3_000 }));
  assert.equal(model.view().kind, "resetting");
  assert.equal(model.view().summary, null);
  model.onState(state("idle", { session: SESSION_TWO }));
  assert.equal(model.view().kind, "idle-cold");
});
