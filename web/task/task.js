/*
 * Prism Live browser load task, prompt 3.1.
 *
 * IMPORTANT: the dwell and angular-speed numbers were authored for eye control. Quest 3S has
 * no eye tracking, so pointer and head-reticle control are harder at the same values. The ramp
 * is provisional until Week E retunes it against real runs.
 *
 * This file owns the task timing for both live and standalone modes. Keep it independent of the
 * DOM and transport: app.js supplies an input source and chooses whether events go to WebSocket
 * or console. That prevents the standalone judging path from growing a second copy of the ramp.
 */

(function exposeLoadTask(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.PrismLoadTask = api;
})(typeof globalThis === "undefined" ? this : globalThis, function buildLoadTask() {
  "use strict";

  const VERSION = 1;
  const BUILD = "3.2.0";
  const LOAD_DURATION_MS = 75_000;
  const DEFAULT_DISTANCE_CM = 60;
  const DEFAULT_DIAGONAL_IN = 27;
  const DEFAULT_ASPECT_WIDTH = 16;
  const DEFAULT_ASPECT_HEIGHT = 9;
  const CM_PER_INCH = 2.54;
  const MISS_PENALTY_MS = 400;
  const MIN_SPLIT_INTERVAL_MS = 1_800;
  const STANDALONE_SESSION = "S-19700101-0001";

  const DEFAULT_SCREEN_WIDTH_CM = screenWidthFromDiagonal(
    DEFAULT_DIAGONAL_IN,
    DEFAULT_ASPECT_WIDTH,
    DEFAULT_ASPECT_HEIGHT,
  );

  function clamp(value, low, high) {
    return Math.min(high, Math.max(low, value));
  }

  function lerp(start, end, amount) {
    return start + (end - start) * amount;
  }

  function round(value, places) {
    const scale = 10 ** places;
    return Math.round(value * scale) / scale;
  }

  function screenWidthFromDiagonal(diagonalIn, aspectWidth = 16, aspectHeight = 9) {
    requirePositive(diagonalIn, "diagonalIn");
    requirePositive(aspectWidth, "aspectWidth");
    requirePositive(aspectHeight, "aspectHeight");
    return (
      diagonalIn * CM_PER_INCH * (aspectWidth / Math.hypot(aspectWidth, aspectHeight))
    );
  }

  function createGeometry(
    distanceCm = DEFAULT_DISTANCE_CM,
    screenWidthCm = DEFAULT_SCREEN_WIDTH_CM,
  ) {
    requirePositive(distanceCm, "distanceCm");
    requirePositive(screenWidthCm, "screenWidthCm");
    return Object.freeze({ distanceCm, screenWidthCm });
  }

  function horizontalFovDeg(geometry) {
    requireGeometry(geometry);
    return radiansToDegrees(2 * Math.atan(geometry.screenWidthCm / (2 * geometry.distanceCm)));
  }

  /* Project a horizontal visual angle onto the physical monitor plane. */
  function angleToPixel(angleDeg, viewportWidthPx, geometry) {
    requirePositive(viewportWidthPx, "viewportWidthPx");
    requireGeometry(geometry);
    const physicalX = Math.tan(degreesToRadians(angleDeg)) * geometry.distanceCm;
    return viewportWidthPx / 2 + physicalX * (viewportWidthPx / geometry.screenWidthCm);
  }

  function pixelToAngle(pixelX, viewportWidthPx, geometry) {
    requireFinite(pixelX, "pixelX");
    requirePositive(viewportWidthPx, "viewportWidthPx");
    requireGeometry(geometry);
    const physicalX =
      (pixelX - viewportWidthPx / 2) * (geometry.screenWidthCm / viewportWidthPx);
    return radiansToDegrees(Math.atan(physicalX / geometry.distanceCm));
  }

  /* Instantaneous projected velocity. Motion itself remains constant in degrees per second. */
  function pixelsPerSecond(angleDeg, angularSpeedDegPerSecond, viewportWidthPx, geometry) {
    requireFinite(angleDeg, "angleDeg");
    requirePositive(angularSpeedDegPerSecond, "angularSpeedDegPerSecond");
    requirePositive(viewportWidthPx, "viewportWidthPx");
    requireGeometry(geometry);
    const angleRad = degreesToRadians(angleDeg);
    const pixelsPerCm = viewportWidthPx / geometry.screenWidthCm;
    return (
      angularSpeedDegPerSecond *
      (Math.PI / 180) *
      geometry.distanceCm *
      pixelsPerCm *
      (1 / Math.cos(angleRad)) ** 2
    );
  }

  function difficultyAt(elapsedMs) {
    requireFinite(elapsedMs, "elapsedMs");
    return clamp(elapsedMs / LOAD_DURATION_MS, 0, 1);
  }

  function rampAt(elapsedMs, misses = 0) {
    if (!Number.isInteger(misses) || misses < 0) {
      throw new RangeError("misses must be a whole number of 0 or more");
    }
    const difficulty = difficultyAt(elapsedMs);
    const nominalSplitIntervalMs = lerp(5_500, 2_200, difficulty);
    return Object.freeze({
      difficulty,
      dwellMs: lerp(900, 420, difficulty),
      nominalSplitIntervalMs,
      splitIntervalMs: Math.max(
        MIN_SPLIT_INTERVAL_MS,
        nominalSplitIntervalMs - misses * MISS_PENALTY_MS,
      ),
      distractors: Math.round(lerp(1, 3, difficulty)),
      angularSpeedDegPerSecond: lerp(6, 19, difficulty),
    });
  }

  class LoadTask {
    constructor({ emit, random = Math.random } = {}) {
      if (typeof emit !== "function") throw new TypeError("emit must be a function");
      if (typeof random !== "function") throw new TypeError("random must be a function");
      this.emit = emit;
      this.random = random;
      this.active = false;
      this.complete = false;
      this.paused = false;
      this.session = null;
      this.misses = 0;
      this.round = null;
      this.angleDeg = -8;
      this.direction = 1;
      this.motionLimitDeg = 20;
      this.anchorClientMs = 0;
      this.anchorElapsedMs = 0;
      this.lastTickClientMs = 0;
      this.nextSplitElapsedMs = 0;
      this.hoveredId = null;
      this.hoverStartedClientMs = null;
      this.dwellProgress = 0;
    }

    setMotionLimitDegrees(limitDeg) {
      requirePositive(limitDeg, "limitDeg");
      this.motionLimitDeg = limitDeg;
      this.angleDeg = clamp(this.angleDeg, -limitDeg, limitDeg);
    }

    start({ session, elapsedMs = 0, nowMs }) {
      requireSession(session);
      requireFinite(elapsedMs, "elapsedMs");
      requireNonNegative(nowMs, "nowMs");
      const elapsed = clamp(elapsedMs, 0, LOAD_DURATION_MS);
      this.active = elapsed < LOAD_DURATION_MS;
      this.complete = elapsed >= LOAD_DURATION_MS;
      this.paused = false;
      this.session = session;
      this.misses = 0;
      this.round = null;
      this.angleDeg = clamp(-8, -this.motionLimitDeg, this.motionLimitDeg);
      this.direction = 1;
      this.anchorClientMs = nowMs;
      this.anchorElapsedMs = elapsed;
      this.lastTickClientMs = nowMs;
      this.nextSplitElapsedMs = Math.min(
        LOAD_DURATION_MS,
        elapsed + rampAt(elapsed, this.misses).splitIntervalMs,
      );
      this.#clearHover();
      return this.snapshot(nowMs);
    }

    sync(elapsedMs, nowMs) {
      requireFinite(elapsedMs, "elapsedMs");
      requireNonNegative(nowMs, "nowMs");
      if (!this.active || this.complete) return this.snapshot(nowMs);
      this.anchorElapsedMs = clamp(elapsedMs, 0, LOAD_DURATION_MS);
      this.anchorClientMs = nowMs;
      this.lastTickClientMs = nowMs;
      this.paused = false;
      return this.snapshot(nowMs);
    }

    freeze(nowMs) {
      requireNonNegative(nowMs, "nowMs");
      if (!this.active || this.paused) return this.snapshot(nowMs);
      this.anchorElapsedMs = this.elapsedAt(nowMs);
      this.anchorClientMs = nowMs;
      this.lastTickClientMs = nowMs;
      this.paused = true;
      this.#clearHover();
      return this.snapshot(nowMs);
    }

    stop() {
      this.active = false;
      this.complete = false;
      this.paused = false;
      this.round = null;
      this.session = null;
      this.#clearHover();
    }

    elapsedAt(nowMs) {
      requireNonNegative(nowMs, "nowMs");
      if (this.paused) return this.anchorElapsedMs;
      return clamp(
        this.anchorElapsedMs + Math.max(0, nowMs - this.anchorClientMs),
        0,
        LOAD_DURATION_MS,
      );
    }

    tick(nowMs, hoveredId = null) {
      requireNonNegative(nowMs, "nowMs");
      if (!this.active || this.paused) return this.snapshot(nowMs);

      const elapsedMs = this.elapsedAt(nowMs);
      const deltaMs = Math.max(0, nowMs - this.lastTickClientMs);
      const deltaSeconds = deltaMs / 1000;
      this.lastTickClientMs = nowMs;
      const previousElapsedMs = Math.max(0, elapsedMs - deltaMs);
      const speedBefore = rampAt(previousElapsedMs, this.misses).angularSpeedDegPerSecond;
      const speedAfter = rampAt(elapsedMs, this.misses).angularSpeedDegPerSecond;
      this.#advanceMotion(deltaSeconds, (speedBefore + speedAfter) / 2);

      if (elapsedMs >= LOAD_DURATION_MS) {
        this.active = false;
        this.complete = true;
        this.round = null;
        this.#clearHover();
        return this.snapshot(nowMs);
      }

      this.#interact(nowMs, elapsedMs, hoveredId);
      if (elapsedMs >= this.nextSplitElapsedMs) {
        if (this.round && !this.round.resolved) this.#resolveMiss(nowMs, elapsedMs);
        this.#split(nowMs, elapsedMs);
      }
      return this.snapshot(nowMs);
    }

    snapshot(nowMs) {
      const elapsedMs = this.session === null ? 0 : this.elapsedAt(nowMs);
      const ramp = rampAt(elapsedMs, this.misses);
      return {
        active: this.active,
        complete: this.complete,
        paused: this.paused,
        session: this.session,
        elapsedMs,
        misses: this.misses,
        ramp,
        angleDeg: this.angleDeg,
        direction: this.direction,
        round: this.round,
        hoveredId: this.hoveredId,
        dwellProgress: this.dwellProgress,
      };
    }

    #advanceMotion(deltaSeconds, speedDegPerSecond) {
      const limit = this.motionLimitDeg;
      if (!(deltaSeconds > 0) || !(limit > 0)) return;
      const position = this.angleDeg + limit;
      const period = 4 * limit;
      let phase = this.direction > 0 ? position : period - position;
      phase = (phase + speedDegPerSecond * deltaSeconds) % period;
      if (phase <= 2 * limit) {
        this.angleDeg = phase - limit;
        this.direction = 1;
      } else {
        this.angleDeg = period - phase - limit;
        this.direction = -1;
      }
    }

    #interact(nowMs, elapsedMs, hoveredId) {
      if (!this.round || this.round.resolved) {
        this.#clearHover();
        return;
      }
      const valid =
        hoveredId === "moving" || this.round.decoys.some((decoy) => decoy.id === hoveredId);
      const nextHovered = valid ? hoveredId : null;
      if (nextHovered !== this.hoveredId) {
        // Leaving either half before dwell completes is evidence that the person is still
        // pursuing the task. Restricting abandon to the moving half would make a brief chase of a
        // distractor invisible and its later automatic timeout look like disengagement.
        if (this.hoveredId !== null && this.hoverStartedClientMs !== null) {
          const heldMs = nowMs - this.hoverStartedClientMs;
          if (heldMs > 0 && heldMs < rampAt(elapsedMs, this.misses).dwellMs) {
            this.#emit("abandon", nowMs, elapsedMs);
          }
        }
        this.hoveredId = nextHovered;
        this.hoverStartedClientMs = nextHovered === null ? null : nowMs;
        this.dwellProgress = 0;
      }
      if (this.hoveredId === null || this.hoverStartedClientMs === null) return;

      const parameters = rampAt(elapsedMs, this.misses);
      const heldMs = Math.max(0, nowMs - this.hoverStartedClientMs);
      this.dwellProgress = clamp(heldMs / parameters.dwellMs, 0, 1);
      if (this.dwellProgress < 1) return;
      if (this.hoveredId === "moving") {
        this.#emit("lock", nowMs, elapsedMs);
        this.round.resolved = true;
        this.round.outcome = "lock";
        this.#clearHover();
      } else {
        this.#resolveMiss(nowMs, elapsedMs);
      }
    }

    #resolveMiss(nowMs, elapsedMs) {
      this.#emit("miss", nowMs, elapsedMs);
      this.misses += 1;
      if (this.round) {
        this.round.resolved = true;
        this.round.outcome = "miss";
        const penalized = rampAt(elapsedMs, this.misses).splitIntervalMs;
        this.nextSplitElapsedMs = Math.min(
          this.nextSplitElapsedMs,
          this.round.splitElapsedMs + penalized,
        );
      }
      this.#clearHover();
    }

    #split(nowMs, elapsedMs) {
      const parameters = rampAt(elapsedMs, this.misses);
      const decoys = [];
      for (let index = 0; index < parameters.distractors; index += 1) {
        const side = index % 2 === 0 ? -1 : 1;
        const rank = Math.floor(index / 2);
        const offset = side * (2.6 + rank * 1.5 + this.random() * 0.6);
        decoys.push({
          id: `decoy-${index}`,
          angleDeg: clamp(
            this.angleDeg + offset,
            -this.motionLimitDeg,
            this.motionLimitDeg,
          ),
          yOffset: (index - (parameters.distractors - 1) / 2) * 0.14,
        });
      }
      this.round = {
        splitElapsedMs: elapsedMs,
        resolved: false,
        outcome: null,
        decoys,
      };
      this.#clearHover();
      this.#emit("split", nowMs, elapsedMs);
      this.nextSplitElapsedMs = Math.min(
        LOAD_DURATION_MS,
        elapsedMs + parameters.splitIntervalMs,
      );
    }

    #emit(event, nowMs, elapsedMs) {
      const parameters = rampAt(elapsedMs, this.misses);
      this.emit({
        type: "task_event",
        v: VERSION,
        session: this.session,
        t_client: round(nowMs, 3),
        event,
        dwell_ms: round(parameters.dwellMs, 3),
        split_interval_ms: round(parameters.splitIntervalMs, 3),
        difficulty: round(parameters.difficulty, 3),
      });
    }

    #clearHover() {
      this.hoveredId = null;
      this.hoverStartedClientMs = null;
      this.dwellProgress = 0;
    }
  }

  class StateGate {
    constructor(task) {
      if (!(task instanceof LoadTask)) throw new TypeError("task must be a LoadTask");
      this.task = task;
    }

    apply(state, nowMs) {
      if (!state || state.type !== "state") return false;
      if (state.segment !== "load") {
        if (this.task.active || this.task.paused) this.task.stop();
        return false;
      }
      if (this.task.complete && this.task.session === state.session) return true;
      if (!this.task.active || this.task.session !== state.session) {
        this.task.start({
          session: state.session,
          elapsedMs: state.segment_elapsed_ms,
          nowMs,
        });
      } else {
        this.task.sync(state.segment_elapsed_ms, nowMs);
      }
      return true;
    }

    disconnect(nowMs) {
      if (this.task.active) this.task.freeze(nowMs);
    }
  }

  function requireGeometry(geometry) {
    if (!geometry || typeof geometry !== "object") throw new TypeError("geometry is required");
    requirePositive(geometry.distanceCm, "geometry.distanceCm");
    requirePositive(geometry.screenWidthCm, "geometry.screenWidthCm");
  }

  function requireSession(session) {
    if (typeof session !== "string" || !/^S-\d{8}-\d{4}$/.test(session)) {
      throw new TypeError("session must be S-YYYYMMDD-NNNN");
    }
  }

  function requireFinite(value, name) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      throw new TypeError(`${name} must be a finite number`);
    }
  }

  function requirePositive(value, name) {
    requireFinite(value, name);
    if (!(value > 0)) throw new RangeError(`${name} must be greater than 0`);
  }

  function requireNonNegative(value, name) {
    requireFinite(value, name);
    if (value < 0) throw new RangeError(`${name} must be 0 or more`);
  }

  function degreesToRadians(value) {
    return (value * Math.PI) / 180;
  }

  function radiansToDegrees(value) {
    return (value * 180) / Math.PI;
  }

  return Object.freeze({
    BUILD,
    VERSION,
    LOAD_DURATION_MS,
    DEFAULT_DISTANCE_CM,
    DEFAULT_DIAGONAL_IN,
    DEFAULT_SCREEN_WIDTH_CM,
    MISS_PENALTY_MS,
    MIN_SPLIT_INTERVAL_MS,
    STANDALONE_SESSION,
    LoadTask,
    StateGate,
    angleToPixel,
    createGeometry,
    difficultyAt,
    horizontalFovDeg,
    pixelToAngle,
    pixelsPerSecond,
    rampAt,
    screenWidthFromDiagonal,
  });
});
