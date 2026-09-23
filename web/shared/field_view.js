/* Shared live field wiring. Mapping, scheduled pulse and drawing remain separate references. */
(function exposeFieldView(root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.PrismFieldView = api;
})(typeof globalThis === "undefined" ? this : globalThis, function buildFieldView(root) {
  "use strict";

  const STATE_STALE_MS = 2_500;
  const WEIGHT_SETTLE_MS = 250;
  const ACTIVE_SEGMENTS = new Set(["baseline", "load", "regulate", "resolve"]);
  // Explicitly authored test appearance, not a fabricated state or physiology. No test beats.
  const STANDALONE_TOKENS = Object.freeze({
    fog: 0.20, light: 0.70, hue: 214, sat: 0.16, horizon: 0.50, pulse: 0, motion: 0.010,
  });

  class FieldView {
    constructor(canvas, dependencies = {}) {
      const Mapping = dependencies.mapping || root.PrismFieldMapping;
      const Pulse = dependencies.pulse || root.PrismFieldPulse;
      const Drawing = dependencies.drawing || root.PrismFieldRenderer;
      if (!Mapping || !Pulse || !Drawing) throw new Error("shared field dependencies did not load");
      this.canvas = canvas;
      this.canvas.hidden = true;
      this.mapping = new Mapping.FieldState();
      this.pulseAmplitude = Mapping.pulseAmplitude;
      this.pulse = new Pulse.BeatPulse();
      this.renderer = null;
      this.available = true;
      this.onError = dependencies.onError || ((error) => root.console.error("FIELD UNAVAILABLE", error));
      this.requestFrame = dependencies.requestFrame || root.requestAnimationFrame.bind(root);
      this.cancelFrame = dependencies.cancelFrame || root.cancelAnimationFrame.bind(root);
      this.session = null;
      this.active = false;
      this.frozen = true;
      this.standalone = false;
      this.standaloneLayers = [];
      this.drift = 0;
      this.lastFrameAt = null;
      this.frameId = null;
      this.size = null;
      this.paletteIdentity = null;
      this.paletteLayers = [];
      this.displayLayers = [];
      this.weightEase = null;
      this.frame = this.frame.bind(this);
      this.onContextLost = (event) => {
        event.preventDefault();
        this.fail(new Error("Graphics context lost; reload this display to restore the field."));
      };
      this.canvas.addEventListener?.("webglcontextlost", this.onContextLost);
      try {
        this.renderer = new Drawing.Renderer(canvas);
      } catch (error) {
        this.fail(error);
      }
    }

    onState(state, now) {
      if (!this.mapping.onState(state)) return false;
      if (this.frozen) this.weightEase = null;
      this.updateLayers(state, now);
      if (this.standalone || state.session !== this.session) {
        this.session = state.session;
        this.pulse.reset(state.session);
        this.drift = 0;
      } else if (this.frozen) {
        this.pulse.clear(); // Reconnect never replays the old queue.
      }
      this.standalone = false;
      this.active = ACTIVE_SEGMENTS.has(state.segment);
      this.frozen = !this.active || !this.available;
      this.canvas.hidden = this.frozen;
      if (this.frozen) {
        this.pulse.clear();
        this.stopFrames();
        if (this.available) {
          try { this.renderer.clear(); } catch (error) { this.fail(error); }
        }
        return true;
      }
      if (this.lastFrameAt === null) this.lastFrameAt = now;
      this.draw(now);
      this.startFrames();
      return true;
    }

    onBeat(message, localPlay, receivedAt) {
      if (this.frozen || !this.active || this.standalone || message.session !== this.session) {
        return false;
      }
      return this.pulse.schedule(message, localPlay, receivedAt);
    }

    freeze() {
      this.frozen = true;
      this.pulse.clear();
      this.stopFrames();
      // Do not redraw: the last verified pixels, including any in-progress pulse, are frozen.
    }

    setStandalone(tokens = STANDALONE_TOKENS, now = root.performance.now()) {
      this.mapping.reset();
      this.session = null;
      this.pulse.reset();
      this.standalone = true;
      this.standaloneLayers = [{ tokens: { ...tokens, pulse: 0 }, weight: 1 }];
      this.active = true;
      this.frozen = !this.available;
      this.canvas.hidden = !this.available;
      this.drift = 0;
      this.lastFrameAt = now;
      this.draw(now);
      this.startFrames();
    }

    frame(now) {
      this.frameId = null;
      if (this.frozen || !this.active) return;
      const layers = this.layers();
      const motion = layers.reduce((value, layer) => value + layer.tokens.motion * layer.weight, 0);
      const elapsed = this.lastFrameAt === null ? 0 : Math.max(0, Math.min(100, now - this.lastFrameAt));
      this.drift += motion * elapsed / 1000;
      this.lastFrameAt = now;
      this.draw(now);
      this.startFrames();
    }

    layers() {
      return this.standalone ? this.standaloneLayers : this.displayLayers;
    }

    updateLayers(state, now) {
      const identity = `${state.session}:${state.segment}`;
      const target = this.mapping.layers();
      if (identity !== this.paletteIdentity || !target.length) {
        this.paletteLayers = target;
        this.displayLayers = target;
        this.weightEase = null;
      } else if (target.length === 2 || this.displayLayers.length === 2) {
        const current = this.displayLayers.length === 2 ? this.displayLayers[1].weight : 1;
        const weight = target.length === 2 ? target[1].weight : 1;
        this.paletteLayers = target.length === 2 ? target : [
          { tokens: this.displayLayers[0].tokens, weight: 0 }, target[0],
        ];
        if (!this.weightEase || this.weightEase.to !== weight) {
          this.weightEase = { from: current, to: weight, at: now };
        }
      } else {
        this.paletteLayers = target;
        this.displayLayers = target;
      }
      this.paletteIdentity = identity;
    }

    draw(now) {
      if (!this.available) return;
      if (!this.standalone && this.weightEase) {
        // Cosmetic lag ONLY toward a weight already reported by the host. Never extrapolate
        // host segment time, mix hue tokens or move beyond the last verified target weight.
        const fraction = Math.max(0, Math.min(1, (now - this.weightEase.at) / WEIGHT_SETTLE_MS));
        const weight = this.weightEase.from + (this.weightEase.to - this.weightEase.from) * fraction;
        this.displayLayers = this.paletteLayers.map((layer, index) => ({
          tokens: layer.tokens, weight: index ? weight : 1 - weight,
        }));
      }
      const pulse = this.pulse.value(now);
      const width = this.canvas.clientWidth;
      const height = this.canvas.clientHeight;
      if (!(width > 0 && height > 0)) return; // Spectator reveal/idle may hide the entire panel.
      const ratio = Math.min(2, root.devicePixelRatio || 1);
      const size = `${width}:${height}:${ratio}`;
      try {
        if (size !== this.size) {
          this.renderer.resize(width, height, ratio);
          this.size = size;
        }
        // State can lag beats by two seconds. Cap EVERY palette by the current state's and
        // active beat's tapered amplitudes, not their product (no doubled taper), and never
        // retain a stale outgoing-palette pulse during the final 250 ms of a dissolve.
        const amplitude = this.standalone ? 0 : Math.min(this.mapping.tokens?.pulse ?? 0,
          this.pulseAmplitude(this.mapping.state, this.pulse.activeRate()));
        const layers = this.layers().map(layer => ({ ...layer,
          tokens: { ...layer.tokens, pulse: amplitude } }));
        this.renderer.render(layers, { pulse, drift: this.drift });
      } catch (error) {
        this.fail(error);
      }
    }

    startFrames() {
      if (!this.available || this.frozen || !this.active) return;
      if (this.frameId === null) this.frameId = this.requestFrame(this.frame);
    }

    stopFrames() {
      if (this.frameId !== null) this.cancelFrame(this.frameId);
      this.frameId = null;
      this.lastFrameAt = null;
    }

    destroy() {
      this.freeze();
      this.canvas.removeEventListener?.("webglcontextlost", this.onContextLost);
      this.renderer?.dispose?.();
    }

    fail(error) {
      this.available = false;
      this.freeze();
      this.canvas.hidden = true;
      this.onError(error);
    }
  }

  // Task shares the exact clock arithmetic, and validates every clock field it consumes.
  function validPong(message) {
    return message?.type === "clock" && message.v === 1 && message.role === "pong" &&
      Number.isFinite(message.t_client_sent) && message.t_client_sent >= 0 &&
      Number.isSafeInteger(message.t_engine) && message.t_engine >= 0;
  }

  return Object.freeze({ FieldView, STATE_STALE_MS, WEIGHT_SETTLE_MS, STANDALONE_TOKENS, validPong });
});
