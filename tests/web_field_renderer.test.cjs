"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const Field = require("../web/shared/field_renderer.js");

const BASE = { fog: .26, light: .62, hue: 208, sat: .12, horizon: .50, pulse: .10, motion: .008 };
const PALETTES = [
  BASE,
  { fog: .20, light: .70, hue: 214, sat: .16, horizon: .50, pulse: .10, motion: .010 },
  { fog: .34, light: .95, hue: 46, sat: .30, horizon: .56, pulse: .16, motion: .030 },
  { fog: .44, light: .50, hue: 30, sat: .14, horizon: .58, pulse: .08, motion: .006 },
  { fog: .25, light: 1.05, hue: 202, sat: .30, horizon: .50, pulse: .16, motion: .007 },
  { fog: .58, light: .38, hue: 24, sat: .14, horizon: .44, pulse: .08, motion: .008 },
  { fog: .30, light: .88, hue: 208, sat: .23, horizon: .50, pulse: .13, motion: .004 },
  { fog: .20, light: .88, hue: 208, sat: .23, horizon: .50, pulse: .13, motion: .010 },
  { ...BASE, pulse: .22, fog: .72, light: .35, sat: .35, horizon: .44 },
  { ...BASE, pulse: 500, fog: 1, light: 1.1, sat: 1, horizon: .60 },
];

function assertSafe(sample) {
  const delta = sample.luminance - sample.restLuminance;
  assert.ok(delta >= -1e-12);
  assert.ok(delta <= .22 * sample.baseLuminance + 1e-12, `${delta} > .22 × ${sample.baseLuminance}`);
  assert.ok(delta <= sample.budget + 1e-12);
  assert.ok(delta <= .09 + 1e-12);
  for (let i = 0; i < 3; i += 1) {
    assert.ok(sample.rgb[i] >= sample.rest[i]);
    assert.ok(Math.abs(sample.rgb[i] * 255 - Math.round(sample.rgb[i] * 255)) < 1e-10);
  }
  for (const layer of sample.layers) {
    assert.deepEqual(layer.rest.base, layer.candidate.base, "heartbeat must not touch the base gradient");
    assert.deepEqual(layer.rest.haze, layer.candidate.haze, "heartbeat must not pulse full-field haze");
  }
}

test("quantized pulse luminance is bounded across palettes, positions, drift and amplitude", () => {
  for (const tokens of PALETTES) {
    for (const drift of [0, .43]) {
      for (const pulse of [0, .01, .1, .5, 1]) {
        for (let y = 0; y <= 1; y += 1 / 16) {
          for (let x = 0; x <= 1; x += 1 / 16) {
            assertSafe(Field.samplePixel(tokens, x, y, { pulse, drift }));
          }
        }
      }
    }
  }
});

test("cross-dissolve mixes complete palettes and retains the per-pixel safety cap", () => {
  for (const weight of [0, .1, .5, .9, 1]) {
    const layers = [{ tokens: PALETTES[1], weight }, { tokens: PALETTES[3], weight: 1 - weight }];
    for (let y = 0; y <= 1; y += 1 / 20) {
      for (let x = 0; x <= 1; x += 1 / 20) {
        assertSafe(Field.sampleLayers(layers, x, y, { pulse: 1 }));
      }
    }
    const sample = Field.sampleLayers(layers, .5, .4);
    const a = Field.samplePixel(PALETTES[1], .5, .4), b = Field.samplePixel(PALETTES[3], .5, .4);
    assert.ok(Math.abs(sample.baseLuminance - (weight * a.baseLuminance + (1 - weight) * b.baseLuminance)) < 1e-12);
  }
});

test("increasing the heartbeat envelope never darkens any channel during its rise", () => {
  for (const tokens of PALETTES) {
    for (const [x, y] of [[.5, .4], [.3, .44], [.15, .55], [.7, .62]]) {
      let previous = [0, 0, 0];
      for (let i = 0; i <= 100; i += 1) {
        const sample = Field.samplePixel(tokens, x, y, { pulse: i / 100 });
        for (let c = 0; c < 3; c += 1) assert.ok(sample.rgb[c] >= previous[c]);
        previous = sample.rgb;
      }
    }
  }
});

test("outside the fog and fixed elliptical light the heartbeat changes no pixel", () => {
  for (const tokens of PALETTES) {
    for (const [x, y] of [[0, 0], [1, 0], [0, 1], [1, 1], [.5, 1]]) {
      const rest = Field.samplePixel(tokens, x, y, { pulse: 0 });
      const peak = Field.samplePixel(tokens, x, y, { pulse: 1 });
      const active = peak.layers[0].candidate;
      if (active.fogAlpha === 0 && active.streakAlpha === 0 && active.lightAlpha === 0) {
        assert.deepEqual(peak.rgb, rest.rgb);
      }
    }
  }
});

test("fixed light is centered at .50,.40 regardless of horizon, drift or palette", () => {
  for (const tokens of PALETTES) {
    for (const horizon of [.44, .5, .58, .6]) {
      const center = Field.samplePixel({ ...tokens, horizon }, .5, .4, { drift: .8 }).layers[0].rest.lightAlpha;
      for (const [x, y] of [[.4, .4], [.6, .4], [.5, .3], [.5, .5]]) {
        const elsewhere = Field.samplePixel({ ...tokens, horizon }, x, y, { drift: .8 }).layers[0].rest.lightAlpha;
        assert.ok(center > elsewhere);
      }
    }
  }
});

test("drift moves fog streaks but neither the base nor ambient haze nor light", () => {
  const rest = Field.samplePixel(PALETTES[2], .4, .55, { drift: 0 }).layers[0].rest;
  const moved = Field.samplePixel(PALETTES[2], .4, .55, { drift: .4 }).layers[0].rest;
  assert.deepEqual(rest.base, moved.base);
  assert.deepEqual(rest.haze, moved.haze);
  assert.equal(rest.lightAlpha, moved.lightAlpha);
  assert.notEqual(rest.streakAlpha, moved.streakAlpha);
});

test("larger top-origin horizon values lower the sharp transition", () => {
  const low = Field.samplePixel({ ...BASE, horizon: .6 }, .5, .5).layers[0].rest.base;
  const high = Field.samplePixel({ ...BASE, horizon: .44 }, .5, .5).layers[0].rest.base;
  assert.ok(Field.luminance(low.map(Field.linear)) > Field.luminance(high.map(Field.linear)));
});

test("malformed input fails visibly and numeric tokens are bounded before drawing", () => {
  assert.throws(() => Field.prepareLayers([]), RangeError);
  assert.throws(() => Field.prepareLayers([{ tokens: BASE, weight: .7 }]), RangeError);
  assert.throws(() => Field.prepareLayers([{ tokens: { ...BASE, pulse: NaN }, weight: 1 }]), TypeError);
  assert.throws(() => Field.samplePixel(BASE, .5, .5, { pulse: Infinity }), TypeError);
  const [{ tokens }] = Field.prepareLayers([{ tokens: { ...BASE, pulse: 5, light: 0 }, weight: 1 }]);
  assert.equal(tokens.pulse, .22); assert.equal(tokens.light, .35);
  const [{ tokens: bounded }] = Field.prepareLayers([{ tokens: Object.fromEntries(Field.KEYS.map((key) => [key, 1000])), weight: 1 }]);
  for (const key of Field.KEYS) assert.equal(bounded[key], Field.LIMITS[key][1]);
  assert.equal(Field.prepareLayers([{ tokens: { ...BASE, horizon: .38 }, weight: 1 }])[0].tokens.horizon, .38);
});
