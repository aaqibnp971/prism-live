"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const Task = require("../web/task/task.js");
const fs = require("node:fs");
const vm = require("node:vm");

function close(actual, expected, tolerance = 1e-9) {
  assert.ok(
    Math.abs(actual - expected) <= tolerance,
    `expected ${actual} to be within ${tolerance} of ${expected}`,
  );
}

function taskShell(search, width, scale = 1) {
  const elements = new Map();
  const listeners = new Map();
  const viewport = { width };
  let fullscreenCalls = 0;
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      hidden: false, dataset: {}, textContent: "", handlers: new Map(),
      addEventListener(name, handler) { this.handlers.set(name, handler); },
      getBoundingClientRect() { return { width: viewport.width, height: 800 }; },
      requestFullscreen() { fullscreenCalls++; return Promise.resolve(); },
    });
    return elements.get(id);
  }
  const context = {
    PrismLoadTask: Task, URLSearchParams, Map, console,
    document: { getElementById: element, documentElement: {} },
    location: { search, protocol: "file:", hostname: "" },
    devicePixelRatio: scale,
    addEventListener(name, handler) { listeners.set(name, handler); },
    requestAnimationFrame() {},
    getComputedStyle() { return { getPropertyValue() { return "58px"; } }; },
    WebSocket: class { addEventListener() {} },
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("../web/task/app.js"), "utf8"), context);
  return { element, listeners, viewport, fullscreenCalls: () => fullscreenCalls };
}

test("shared laptop task measures physical viewport and cannot cover the console", () => {
  const shell = taskShell("?tiled=1&monitor_width_cm=60&monitor_width_px=1920", 960, 1.25);
  assert.equal(shell.element("fullscreen-button").hidden, true);
  shell.element("fullscreen-button").handlers.get("click")();
  assert.equal(shell.fullscreenCalls(), 0);
  assert.match(shell.element("geometry-label").textContent, /37\.5 cm screen width/);
  assert.match(shell.element("fov-label").textContent, /viewport-scaled geometry/);
  shell.viewport.width = 768;
  shell.listeners.get("resize")();
  assert.match(shell.element("geometry-label").textContent, /30\.0 cm screen width/);
});

test("ordinary task page retains its full-screen control and configured geometry", () => {
  const shell = taskShell("?screen_width_cm=59.77", 1920);
  assert.equal(shell.element("fullscreen-button").hidden, false);
  shell.element("fullscreen-button").handlers.get("click")();
  assert.equal(shell.fullscreenCalls(), 1);
  assert.match(shell.element("geometry-label").textContent, /59\.8 cm screen width/);
  assert.match(shell.element("fov-label").textContent, /full-screen width assumed/);
});

test("27-inch 16:9 default and projection use the configured physical geometry", () => {
  close(Task.DEFAULT_SCREEN_WIDTH_CM, 59.768, 0.01);
  const geometry = Task.createGeometry();
  const fov = Task.horizontalFovDeg(geometry);
  assert.ok(fov > 52.8 && fov < 53.0);
  close(Task.angleToPixel(-fov / 2, 1920, geometry), 0, 1e-9);
  close(Task.angleToPixel(0, 1920, geometry), 960, 1e-9);
  close(Task.angleToPixel(fov / 2, 1920, geometry), 1920, 1e-9);
  close(Task.pixelToAngle(1920, 1920, geometry), fov / 2, 1e-9);
  const centerSpeed = Task.pixelsPerSecond(0, 6, 1920, geometry);
  assert.ok(centerSpeed > 201 && centerSpeed < 203);
});

test("the one 75-second ramp has exact endpoints and integer distractor steps", () => {
  assert.deepEqual(Task.rampAt(0), {
    difficulty: 0,
    dwellMs: 900,
    nominalSplitIntervalMs: 5500,
    splitIntervalMs: 5500,
    distractors: 1,
    angularSpeedDegPerSecond: 6,
  });
  assert.deepEqual(Task.rampAt(37_500), {
    difficulty: 0.5,
    dwellMs: 660,
    nominalSplitIntervalMs: 3850,
    splitIntervalMs: 3850,
    distractors: 2,
    angularSpeedDegPerSecond: 12.5,
  });
  assert.deepEqual(Task.rampAt(75_000), {
    difficulty: 1,
    dwellMs: 420,
    nominalSplitIntervalMs: 2200,
    splitIntervalMs: 2200,
    distractors: 3,
    angularSpeedDegPerSecond: 19,
  });
});

test("misses remove 400 ms cumulatively and never cross the 1.8-second floor", () => {
  assert.equal(Task.rampAt(0, 2).splitIntervalMs, 4700);
  assert.equal(Task.rampAt(75_000, 1).splitIntervalMs, 1800);
  assert.equal(Task.rampAt(75_000, 20).splitIntervalMs, 1800);
});

