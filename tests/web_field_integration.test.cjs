"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const Mapping = require("../web/shared/field_mapping.js");
const Pulse = require("../web/shared/field_pulse.js");
const Clock = require("../web/shared/clock_sync_core.js");
const Field = require("../web/shared/field_view.js");
const Task = require("../web/task/task.js");

function state(segment = "baseline", seq = 1, extra = {}) {
  return {
    type: "state", v: 1, session: "S-20260922-0001", seq, t_engine: seq * 2000,
    t_session: segment === "idle" ? null : seq * 2000, segment,
    segment_elapsed_ms: segment === "idle" ? 0 : 1000,
    segment_nominal_ms: segment === "idle" ? 0 : 45000,
    psv: { arousal: .7, valence: .5, cognitive_load: .6, readiness: .5 },
    confidence: { arousal: .4, valence: 0, cognitive_load: .1, readiness: .4 },
    authority: { arousal: .2, valence: 0, cognitive_load: .2, readiness: 0 },
    hr_bpm: 75, ...extra,
  };
}

function beat(seq = 1, extra = {}) {
  return { type: "beat", v: 1, session: "S-20260922-0001", seq,
    t_play: 1500, rr_ms: 800, hr_bpm: 75, quality: "ok", ...extra };
}

function fieldHarness() {
  const frames = new Map();
  let nextFrame = 0;
  const canvas = { hidden: false, clientWidth: 800, clientHeight: 600 };
  class Renderer {
    constructor() { this.draws = []; this.sizes = []; this.clears = 0; }
    render(layers, options) { this.draws.push(structuredClone({ layers, ...options })); }
    resize(...args) { this.sizes.push(args); }
    clear() { this.clears++; }
  }
  const view = new Field.FieldView(canvas, {
    mapping: Mapping, pulse: Pulse, drawing: { Renderer },
    requestFrame(fn) { frames.set(++nextFrame, fn); return nextFrame; },
    cancelFrame(id) { frames.delete(id); },
  });
  return { view, canvas, frames, next(now) {
    assert.equal(frames.size, 1);
    const [id, fn] = frames.entries().next().value;
    frames.delete(id);
    fn(now);
  } };
}

test("graphics initialization failure is visible, disables only field and keeps accepting host state", () => {
  const errors = [];
  const canvas = { hidden: false };
  const view = new Field.FieldView(canvas, {
    mapping: Mapping, pulse: Pulse,
    drawing: { Renderer: class { constructor() { throw new Error("WebGL unavailable"); } } },
    onError(error) { errors.push(error.message); },
    requestFrame() { assert.fail("An unavailable field must not animate"); }, cancelFrame() {},
  });
  assert.deepEqual(errors, ["WebGL unavailable"]);
  assert.equal(view.available, false);
  assert.equal(view.onState(state(), 0), true);
  assert.equal(view.onBeat(beat(), 500, 0), false);
  assert.equal(canvas.hidden, true);
});

test("runtime graphics loss freezes and reports the field without an uncaught frame exception", () => {
  const { view, canvas, next, frames } = fieldHarness();
  const errors = [];
  view.onError = error => errors.push(error.message);
  view.onState(state(), 0);
  view.renderer.render = () => { throw new Error("Graphics lost"); };
  next(100);
  assert.deepEqual(errors, ["Graphics lost"]);
  assert.equal(frames.size, 0);
  assert.equal(canvas.hidden, true);
  assert.equal(view.onState(state("load", 2), 200), true);
});

test("shared view renders all four host segments, with no pulse before scheduled play", () => {
  const { view, canvas, next } = fieldHarness();
  for (const [index, segment] of Mapping.ACTIVE_SEGMENTS.entries()) {
    assert.equal(view.onState(state(segment, index + 1), 0), true);
    assert.equal(canvas.hidden, false);
  }
  assert.equal(view.onBeat(beat(), 500, 100), true);
  next(499);
  assert.equal(view.renderer.draws.at(-1).pulse, 0);
  next(500);
  assert.equal(view.renderer.draws.at(-1).pulse, 0);
  next(545);
  assert.ok(Math.abs(view.renderer.draws.at(-1).pulse - .5) < 1e-12);
  next(590);
  assert.equal(view.renderer.draws.at(-1).pulse, 1);
  assert.deepEqual(view.renderer.sizes, [[800, 600, 1]]);
});

