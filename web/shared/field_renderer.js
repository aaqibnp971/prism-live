/* Plain-JS drawing reference for Unity: seven tokens in, a field out.
 * The extracted component in docs/design/field-frames.html supplies the look.
 * No state, clock, beat scheduling, authority, or segment decisions live here.
 * Coordinates are top-origin. The light centre is always (0.50, 0.40).
 * Pulse changes local fog/streaks/light, NEVER base gradient or ambient haze.
 * Its positive linear-sRGB luminance delta is capped against the darker base
 * gradient, including after layer blending and 8-bit framebuffer quantization.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.PrismFieldRenderer = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const KEYS = Object.freeze(["fog", "light", "hue", "sat", "horizon", "pulse", "motion"]);
  const MAX_LAYERS = 4;
  const MAX_PULSE = 0.22;
  const ABSOLUTE_PULSE_LIMIT = 0.09;
  // Global token bounds from experience-script.md §4, shared with the port.
  // Horizon .38 is allowed by the sheet but would put the light below ground;
  // the authored state mapping stays >= .44 (known-limits records the risk).
  const LIMITS = Object.freeze({ fog: [0.18, 0.62], light: [0.35, 1.10],
    hue: [18, 216], sat: [0.08, 0.36], horizon: [0.38, 0.62],
    pulse: [0, MAX_PULSE], motion: [0.004, 0.045] });
  const clamp = (n, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, n));
  const mix = (a, b, t) => a.map((n, i) => n + (b[i] - n) * t);
  const linear = (n) => n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4;
  const encoded = (n) => n <= 0.0031308 ? 12.92 * n : 1.055 * n ** (1 / 2.4) - 0.055;
  const luminance = (rgb) => 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
  const roundAlpha = (n) => Math.round(n * 1000) / 1000;

  function hsv(h, s, v) {
    const hue = ((h % 360) + 360) % 360;
    const c = v * s, x = c * (1 - Math.abs((hue / 60) % 2 - 1)), m = v - c;
    const rgb = hue < 60 ? [c, x, 0] : hue < 120 ? [x, c, 0] : hue < 180 ? [0, c, x]
      : hue < 240 ? [0, x, c] : hue < 300 ? [x, 0, c] : [c, 0, x];
    return rgb.map((n) => Math.round(clamp(n + m) * 255) / 255);
  }

  function stops(x, points) {
    if (x <= points[0][0]) return points[0][1];
    for (let i = 1; i < points.length; i += 1) {
      if (x <= points[i][0]) {
        const t = clamp((x - points[i - 1][0]) / (points[i][0] - points[i - 1][0]));
        return points[i - 1][1] + t * (points[i][1] - points[i - 1][1]);
      }
    }
    return points[points.length - 1][1];
  }

  function prepareLayers(layers) {
    if (!Array.isArray(layers) || layers.length < 1 || layers.length > MAX_LAYERS) {
      throw new RangeError(`The field needs 1–${MAX_LAYERS} palette layers`);
    }
    let sum = 0;
    const result = layers.map(({ tokens, weight }) => {
      if (!tokens || KEYS.some((key) => !Number.isFinite(tokens[key]))) {
        throw new TypeError("All seven field tokens must be finite numbers");
      }
      if (!Number.isFinite(weight) || weight < 0) throw new RangeError("Invalid field layer weight");
      sum += weight;
      return { tokens: Object.fromEntries(KEYS.map((key) =>
        [key, clamp(tokens[key], ...LIMITS[key])])), weight };
    });
    if (Math.abs(sum - 1) > 1e-6) throw new RangeError("Field layer weights must sum to one");
    // Normalize float accumulation error, not an invalid controller decision.
    return result.map((layer) => ({ ...layer, weight: layer.weight / sum }));
  }

  function streakAt(position) {
    const q = ((position % 40) + 40) % 40;
    return stops(q, [[0, 0], [9, 1], [18, 0], [31, 0.5], [40, 0]]);
  }

  function localLayer(tokens, x, y, width, height, pulse, drift) {
    const { hue, sat, fog, light, horizon, motion } = tokens;
    const value = 0.30 + light * 0.44;
    const colors = [hsv(hue, sat, value - 0.07), hsv(hue, sat * 0.9, value),
      hsv(hue, sat * 1.25, value - 0.15), hsv(hue, sat * 1.3, value - 0.24)];
    const ys = [0, horizon - 0.012, horizon + 0.012, 1];
    let base = colors[3];
    for (let i = 1; i < 4; i += 1) {
      if (y <= ys[i]) { base = mix(colors[i - 1], colors[i], clamp((y - ys[i - 1]) / (ys[i] - ys[i - 1]))); break; }
    }
    const k = tokens.pulse * pulse;
    const fa = Math.min(0.95, fog * (1 + k * 0.5));
    const fogColor = hsv(hue, sat * 0.45, Math.min(0.97, (value + 0.2) * (1 + k * 0.6)));
    // The reference pulses its full-field haze too. Deliberately hold that
    // layer at rest: the brief forbids a full-field heartbeat flash.
    const haze = mix(base, hsv(hue, sat * 0.45, Math.min(0.97, value + 0.2)), roundAlpha(Math.min(0.95, fog) * 0.32));
    // Drift moves the horizontal fog striation, never the fixed light centre.
    const fogAlpha = stops(y, [[horizon - 0.34, 0], [horizon - 0.08, roundAlpha(fa * 0.55)],
      [horizon, roundAlpha(fa)], [horizon + 0.10, roundAlpha(fa * 0.7)], [horizon + 0.34, 0]]);
    const sa = roundAlpha(Math.min(0.5, motion * 7) * fa);
    const radians = Math.PI / 180;
    const lineLength = Math.abs(width * Math.sin(radians)) + Math.abs(height * Math.cos(radians));
    const position = (x - 0.5) * width * Math.sin(radians) + (y - 0.5) * height * Math.cos(radians)
      + lineLength / 2 + drift * height;
    // A normalized nine-tap Gaussian approximates CSS blur(2.5px), keeping
    // the gradient procedural at any canvas size without texture assets.
    let band = 0;
    let normal = 0;
    for (let i = -4; i <= 4; i += 1) {
      const w = Math.exp(-0.5 * (i / 1.25) ** 2);
      band += w * streakAt(position + i * 2); normal += w;
    }
    const mask = stops(y, [[horizon - 0.22, 0], [horizon - 0.04, 1],
      [horizon + 0.06, 1], [horizon + 0.20, 0]]);
    const streakAlpha = sa * band / normal * mask;
    // Preserve the component's DOM stacking: base, haze, streak, fog, light.
    const streaked = mix(haze, fogColor, streakAlpha);
    const fogged = mix(streaked, fogColor, fogAlpha);
    const radius = Math.hypot((x - 0.5) / 0.58, (y - 0.4) / 0.46);
    const la = Math.min(1, light * 0.78 * (1 + k));
    const lightAlpha = stops(radius, [[0, roundAlpha(la)], [0.30, roundAlpha(la * 0.55)],
      [0.60, roundAlpha(la * 0.18)], [1, 0]]);
    const lightColor = hsv(hue, sat * 0.35, Math.min(0.98, value + 0.28 + k * 0.1));
    return { base, haze, fogAlpha, streakAlpha, lightAlpha, color: mix(fogged, lightColor, lightAlpha) };
  }

  /** CPU oracle for tests/Unity ports; actual browser drawing uses the shader. */
  function sampleLayers(layers, x, y, { pulse = 0, drift = 0, width = 272, height = 272 } = {}) {
    if (![x, y, pulse, drift, width, height].every(Number.isFinite) || width <= 0 || height <= 0) {
      throw new TypeError("Invalid field sample coordinates/options");
    }
    const prepared = prepareLayers(layers);
    let rest = [0, 0, 0], candidate = [0, 0, 0], base = [0, 0, 0], budget = 0;
    const details = prepared.map(({ tokens, weight }) => {
      const a = localLayer(tokens, x, y, width, height, 0, drift);
      // Compute fixed rest/peak images, then scale their linear-light delta.
      // Recomputing rounded HSV colours at every envelope step can introduce
      // tiny downward steps during the 90 ms rise; fixed endpoints cannot.
      const b = localLayer(tokens, x, y, width, height, 1, drift);
      const baseLinear = a.base.map(linear), restLinear = a.color.map(linear);
      const difference = b.color.map((n, i) => Math.max(0, linear(n) - restLinear[i]));
      const cap = Math.min(tokens.pulse * luminance(baseLinear), ABSOLUTE_PULSE_LIMIT);
      const scale = Math.min(1, cap / Math.max(1e-12, luminance(difference)));
      for (let i = 0; i < 3; i += 1) {
        base[i] += weight * baseLinear[i]; rest[i] += weight * restLinear[i];
        candidate[i] += weight * (restLinear[i] + difference[i] * scale);
      }
      budget += weight * cap;
      return { rest: a, candidate: b, cap };
    });
    // Quantization is part of the safety proof: derive the delta from the
    // displayed rest pixel, floor every positive channel, and disable dither.
    const restEncoded = rest.map((n) => Math.floor(clamp(encoded(n)) * 255 + 0.5) / 255);
    const restQuantized = restEncoded.map(linear);
    const delta = candidate.map((n, i) => Math.max(0, n - restQuantized[i]));
    const scale = Math.min(1, budget / Math.max(1e-12, luminance(delta)));
    const rgb = pulse <= 0 ? restEncoded : delta.map((n, i) => Math.max(restEncoded[i],
      Math.floor(clamp(encoded(restQuantized[i] + n * scale * clamp(pulse))) * 255 + 1e-7) / 255));
    return { rgb, rest: restEncoded, base, budget, luminance: luminance(rgb.map(linear)),
      restLuminance: luminance(restQuantized), baseLuminance: luminance(base), layers: details };
  }

  const VERTEX = `attribute vec2 a_position; varying vec2 v_uv;
    void main() { v_uv = a_position * 0.5 + 0.5; gl_Position = vec4(a_position,0.0,1.0); }`;
  const FRAGMENT = `precision highp float;
    varying vec2 v_uv;
    uniform vec2 u_size;
    uniform vec4 u_tokensA[4]; // fog, light, hue, saturation
    uniform vec4 u_tokensB[4]; // horizon, pulse, motion, weight
    uniform float u_pulse;
    uniform float u_drift;
    uniform int u_count;
    float alpha(float x) { return floor(x*1000.0+0.5)/1000.0; }
    float lin(float x) { return x<=0.04045 ? x/12.92 : pow((x+0.055)/1.055,2.4); }
    vec3 linearRGB(vec3 x) { return vec3(lin(x.r),lin(x.g),lin(x.b)); }
    float enc(float x) { return x<=0.0031308 ? 12.92*x : 1.055*pow(x,1.0/2.4)-0.055; }
    vec3 encodedRGB(vec3 x) { return clamp(vec3(enc(x.r),enc(x.g),enc(x.b)),0.0,1.0); }
    float lum(vec3 c) { return dot(c,vec3(0.2126,0.7152,0.0722)); }
    vec3 hsv(float h,float s,float v) {
      float c=v*s; float x=c*(1.0-abs(mod(h/60.0,2.0)-1.0)); float m=v-c;
      vec3 rgb;
      if(h<60.0) rgb=vec3(c,x,0.0); else if(h<120.0) rgb=vec3(x,c,0.0);
      else if(h<180.0) rgb=vec3(0.0,c,x); else if(h<240.0) rgb=vec3(0.0,x,c);
      else if(h<300.0) rgb=vec3(x,0.0,c); else rgb=vec3(c,0.0,x);
      return floor(clamp(rgb+m,0.0,1.0)*255.0+0.5)/255.0;
    }
    float ramp(float x,float a,float b) { return clamp((x-a)/(b-a),0.0,1.0); }
    vec3 baseField(vec4 a,vec4 b,vec2 uv) {
      float V=.30+a.y*.44; float H=b.x;
      vec3 c0=hsv(a.z,a.w,V-.07), c1=hsv(a.z,a.w*.9,V);
      vec3 c2=hsv(a.z,a.w*1.25,V-.15), c3=hsv(a.z,a.w*1.3,V-.24);
      if(uv.y<=H-.012) return mix(c0,c1,ramp(uv.y,0.0,H-.012));
      if(uv.y<=H+.012) return mix(c1,c2,ramp(uv.y,H-.012,H+.012));
      return mix(c2,c3,ramp(uv.y,H+.012,1.0));
    }
    float band(float p) {
      float x=mod(p,40.0);
      if(x<=9.0) return x/9.0;
      if(x<=18.0) return 1.0-(x-9.0)/9.0;
      if(x<=31.0) return (x-18.0)/13.0*.5;
      return .5*(1.0-(x-31.0)/9.0);
    }
    vec3 field(vec4 a,vec4 b,vec2 uv,vec3 base,float pulse) {
      float V=.30+a.y*.44, H=b.x, k=b.y*pulse;
      float fa=min(.95,a.x*(1.0+k*.5));
      vec3 fogC=hsv(a.z,a.w*.45,min(.97,(V+.2)*(1.0+k*.6)));
      vec3 color=mix(base,hsv(a.z,a.w*.45,min(.97,V+.2)),alpha(min(.95,a.x)*.32));
      float fy=uv.y-H;
      float f=0.0;
      if(fy<=-.08) f=mix(0.0,alpha(fa*.55),ramp(fy,-.34,-.08));
      else if(fy<=0.0) f=mix(alpha(fa*.55),alpha(fa),ramp(fy,-.08,0.0));
      else if(fy<=.10) f=mix(alpha(fa),alpha(fa*.7),ramp(fy,0.0,.10));
      else f=mix(alpha(fa*.7),0.0,ramp(fy,.10,.34));
      float sa=alpha(min(.5,b.z*7.0)*fa);
      float len=u_size.x*.017452406+u_size.y*.999847695;
      float pos=(uv.x-.5)*u_size.x*.017452406+(uv.y-.5)*u_size.y*.999847695+len*.5+u_drift;
      float bands=0.0, norm=0.0;
      for(int j=-4;j<=4;j++) { float q=float(j); float w=exp(-.5*q*q/(1.25*1.25)); bands+=w*band(pos+q*2.0); norm+=w; }
      float mask=min(ramp(fy,-.22,-.04),1.0-ramp(fy,.06,.20));
      color=mix(color,fogC,sa*bands/norm*mask);
      color=mix(color,fogC,f);
      float r=length((uv-vec2(.5,.4))/vec2(.58,.46));
      float la=min(1.0,a.y*.78*(1.0+k));
      float l;
      if(r<=.30) l=mix(alpha(la),alpha(la*.55),ramp(r,0.0,.30));
      else if(r<=.60) l=mix(alpha(la*.55),alpha(la*.18),ramp(r,.30,.60));
      else l=mix(alpha(la*.18),0.0,ramp(r,.60,1.0));
      return mix(color,hsv(a.z,a.w*.35,min(.98,V+.28+k*.1)),l);
    }
    void main() {
      vec2 uv=vec2(v_uv.x,1.0-v_uv.y);
      vec3 rest=vec3(0.0), candidate=vec3(0.0);
      float budget=0.0;
      for(int i=0;i<4;i++) {
        if(i<u_count) {
          vec4 a=u_tokensA[i], b=u_tokensB[i];
          vec3 base=baseField(a,b,uv);
          vec3 r=linearRGB(field(a,b,uv,base,0.0));
          vec3 delta=max(vec3(0.0),linearRGB(field(a,b,uv,base,1.0))-r);
          float cap=min(b.y*lum(linearRGB(base)),.09);
          float scale=min(1.0,cap/max(1e-12,lum(delta)));
          rest+=b.w*r; candidate+=b.w*(r+delta*scale); budget+=b.w*cap;
        }
      }
      vec3 restQ=floor(encodedRGB(rest)*255.0+.5)/255.0;
      vec3 rq=linearRGB(restQ);
      vec3 delta=max(vec3(0.0),candidate-rq);
      float scale=min(1.0,budget/max(1e-12,lum(delta)));
      vec3 outputColor=max(restQ,floor(encodedRGB(rq+delta*scale*u_pulse)*255.0+.000001)/255.0);
      gl_FragColor=vec4(u_pulse<=0.0 ? restQ : outputColor,1.0);
    }`;

  class Renderer {
    constructor(canvas) {
      if (!canvas || typeof canvas.getContext !== "function") throw new TypeError("Field needs a canvas");
      this.canvas = canvas;
      this.gl = canvas.getContext("webgl", { alpha: false, antialias: false,
        depth: false, stencil: false, preserveDrawingBuffer: true, powerPreference: "low-power" });
      if (!this.gl) throw new Error("Ambient field unavailable: WebGL is required");
      const gl = this.gl;
      if (!gl.getShaderPrecisionFormat(gl.FRAGMENT_SHADER, gl.HIGH_FLOAT).precision) {
        throw new Error("Ambient field unavailable: high-precision fragment arithmetic is required");
      }
      const compile = (type, source) => {
        const shader = gl.createShader(type); gl.shaderSource(shader, source); gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
          const error = gl.getShaderInfoLog(shader); gl.deleteShader(shader); throw new Error(error);
        }
        return shader;
      };
      const vertex = compile(gl.VERTEX_SHADER, VERTEX), fragment = compile(gl.FRAGMENT_SHADER, FRAGMENT);
      this.program = gl.createProgram();
      gl.attachShader(this.program, vertex); gl.attachShader(this.program, fragment); gl.linkProgram(this.program);
      gl.deleteShader(vertex); gl.deleteShader(fragment);
      if (!gl.getProgramParameter(this.program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(this.program));
      this.buffer = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
      this.locations = Object.fromEntries(["size", "tokensA[0]", "tokensB[0]", "pulse", "drift", "count"]
        .map((key) => [key, gl.getUniformLocation(this.program, `u_${key}`)]));
      this.position = gl.getAttribLocation(this.program, "a_position");
      gl.disable(gl.DITHER); gl.disable(gl.BLEND); gl.disable(gl.DEPTH_TEST);
    }

    resize(width, height, pixelRatio = 1) {
      if (![width, height, pixelRatio].every(Number.isFinite) || Math.min(width, height, pixelRatio) <= 0) {
        throw new RangeError("Invalid field canvas size");
      }
      const w = Math.max(1, Math.round(width * pixelRatio)), h = Math.max(1, Math.round(height * pixelRatio));
      if (this.canvas.width !== w) this.canvas.width = w;
      if (this.canvas.height !== h) this.canvas.height = h;
      this.cssWidth = width; this.cssHeight = height;
    }

    render(layers, { pulse = 0, drift = 0 } = {}) {
      if (![pulse, drift].every(Number.isFinite)) throw new TypeError("Invalid field pulse/drift");
      const prepared = prepareLayers(layers);
      const gl = this.gl;
      if (gl.isContextLost()) throw new Error("Ambient field unavailable: rendering context was lost");
      gl.viewport(0, 0, this.canvas.width, this.canvas.height);
      gl.useProgram(this.program); gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
      gl.enableVertexAttribArray(this.position); gl.vertexAttribPointer(this.position, 2, gl.FLOAT, false, 0, 0);
      const a = new Float32Array(MAX_LAYERS * 4), b = new Float32Array(MAX_LAYERS * 4);
      prepared.forEach(({ tokens: t, weight }, i) => {
        a.set([t.fog, t.light, t.hue, t.sat], i * 4); b.set([t.horizon, t.pulse, t.motion, weight], i * 4);
      });
      gl.uniform4fv(this.locations["tokensA[0]"], a); gl.uniform4fv(this.locations["tokensB[0]"], b);
      gl.uniform2f(this.locations.size, this.cssWidth || this.canvas.width, this.cssHeight || this.canvas.height);
      gl.uniform1f(this.locations.pulse, clamp(pulse));
      // One drift unit is one view height, not one 40-pixel stripe period. Wrap only the
      // periodic texture coordinate, keeping the authored normalised units/second portable.
      const driftPixels = drift * (this.cssHeight || this.canvas.height);
      gl.uniform1f(this.locations.drift, ((driftPixels % 40) + 40) % 40);
      gl.uniform1i(this.locations.count, prepared.length); gl.drawArrays(gl.TRIANGLES, 0, 6);
    }

    /** Actual RGBA framebuffer bytes, bottom row first (WebGL convention). */
    readPixels() {
      const rgba = new Uint8Array(this.canvas.width * this.canvas.height * 4);
      this.gl.readPixels(0, 0, this.canvas.width, this.canvas.height, this.gl.RGBA, this.gl.UNSIGNED_BYTE, rgba);
      return rgba;
    }

    // The opaque canvas is hidden by the host in cold idle. Clear old pixels
    // without inventing an idle palette or retaining a previous visitor.
    clear() { this.gl.clearColor(0, 0, 0, 1); this.gl.clear(this.gl.COLOR_BUFFER_BIT); }

    dispose() { this.gl.deleteBuffer(this.buffer); this.gl.deleteProgram(this.program); }
  }

  return { Renderer, sampleLayers, samplePixel: (tokens, x, y, options) =>
    sampleLayers([{ tokens, weight: 1 }], x, y, options), prepareLayers, hsv,
    linear, encoded, luminance, KEYS, LIMITS, MAX_LAYERS, MAX_PULSE, ABSOLUTE_PULSE_LIMIT };
});