test("motion integrates in angular coordinates and reflects without leaving its bound", () => {
  const task = new Task.LoadTask({ emit() {}, random: () => 0 });
  task.setMotionLimitDegrees(10);
  task.start({ session: "S-20260919-0001", nowMs: 0 });
  task.tick(1000);
  const expectedTravel = ((6 + Task.rampAt(1000).angularSpeedDegPerSecond) / 2) * 1;
  close(task.angleDeg, -8 + expectedTravel, 1e-9);
  task.tick(3000);
  assert.ok(task.angleDeg >= -10 && task.angleDeg <= 10);
  assert.equal(task.direction, -1);
});

test("split, abandon, lock and wrong-target miss use contract-shaped events", () => {
  const events = [];
  const task = new Task.LoadTask({ emit: (event) => events.push(event), random: () => 0 });
  task.start({ session: "S-20260919-0002", nowMs: 0 });
  task.tick(5500);
  assert.equal(events.at(-1).event, "split");
  assert.equal(task.round.decoys.length, 1);

  task.tick(5501, "moving");
  task.tick(5601, null);
  assert.equal(events.at(-1).event, "abandon");
  task.tick(5602, "moving");
  task.tick(6500, "moving");
  assert.equal(events.at(-1).event, "lock");
  assert.equal(task.round.outcome, "lock");

  for (const event of events) {
    assert.deepEqual(Object.keys(event).sort(), [
      "difficulty",
      "dwell_ms",
      "event",
      "session",
      "split_interval_ms",
      "t_client",
      "type",
      "v",
    ]);
    assert.equal(event.type, "task_event");
    assert.equal(event.v, 1);
    assert.equal(event.session, "S-20260919-0002");
    assert.ok(event.difficulty >= 0 && event.difficulty <= 1);
  }

  const misses = [];
  const wrong = new Task.LoadTask({ emit: (event) => misses.push(event), random: () => 0 });
  wrong.start({ session: "S-20260919-0003", nowMs: 0 });
  wrong.tick(5500);
  wrong.tick(5501, "decoy-0");
  wrong.tick(6500, "decoy-0");
  assert.equal(misses.at(-1).event, "miss");
  assert.equal(wrong.misses, 1);
  assert.equal(wrong.round.outcome, "miss");
});

test("leaving a distractor before dwell emits abandon as active pursuit", () => {
  const events = [];
  const task = new Task.LoadTask({ emit: (event) => events.push(event), random: () => 0 });
  task.start({ session: "S-20260919-0007", nowMs: 0 });
  task.tick(5500);
  task.tick(5501, "decoy-0");
  task.tick(5601, null);
  assert.deepEqual(events.map((event) => event.event), ["split", "abandon"]);
});

test("the live gate starts only on host LOAD, freezes, resumes, and stops on exit", () => {
  const task = new Task.LoadTask({ emit() {} });
  const gate = new Task.StateGate(task);
  const state = (segment, elapsed = 0) => ({
    type: "state",
    session: "S-20260919-0004",
    segment,
    segment_elapsed_ms: elapsed,
  });

  assert.equal(gate.apply(state("baseline"), 100), false);
  assert.equal(task.active, false);
  assert.equal(gate.apply(state("load", 250), 200), true);
  assert.equal(task.active, true);
  assert.equal(task.elapsedAt(200), 250);
  gate.disconnect(300);
  assert.equal(task.paused, true);
  assert.equal(gate.apply(state("load", 1000), 400), true);
  assert.equal(task.paused, false);
  assert.equal(task.elapsedAt(400), 1000);
  assert.equal(gate.apply(state("regulate"), 500), false);
  assert.equal(task.active, false);
});

test("the task ends at 75 seconds", () => {
  const task = new Task.LoadTask({ emit() {} });
  task.start({ session: "S-20260919-0005", nowMs: 10 });
  const end = task.tick(75_010);
  assert.equal(end.active, false);
  assert.equal(end.complete, true);
  assert.equal(end.elapsedMs, 75_000);
});

test("a frame-stepped unattended run covers the full ramp without overrunning", () => {
  const events = [];
  const task = new Task.LoadTask({ emit: (event) => events.push(event), random: () => 0.5 });
  task.start({ session: "S-20260919-0006", nowMs: 0 });
  let snapshot;
  for (let nowMs = 0; nowMs <= 75_020; nowMs += 20) snapshot = task.tick(nowMs);
  assert.equal(snapshot.complete, true);
  assert.equal(snapshot.elapsedMs, 75_000);
  assert.ok(events.filter((event) => event.event === "split").length > 20);
  assert.ok(events.every((event) => event.t_client <= 75_000));
  assert.equal(events.some((event) => event.difficulty < 0 || event.difficulty > 1), false);
});