test("freeze preserves exact last pixels, stops drift and clears queued heartbeat", () => {
  const { view, next, frames } = fieldHarness();
  view.onState(state(), 0);
  next(100);
  view.onBeat(beat(), 500, 100);
  const before = structuredClone(view.renderer.draws);
  const drift = view.drift;
  view.freeze();
  assert.equal(frames.size, 0);
  assert.deepEqual(view.renderer.draws, before);
  assert.equal(view.onBeat(beat(2), 900, 200), false);
  view.onState(state("baseline", 2), 6000);
  next(6090);
  assert.equal(view.renderer.draws.at(-1).pulse, 0);
  assert.ok(view.drift < drift + .001); // No six-second drift catch-up.
});

test("live rendered amplitude follows95..120 taper exactly once across45..180 and all segments", () => {
  for (const segment of Mapping.ACTIVE_SEGMENTS) for (const bpm of [
    ...Array.from({length:136},(_,i)=>i+45),107.5,119.999,120.001,
  ]) {
    const { view, next } = fieldHarness();
    const current = state(segment,1,{hr_bpm:bpm});
    view.onState(current,0);
    const message = beat(1,{hr_bpm:bpm,rr_ms:60000/bpm});
    assert.equal(view.onBeat(message,500,0),bpm<120);
    next(500); next(545);
    const rise = view.renderer.draws.at(-1);
    next(590);
    const peak = view.renderer.draws.at(-1);
    const expected = Mapping.mapState(current).pulse;
    for(const layer of peak.layers) assert.ok(Math.abs(layer.tokens.pulse-expected)<1e-12);
    assert.ok(Math.abs(rise.pulse-(bpm<120 ? .5 : 0))<1e-12);
    assert.equal(peak.pulse,bpm<120 ? 1 : 0);
    // The event object is read-only here; visual rejection never feeds back into audio.
    assert.deepEqual(message,beat(1,{hr_bpm:bpm,rr_ms:60000/bpm}));
    view.destroy();
  }
});

test("fast beat metadata overrides stale slow state without doubling the taper", () => {
  for (const segment of Mapping.ACTIVE_SEGMENTS) {
    for(const extra of [{hr_bpm:107.5,rr_ms:60000/107.5},{hr_bpm:75,rr_ms:60000/107.5}]) {
      const { view, next } = fieldHarness();
      const current = state(segment,1,{hr_bpm:95});
      view.onState(current,0);
      assert.equal(view.onBeat(beat(1,extra),500,0),true);
      next(500); next(590);
      for(const layer of view.renderer.draws.at(-1).layers) {
        assert.ok(Math.abs(layer.tokens.pulse-Mapping.mapState(current).pulse/2)<1e-12);
      }
      view.destroy();
    }
    for(const extra of [{hr_bpm:120,rr_ms:800},{hr_bpm:75,rr_ms:500}]) {
      const { view, next } = fieldHarness();
      view.onState(state(segment,1,{hr_bpm:95}),0);
      assert.equal(view.onBeat(beat(1,extra),500,0),false);
      next(500); next(590);
      assert.equal(view.renderer.draws.at(-1).pulse,0);
      view.destroy();
    }
  }
});

test("scheduled cadence tapers even stale HR/RR; recovery never replays suppressed beats", () => {
  const { view, next } = fieldHarness();
  view.onState(state("load",1,{hr_bpm:95}),0);
  assert.equal(view.onBeat(beat(),500,0),true);
  const second=500+60000/107.5;
  assert.equal(view.onBeat(beat(2),second,0),true);
  next(500); next(590); next(second); next(second+90);
  assert.ok(Math.abs(view.renderer.draws.at(-1).layers[0].tokens.pulse-.08)<1e-12);
  for(let i=0;i<10;i++) {
    const when=second+(i+1)*400;
    assert.equal(view.onBeat(beat(i+3),when,0),false);
    next(when); next(when+90);
    assert.equal(view.renderer.draws.at(-1).pulse,0);
  }
  const recovered=second+4000+800;
  assert.equal(view.onBeat(beat(13),recovered,0),true);
  next(recovered-1); assert.equal(view.renderer.draws.at(-1).pulse,0);
  next(recovered); next(recovered+90);
  assert.equal(view.renderer.draws.at(-1).pulse,1);
});

test("fast state zeros both palettes during a dissolve, including its outgoing settling frame", () => {
  const { view, next } = fieldHarness();
  view.onState(state("load",1,{hr_bpm:95}),0);
  view.onState(state("regulate",2,{hr_bpm:95,segment_elapsed_ms:9999}),0);
  view.onBeat(beat(),500,0); next(500); next(590);
  assert.ok(view.renderer.draws.at(-1).layers.every(layer=>layer.tokens.pulse>0));
  view.onState(state("regulate",3,{hr_bpm:120,segment_elapsed_ms:10000}),600);
  for(const now of [600,650,849,850]) {
    next(now);
    assert.ok(view.renderer.draws.at(-1).layers.every(layer=>layer.tokens.pulse===0));
  }
});

