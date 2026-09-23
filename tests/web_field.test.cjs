"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const Mapping = require("../web/shared/field_mapping.js");
const Pulse = require("../web/shared/field_pulse.js");
const Drawing = require("../web/shared/field_renderer.js");
const SESSION = "S-20260922-0001";
const close = (a, b, epsilon = 1e-10) => assert.ok(Math.abs(a - b) <= epsilon, `${a} != ${b}`);
function state(segment = "baseline", extra = {}) {
  return { type: "state", v: 1, session: SESSION, seq: 1, t_engine: 1000,
    t_session: segment === "idle" ? null : 0, segment, segment_elapsed_ms: 0,
    segment_nominal_ms: segment === "idle" ? 0 : segment === "load" || segment === "regulate" ? 75000 : 45000,
    psv: { arousal: .5, cognitive_load: .5, readiness: .5, valence: .5 },
    confidence: { arousal: 0, cognitive_load: 0, readiness: 0, valence: 0 },
    authority: { arousal: 0, cognitive_load: 0, readiness: 0, valence: 0 }, hr_bpm: 95, ...extra };
}
function beat(seq, time, bpm = 95, extra = {}) {
  return { type: "beat", v: 1, session: SESSION, seq, t_play: time,
    rr_ms: 60000 / bpm, hr_bpm: bpm, quality: "ok", ...extra };
}
const authority = (arousal = 1, cognitive_load = 1) => ({ arousal, cognitive_load, readiness: 0, valence: 0 });

