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