test("resolve combines one rate taper with the final fade; new slower beats cannot undo either", () => {
  const { view, next } = fieldHarness();
  view.onState(state("resolve",1,{hr_bpm:107.5,segment_elapsed_ms:43500}),0);
  view.onBeat(beat(1,{hr_bpm:107.5,rr_ms:60000/107.5}),500,0);
  next(500); next(590);
  assert.ok(Math.abs(view.renderer.draws.at(-1).layers[0].tokens.pulse-.04/Math.sqrt(2))<1e-12);
  view.onState(state("resolve",2,{hr_bpm:120,segment_elapsed_ms:44000}),650);
  view.onBeat(beat(2),1300,700); next(1300); next(1390);
  assert.equal(view.renderer.draws.at(-1).layers[0].tokens.pulse,0);
});

test("new session clears old beats; idle and reset clear physiology rather than hold a field", () => {
  const { view, canvas, next, frames } = fieldHarness();
  view.onState(state(), 0);
  view.onBeat(beat(), 500, 100);
  view.onState(state("baseline", 1, { session: "S-20260922-0002" }), 200);
  next(500);
  next(590);
  assert.equal(view.renderer.draws.at(-1).pulse, 0);
  assert.equal(view.onBeat(beat(2), 1000, 600), false);
  view.onState(state("reset", 2, { session: "S-20260922-0002" }), 650);
  assert.equal(canvas.hidden, true);
  assert.equal(frames.size, 0);
  assert.equal(view.renderer.clears, 1);
  view.onState(state("idle", 1, { session: "S-20260922-0003" }), 700);
  assert.equal(view.renderer.clears, 2);
  assert.deepEqual(view.layers(), []);
});

test("drawing drift never advances host crossfade or sweeps between palette hues", () => {
  const { view, next } = fieldHarness();
  view.onState(state("load", 1, { segment_elapsed_ms: 75000 }), 0);
  view.onState(state("regulate", 2, { segment_elapsed_ms: 2000 }), 10);
  const layers = view.layers();
  for (let now = 20; now < 10000; now += 100) next(now);
  assert.deepEqual(view.layers(), layers);
  assert.deepEqual(layers.map(layer => layer.weight), [.8, .2]);
  assert.ok(layers[0].tokens.hue > 200);
  assert.ok(layers[1].tokens.hue < 50);
  assert.ok(view.drift > 0);
});

test("crossfade eases only toward verified weights within 250 ms, never beyond or through hues", () => {
  const { view, next } = fieldHarness();
  view.onState(state("load", 1, { segment_elapsed_ms: 75000 }), 0);
  view.onState(state("regulate", 2, { segment_elapsed_ms: 0 }), 0);
  assert.deepEqual(view.layers().map(layer => layer.weight), [1, 0]);
  view.onState(state("regulate", 3, { segment_elapsed_ms: 2000 }), 100);
  next(225);
  assert.ok(Math.abs(view.layers()[1].weight - .1) < 1e-12);
  next(350);
  assert.equal(view.layers()[1].weight, .2);
  next(2000);
  assert.equal(view.layers()[1].weight, .2); // No host update, no extrapolation.
  view.onState(state("regulate", 4, { segment_elapsed_ms: 4000 }), 2000);
  next(2250);
  assert.equal(view.layers()[1].weight, .4);
  assert.ok(view.layers()[0].tokens.hue > 200);
  assert.ok(view.layers()[1].tokens.hue < 50);
  view.onState(state("regulate", 5, { segment_elapsed_ms: 10000 }), 2300);
  next(2550);
  assert.equal(view.layers()[1].weight, 1);
});

test("task-only standalone labels authored load appearance and cannot synthesize beats", () => {
  const { view, next } = fieldHarness();
  view.setStandalone({ ...Mapping.BASES.load, pulse: .22 }, 0);
  assert.equal(view.layers()[0].tokens.pulse, 0);
  assert.equal(view.onBeat(beat(), 500, 100), false);
  for (let now = 0; now < 1000; now += 20) next(now);
  assert.equal(view.renderer.draws.every(draw => draw.pulse === 0), true);
  assert.ok(view.drift > 0);
});