test("baseline clears from three actual confidences, never time, baseline_quality or valence", () => {
  const opening = Mapping.mapState(state());
  close(opening.fog, .38); close(opening.light, .45); close(opening.horizon, .44);
  assert.deepEqual(Mapping.mapState(state("baseline", { segment_elapsed_ms: 56000 })), opening);
  const cleared = Mapping.mapState(state("baseline", { confidence: { arousal: .393, cognitive_load: .393, readiness: .393, valence: 0 }, signal: { baseline_quality: 0 } }));
  close(cleared.fog, .26); close(cleared.light, .62); close(cleared.horizon, .50);
  const excess = Mapping.mapState(state("baseline", { confidence: { arousal: 1, cognitive_load: 1, readiness: 1, valence: 0 } }));
  assert.deepEqual(excess, cleared);
});
test("host authority is applied once, not inferred from confidence or multiplied by .20 again", () => {
  const s = state("load", { authority: authority(.2, .2), psv: { arousal: 1, cognitive_load: 1, readiness: 0, valence: .5 } });
  const t = Mapping.mapState(s);
  close(t.hue, 206.8); close(t.light, .91); close(t.sat, .244); close(t.fog, .26); close(t.motion, .0064);
  const changedConfidence = { ...s, confidence: { arousal: 1, cognitive_load: 1, readiness: 1, valence: 0 } };
  assert.deepEqual(Mapping.mapState(changedConfidence), t);
  const zero = { ...s, authority: authority(0, 0) };
  assert.deepEqual(Mapping.mapState(zero), Mapping.mapState({ ...zero, psv: { ...s.psv, arousal: 0, cognitive_load: 0 } }));
});
test("regulate moves toward warm dim thick still as body arousal and load fall, not with a timer", () => {
  const base = state("regulate", { authority: authority() });
  const high = Mapping.mapState({ ...base, psv: { ...base.psv, arousal: 1, cognitive_load: 1 } });
  const low = Mapping.mapState({ ...base, psv: { ...base.psv, arousal: 0, cognitive_load: 0 }, hr_bpm: 62 });
  assert.deepEqual(high, { fog: .34, light: .95, hue: 46, sat: .30, horizon: .56, pulse: .16, motion: .030 });
  close(low.fog, .58); close(low.light, .38); close(low.hue, 24); close(low.sat, .14); close(low.horizon, .44); close(low.pulse, .08);
  assert.deepEqual(Mapping.mapState({ ...base, segment_elapsed_ms: 105000 }), Mapping.mapState(base));
});
test("resolve taper uses observed host authority ratios and opening uses host remaining time", () => {
  const tracker = new Mapping.FieldState();
  const high = state("regulate", { authority: authority(), psv: { arousal: 1, cognitive_load: 1, readiness: .5, valence: .5 } });
  assert.equal(tracker.onState(high), true);
  assert.equal(tracker.onState(state("resolve", { seq: 2, t_engine: 2000, authority: authority(.8, .6) })), true);
  close(tracker.tokens.light, .95);
  tracker.onState(state("resolve", { seq: 3, t_engine: 18000, segment_elapsed_ms: 16500, authority: authority(.4, .3) }));
  close(tracker.tokens.light, (.95 + .50) / 2); close(tracker.tokens.hue, 38); close(tracker.tokens.sat, .22);
  tracker.onState(state("resolve", { seq: 4, t_engine: 40000, segment_elapsed_ms: 33000 }));
  close(tracker.tokens.light, .5); close(tracker.tokens.hue, 30); close(tracker.tokens.horizon, .58);
  for (const [elapsed, horizon] of [[25000,.44],[29000,.51],[33000,.58]]) {
    close(Mapping.mapState(state("resolve", { segment_elapsed_ms: elapsed })).horizon, horizon);
  }
  close(Mapping.mapState(state("resolve", { segment_elapsed_ms: 43500 })).pulse, .08 / Math.sqrt(2));
  close(Mapping.mapState(state("resolve", { segment_elapsed_ms: 45000 })).pulse, 0);
  close(Mapping.mapState(state("resolve", { segment_elapsed_ms: 50000 })).pulse, 0);
});
test("separate palettes dissolve in ten HOST seconds; never sweep hue across green", () => {
  const tracker = new Mapping.FieldState();
  tracker.onState(state("load"));
  tracker.onState(state("regulate", { seq: 2, t_engine: 2000, segment_elapsed_ms: 5000 }));
  const layers = tracker.layers();
  assert.equal(layers.length, 2); close(layers[0].weight, .5); close(layers[1].weight, .5);
  assert.ok(layers[0].tokens.hue >= 202); assert.ok(layers[1].tokens.hue <= 46);
  // Calling repeatedly without a host message cannot advance segment/dissolve progress.
  assert.deepEqual(tracker.layers(), layers);
  tracker.onState(state("regulate", { seq: 3, t_engine: 10000, segment_elapsed_ms: 10000 }));
  assert.equal(tracker.layers().length, 1);
});
test("new session, cold/reset/idle and mid-resolve join never borrow another visitor's state", () => {
  const tracker = new Mapping.FieldState();
  assert.deepEqual(tracker.layers(), []);
  tracker.onState(state("regulate"));
  tracker.onState(state("resolve", { session: "S-20260922-0002", segment_elapsed_ms: 30000 }));
  assert.equal(tracker.resolveEntry, null); close(tracker.tokens.light, .50); assert.equal(tracker.layers().length, 1);
  tracker.onState(state("reset", { session: "S-20260922-0002", seq: 2 }));
  assert.deepEqual(tracker.layers(), []);
  tracker.onState(state("idle", { session: "S-20260922-0003" }));
  assert.deepEqual(tracker.layers(), []);
  tracker.onState(state("baseline", { session: "S-20260922-0003", seq: 2 }));
  close(tracker.tokens.fog, .38);
});
test("invalid, duplicate, regressing host states leave field untouched", () => {
  const tracker = new Mapping.FieldState();
  tracker.onState(state());
  const old = tracker.layers();
  assert.equal(tracker.onState(state()), false);
  assert.equal(tracker.onState(state("load", { seq: 2, t_engine: 999 })), false);
  assert.equal(tracker.onState(state("load", { seq: 2, authority: authority(NaN) })), false);
  assert.equal(tracker.onState(state("load", { seq: 2, authority: { ...authority(), valence: .1 } })), false);
  assert.deepEqual(tracker.layers(), old);
});
test("every integer45..180 preserves authored amplitudes then linearly tapers95..120 in every segment", () => {
  for (const segment of Mapping.ACTIVE_SEGMENTS) {
    const top = Mapping.mapState(state(segment)).pulse;
    const low = { baseline: .06, load: .10, regulate: .08, resolve: .08 }[segment];
    for (const bpm of [...Array.from({length:136},(_,i)=>i+45),94.999,95.001,107.5,119.999,120.001]) {
      const gain = bpm <= 95 ? 1 : bpm >= 120 ? 0 : (120-bpm)/25;
      const authored = low + (top-low) * Math.max(0,Math.min(1,(bpm-62)/33));
      close(Mapping.mapState(state(segment, { hr_bpm: bpm })).pulse, authored*gain);
    }
    close(Mapping.mapState(state(segment, { hr_bpm: 107.5 })).pulse, top/2);
    assert.equal(Mapping.mapState(state(segment, { hr_bpm: null })).pulse, 0);
  }
  for (const hr_bpm of [null,NaN,Infinity,0,-1]) assert.equal(Mapping.visualRateGain(hr_bpm),0);
  close(Mapping.mapState(state("resolve",{hr_bpm:107.5,segment_elapsed_ms:43500})).pulse,.04/Math.sqrt(2));
  assert.equal(Mapping.mapState(state("resolve",{hr_bpm:120,segment_elapsed_ms:43500})).pulse,0);
});
test("all valid mapped states respect seven token ranges and leave light above horizon", () => {
  for (const segment of Mapping.ACTIVE_SEGMENTS) for (const p of [0,.32,.5,1]) for (const a of [0,.2,1]) for (const elapsed of [0,25000,33000,42000,45000,105000]) {
    const t = Mapping.mapState(state(segment, { psv: { arousal: p, cognitive_load: 1-p, valence: .5, readiness: p }, authority: authority(a,a), segment_elapsed_ms: elapsed }));
    assert.deepEqual(Object.keys(t).sort(), [...Mapping.KEYS].sort());
    for (const [key,min,max] of [["fog",.18,.62],["light",.35,1.1],["hue",18,216],["sat",.08,.36],["horizon",.44,.62],["pulse",0,.22],["motion",.004,.045]]) assert.ok(t[key] >= min-1e-10 && t[key] <= max+1e-10, `${segment}.${key}=${t[key]}`);
    assert.ok(.40 < t.horizon);
  }
});