function taskHarness(search = "") {
  let now = 100;
  const elements = new Map(), intervals = new Map(), sockets = [], views = [];
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      hidden: false, dataset: {}, textContent: "", handlers: new Map(),
      addEventListener(name, fn) { this.handlers.set(name, fn); },
      getBoundingClientRect() { return { left: 0, top: 0, width: 1280, height: 720 }; },
    });
    return elements.get(id);
  }
  class ViewStub {
    constructor() { this.mapping = new Mapping.FieldState(); this.states = []; this.beats = []; this.freezes = 0; views.push(this); }
    onState(message, at) { if (!this.mapping.onState(message)) return false; this.states.push([message, at]); return true; }
    onBeat(...args) { this.beats.push(args); }
    freeze() { this.freezes++; }
    setStandalone(...args) { this.standalone = args; }
    destroy() {}
  }
  class Socket {
    static OPEN = 1;
    constructor() { this.events = new Map(); this.sent = []; this.readyState = 0; sockets.push(this); }
    addEventListener(name, fn) { this.events.set(name, fn); }
    send(raw) { this.sent.push(JSON.parse(raw)); }
    open() { this.readyState = 1; this.events.get("open")(); }
    message(message) { this.events.get("message")({ data: JSON.stringify(message) }); }
    close() { this.readyState = 3; this.events.get("close")(); }
  }
  const context = {
    PrismLoadTask: Task, PrismFieldView: { ...Field, FieldView: ViewStub },
    PrismClockSync: Clock, PrismFieldPulse: Pulse, URLSearchParams, Map,
    console: { warn() {}, info() {}, error() {} }, WebSocket: Socket,
    document: { getElementById: element, documentElement: {} },
    location: { search, protocol: "file:", hostname: "" },
    performance: { now: () => now }, addEventListener() {}, requestAnimationFrame() {},
    setInterval(fn, ms) { intervals.set(ms, fn); return ms; },
    clearInterval() {}, setTimeout() {}, clearTimeout() {},
    getComputedStyle() { return { getPropertyValue() { return "58px"; } }; },
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("../web/task/app.js"), "utf8"), context);
  return { element, intervals, sockets, view: views[0], at(value) { now = value; } };
}

test("task page draws every active segment without waiting overlay and synchronizes t_play", () => {
  const h = taskHarness();
  const socket = h.sockets[0];
  socket.open();
  assert.equal(socket.sent[0].client, "task-screen");
  assert.equal(socket.sent[1].role, "ping");
  socket.message({ type: "clock", v: 1, role: "pong", t_client_sent: 100, t_engine: 1100 });
  for (const [index, segment] of Mapping.ACTIVE_SEGMENTS.entries()) {
    socket.message(state(segment, index + 1));
    assert.equal(h.element("connection-overlay").hidden, true, segment);
  }
  socket.message(beat(1, { t_play: 1500 }));
  assert.equal(h.view.beats.length, 1);
  assert.equal(h.view.beats[0][1], 500); // T_engine offset is 1000, not arrival time 100.
  assert.equal(h.view.beats[0][2], 100);
  socket.message(beat(2, { quality: "rejected" }));
  assert.equal(h.view.beats.length, 1);
});

test("task stale state freezes visibly even while WebSocket remains open", () => {
  const h = taskHarness();
  h.sockets[0].open();
  h.sockets[0].message(state("load"));
  h.at(2700);
  h.intervals.get(200)();
  assert.ok(h.view.freezes >= 1);
  assert.equal(h.element("connection-overlay").hidden, false);
  assert.equal(h.element("connection-overlay").dataset.lost, "true");
  assert.match(h.element("overlay-title").textContent, /CONNECTION LOST.*DISPLAY FROZEN/);
});

test("task standalone never opens a connection and marks field as test-only without beats", () => {
  const h = taskHarness("?standalone=1");
  assert.equal(h.sockets.length, 0);
  assert.equal(h.view.standalone[0].pulse, 0);
  assert.match(h.element("field-mode-label").textContent, /FIXED LOAD TEST FIELD.*NO HEARTBEATS/);
});

test("both pages load the same build-free field references before their app", () => {
  for (const page of ["task", "spectator"]) {
    const html = fs.readFileSync(require.resolve(`../web/${page}/index.html`), "utf8");
    let previous = -1;
    for (const script of ["field_mapping.js", "field_pulse.js", "field_renderer.js", "field_view.js", "app.js"]) {
      const index = html.indexOf(script);
      assert.ok(index > previous, `${page}: missing/out-of-order ${script}`);
      previous = index;
    }
  }
});