test("90 ms rise is monotone, peaks exactly at90, falls tozero at270", () => {
  assert.equal(Pulse.RISE_MS, 90);
  let last = 0;
  for (let age = 0; age <= 90; age++) { const value = Pulse.envelope(age); assert.ok(value >= last); last = value; }
  close(Pulse.envelope(90), 1);
  for (let age = 90; age <= 270; age++) { const value = Pulse.envelope(age); assert.ok(value <= last); last = value; }
  assert.equal(last, 0); assert.equal(Pulse.envelope(-1), 0); assert.equal(Pulse.envelope(Infinity), 0);
});
test("every integer rate45..180: future-only, full90msrise below120, zero at120+, no overlap", () => {
  for (let bpm = 45; bpm <= 180; bpm++) {
    const pulse = new Pulse.BeatPulse(); pulse.reset(SESSION);
    const starts = Array.from({length: 12}, (_, i) => Math.round(500 + i * 60000 / bpm));
    starts.forEach((time, i) => assert.equal(pulse.schedule(beat(i+1,time,bpm),time,0),bpm<120, `${bpm} bpm seq${i+1}`));
    for (let now = 0; now <= starts.at(-1) + 300; now++) {
      const value = pulse.value(now);
      const active = starts.filter((time) => now >= time && now < time+270);
      assert.ok(active.length <= 1);
      close(value, bpm<120 && active.length ? Pulse.envelope(now-active[0]) : 0);
      assert.ok(value >= 0 && value <= 1);
    }
    assert.equal(pulse.stats.late, 0); assert.equal(pulse.stats.rate_limited, bpm<120 ? 0 : starts.length);
  }
});
test("latearrival/rejected/duplicate/wrongsession neverflash; frozen renderer nevercatchesup", () => {
  const pulse = new Pulse.BeatPulse(); pulse.reset(SESSION);
  assert.equal(pulse.schedule(beat(1,500),500,501),false);
  assert.equal(pulse.schedule(beat(2,700,95,{quality:"rejected"}),700,0),false);
  assert.equal(pulse.schedule(beat(3,1000,95,{session:"S-20260922-9999"}),1000,0),false);
  assert.equal(pulse.value(700),0);
  assert.equal(pulse.schedule(beat(3,1000),1000,0),true);
  assert.equal(pulse.schedule(beat(3,1000),1000,0),false);
  assert.equal(pulse.value(999),0); assert.equal(pulse.value(1090),0); // RAF missed onset >45ms.
  assert.equal(pulse.stats.late,2);
  assert.equal(pulse.schedule(beat(4,2000),2000,1500),true);
  pulse.value(2000); close(pulse.value(2090),1);
  pulse.clear(); assert.equal(pulse.value(2100),0);
});
test("unexpected faster stream suppresses visuals instead of stacking or speeding the rise", () => {
  const pulse = new Pulse.BeatPulse(); pulse.reset(SESSION);
  assert.equal(pulse.schedule(beat(1,500,200),500,0),false);
  assert.equal(pulse.schedule(beat(2,1200,95),1200,0),true);
  assert.equal(pulse.schedule(beat(3,1201,95),1201,0),false);
  assert.equal(pulse.stats.rate_limited,2);
  pulse.value(1200); close(pulse.value(1290),1);
  pulse.reset("S-20260922-0002"); assert.equal(pulse.value(1291),0);
});

test("HR, RR and scheduled cadence each independently enforce120 cutoff; no every-other-beat substitute", () => {
  for (const extra of [{hr_bpm:120,rr_ms:800},{hr_bpm:75,rr_ms:500}]) {
    const pulse = new Pulse.BeatPulse(); pulse.reset(SESSION);
    assert.equal(pulse.schedule(beat(1,500,75,extra),500,0),false);
    assert.equal(pulse.value(500),0); assert.equal(pulse.value(590),0);
  }
  for (const interval of [500,400,60000/180]) {
    const pulse = new Pulse.BeatPulse(); pulse.reset(SESSION);
    // Even inconsistent low-HR/long-RR metadata cannot turn a fast cadence into flashes.
    for(let i=0;i<20;i++) {
      const time=500+i*interval;
      assert.equal(pulse.schedule(beat(i+1,time,75),time,0),i===0);
      pulse.value(time); close(pulse.value(time+90),i===0 ? 1 : 0);
    }
    const recovered=500+19*interval+800;
    assert.equal(pulse.schedule(beat(21,recovered,75),recovered,0),true);
    pulse.value(recovered); close(pulse.value(recovered+90),1);
  }
});

test("45..180 bpm scheduled envelopes obey the rendered luminance rail in every segment", () => {
  for (let bpm = 45; bpm <= 180; bpm++) {
    const pulse = new Pulse.BeatPulse(); pulse.reset(SESSION);
    const starts = [500, 500 + 60000 / bpm, 500 + 120000 / bpm];
    starts.forEach((start, index) => assert.equal(pulse.schedule(beat(index + 1, start, bpm), start, 0), bpm<120));
    for (const start of starts) {
      for (const age of [0, 45, 90, 180, 270]) {
        const strength = pulse.value(start + age);
        for (const segment of Mapping.ACTIVE_SEGMENTS) {
          const tokens = Mapping.mapState(state(segment, { hr_bpm: bpm, authority: authority() }));
          for (const [x,y] of [[.5,.4], [.2,.5], [.8,.6], [0,1]]) {
            const pixel = Drawing.samplePixel(tokens, x, y, { pulse: strength });
            const change = pixel.luminance - pixel.restLuminance;
            assert.ok(change <= .22 * pixel.baseLuminance + 1e-12);
            assert.ok(change <= .09 + 1e-12);
            assert.deepEqual(pixel.layers[0].rest.base, pixel.layers[0].candidate.base);
            assert.deepEqual(pixel.layers[0].rest.haze, pixel.layers[0].candidate.haze);
            if(bpm>=120) { assert.equal(strength,0); assert.equal(change,0); assert.deepEqual(pixel.rgb,pixel.rest); }
          }
        }
      }
    }
  }
});
