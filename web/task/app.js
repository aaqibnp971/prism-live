/*
 * Browser shell for prompts 3.1 / 3.4.
 *
 * The ramp was authored for eye control, but Quest 3S has no eye tracking. Pointer and later
 * head-reticle control use these provisional values until Week E. All timing and difficulty live
 * in task.js; this file only supplies pointer input, drawing, state gating and an event sink.
 */

(function runTaskScreen() {
  "use strict";

  const Task = globalThis.PrismLoadTask;
  const Field = globalThis.PrismFieldView;
  const Clock = globalThis.PrismClockSync;
  const Pulse = globalThis.PrismFieldPulse;
  if (!Task) throw new Error("task.js did not load");
  if (!Field || !Clock || !Pulse) throw new Error("shared field and clock dependencies did not load");

  const params = new URLSearchParams(globalThis.location.search);
  const standalone = params.has("standalone") && params.get("standalone") !== "0";
  const distanceCm = positiveParameter("distance_cm", Task.DEFAULT_DISTANCE_CM);
  let screenWidthCm = positiveParameter("screen_width_cm", Task.DEFAULT_SCREEN_WIDTH_CM);
  let geometry = Task.createGeometry(distanceCm, screenWidthCm);
  const tiled = params.get("tiled") === "1";
  const monitorWidthCm = positiveParameter("monitor_width_cm", 0);
  const monitorWidthPx = positiveParameter("monitor_width_px", 0);

  const field = requiredElement("field");
  const fieldView = new Field.FieldView(requiredElement("ambient-field"), {
    onError(error) {
      requiredElement("field-error").hidden = false;
      console.error("FIELD UNAVAILABLE", error);
    },
  });
  const targetLayer = requiredElement("target-layer");
  const reticle = requiredElement("reticle");
  const instruction = requiredElement("instruction");
  const linkDot = requiredElement("link-dot");
  const linkLabel = requiredElement("link-label");
  const segmentLabel = requiredElement("segment-label");
  const timerLabel = requiredElement("timer-label");
  const connectionOverlay = requiredElement("connection-overlay");
  const overlayTitle = requiredElement("overlay-title");
  const overlayDetail = requiredElement("overlay-detail");
  const completeOverlay = requiredElement("complete-overlay");
  const restartButton = requiredElement("restart-button");
  const debugRestartButton = requiredElement("debug-restart-button");
  const fullscreenButton = requiredElement("fullscreen-button");
  const geometryLabel = requiredElement("geometry-label");
  const fovLabel = requiredElement("fov-label");
  const speedLabel = requiredElement("speed-label");
  const difficultyLabel = requiredElement("difficulty-label");
  const feedback = requiredElement("feedback");

  const pointer = new PointerInput(field);
  const targetNodes = new Map();
  let socket = null;
  let reconnectTimer = null;
  let lastSnapshot = null;
  let lastTargets = [];
  let lastSegment = "idle";
  let linkConnected = false;
  let feedbackTimer = null;
  let clockSync = null;
  let pingTimer = null;
  let pendingBeats = [];
  let lastStateAt = null;
  let feedFrozen = true;
  let blockedConnection = null;

  const task = new Task.LoadTask({ emit: deliverTaskEvent });
  const stateGate = new Task.StateGate(task);

  const defaultGeometry = !params.has("distance_cm") && !params.has("screen_width_cm") &&
    !params.has("monitor_width_cm");
  // A shared laptop keeps the attendant warning visible beside this window. Never offer a
  // page fullscreen button that would cover it. The launcher's measured monitor width is
  // apportioned to this actual CSS viewport (including Windows DPI scaling), on every resize.
  fullscreenButton.hidden = tiled;
  fullscreenButton.addEventListener("click", () => {
    if (!tiled) {
      field.requestFullscreen?.().catch((error) => console.warn("Fullscreen was refused", error));
    }
  });
  restartButton.addEventListener("click", startStandalone);
  debugRestartButton.addEventListener("click", startStandalone);
  globalThis.addEventListener("keydown", (event) => {
    if (standalone && event.key.toLowerCase() === "r") startStandalone();
  });
  globalThis.addEventListener("resize", updateMotionLimit);
  globalThis.addEventListener("beforeunload", () => {
    globalThis.clearInterval(pingTimer);
    fieldView.destroy();
  });

  if (standalone) {
    debugRestartButton.hidden = false;
    setLinkState("connected", "STANDALONE • EVENTS → CONSOLE");
    requiredElement("field-mode-label").textContent = "FIXED LOAD TEST FIELD • NO HEARTBEATS";
    hideConnectionOverlay();
    startStandalone();
  } else {
    setLinkState("connecting", "CONNECTING");
    showWaitingOverlay();
    connect();
    globalThis.setInterval(checkStateFreshness, 200);
  }

  updateMotionLimit();
  globalThis.requestAnimationFrame(frame);

  function frame(nowMs) {
    const rect = field.getBoundingClientRect();
    const sample = pointer.sample(rect);
    const hoveredId = hitTest(sample, lastTargets);
    const snapshot = task.tick(nowMs, hoveredId);
    const targets = targetPositions(snapshot, rect);
    render(snapshot, sample, targets, rect);
    lastSnapshot = snapshot;
    lastTargets = targets;
    globalThis.requestAnimationFrame(frame);
  }

  function render(snapshot, sample, targets, rect) {
    renderTargets(targets, snapshot.hoveredId);
    const reticleVisible = sample.active && snapshot.active && !snapshot.paused;
    reticle.dataset.active = String(reticleVisible);
    if (sample.active) {
      reticle.style.transform = `translate3d(${sample.x}px, ${sample.y}px, 0)`;
    }
    reticle.style.setProperty("--dwell", `${snapshot.dwellProgress}turn`);

    instruction.dataset.running = String(snapshot.active && snapshot.elapsedMs > 4_000);
    instruction.hidden = !snapshot.active && !snapshot.paused;
    timerLabel.hidden = !standalone && lastSegment !== "load";
    timerLabel.textContent = ((Task.LOAD_DURATION_MS - snapshot.elapsedMs) / 1000).toFixed(1);
    segmentLabel.textContent = standalone
      ? snapshot.complete
        ? "STANDALONE COMPLETE"
        : "STANDALONE LOAD"
      : lastSegment.toUpperCase();

    const speedPx = Task.pixelsPerSecond(
      snapshot.angleDeg,
      snapshot.ramp.angularSpeedDegPerSecond,
      Math.max(1, rect.width),
      geometry,
    );
    speedLabel.textContent = `${snapshot.ramp.angularSpeedDegPerSecond.toFixed(2)}°/s → ${speedPx.toFixed(1)} px/s here`;
    difficultyLabel.textContent = `difficulty ${snapshot.ramp.difficulty.toFixed(3)} • dwell ${snapshot.ramp.dwellMs.toFixed(0)} ms • split ${(
      snapshot.ramp.splitIntervalMs / 1000
    ).toFixed(2)} s • misses ${snapshot.misses}`;

    if (standalone && snapshot.complete) completeOverlay.hidden = false;
  }

  function targetPositions(snapshot, rect) {
    if ((!snapshot.active && !snapshot.paused) || snapshot.complete) return [];
    const centerY = rect.height * 0.52;
    const positions = [
      {
        id: "moving",
        decoy: false,
        x: Task.angleToPixel(snapshot.angleDeg, Math.max(1, rect.width), geometry),
        y: centerY,
      },
    ];
    if (snapshot.round && !snapshot.round.resolved) {
      for (const decoy of snapshot.round.decoys) {
        positions.push({
          id: decoy.id,
          decoy: true,
          x: Task.angleToPixel(decoy.angleDeg, Math.max(1, rect.width), geometry),
          y: centerY + decoy.yOffset * Math.min(rect.height, 760),
        });
      }
    }
    return positions;
  }

  function renderTargets(targets, hoveredId) {
    const visible = new Set();
    const size = targetSizePx();
    for (const target of targets) {
      visible.add(target.id);
      let node = targetNodes.get(target.id);
      if (!node) {
        node = document.createElement("div");
        node.className = "target";
        node.dataset.id = target.id;
        targetLayer.append(node);
        targetNodes.set(target.id, node);
      }
      node.classList.toggle("target--decoy", target.decoy);
      node.dataset.visible = "true";
      node.dataset.hovered = String(target.id === hoveredId);
      node.style.transform = `translate3d(${target.x - size / 2}px, ${target.y - size / 2}px, 0)`;
    }
    for (const [id, node] of targetNodes) {
      if (!visible.has(id)) {
        node.dataset.visible = "false";
        node.dataset.hovered = "false";
      }
    }
  }

  function hitTest(sample, targets) {
    if (!sample.active || !lastSnapshot?.active || lastSnapshot.paused) return null;
    const radius = targetSizePx() / 2 + 7;
    let closest = null;
    let closestDistance = Infinity;
    for (const target of targets) {
      const distance = Math.hypot(sample.x - target.x, sample.y - target.y);
      if (distance <= radius && distance < closestDistance) {
        closest = target.id;
        closestDistance = distance;
      }
    }
    return closest;
  }

  function deliverTaskEvent(message) {
    flashFeedback(message.event);
    if (standalone) {
      console.info("task_event", message);
      return;
    }
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
  }

  function flashFeedback(event) {
    if (event === "abandon") return;
    const labels = { split: "SPLIT", lock: "LOCKED", miss: "MISS" };
    feedback.textContent = labels[event] || event.toUpperCase();
    feedback.dataset.event = event;
    feedback.dataset.visible = "false";
    void feedback.offsetWidth;
    feedback.dataset.visible = "true";
    globalThis.clearTimeout(feedbackTimer);
    feedbackTimer = globalThis.setTimeout(() => {
      feedback.dataset.visible = "false";
    }, 540);
  }

  function startStandalone() {
    if (!standalone) return;
    completeOverlay.hidden = true;
    lastSegment = "load";
    const now = performance.now();
    fieldView.setStandalone(Field.STANDALONE_TOKENS, now);
    task.start({ session: Task.STANDALONE_SESSION, elapsedMs: 0, nowMs: now });
    lastSnapshot = null;
    lastTargets = [];
    console.info("Prism load task: standalone 75 s run started", {
      distance_cm: geometry.distanceCm,
      screen_width_cm: geometry.screenWidthCm,
      horizontal_fov_deg: Task.horizontalFovDeg(geometry),
    });
  }

  function connect() {
    if (standalone || socket !== null) return;
    setLinkState("connecting", "CONNECTING");
    const connection = new WebSocket(socketUrl());
    socket = connection;
    blockedConnection = null;
    clockSync = new Clock.ClockSync();
    pendingBeats = [];
    connection.addEventListener("open", () => {
      if (socket !== connection) return;
      linkConnected = true;
      lastStateAt = performance.now(); // First-state deadline, not a generated host state.
      connection.send(
        JSON.stringify({ type: "hello", v: Task.VERSION, client: "task-screen", build: Task.BUILD }),
      );
      setLinkState("connecting", "WAITING FOR HOST STATE");
      sendPing(connection);
      pingTimer = globalThis.setInterval(() => sendPing(connection), Clock.PING_INTERVAL_MS);
    });
    connection.addEventListener("message", (event) => {
      if (socket !== connection || blockedConnection === connection || typeof event.data !== "string") return;
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (error) {
        console.error("Bridge sent invalid JSON", error);
        freezeFeed();
        connection.close();
        return;
      }
      const now = performance.now();
      if (message.type === "clock") {
        if (!Field.validPong(message)) {
          freezeFeed();
          connection.close();
          return;
        }
        clockSync.onPong(message, now);
        if (clockSync.ready) {
          const queued = pendingBeats;
          pendingBeats = [];
          for (const beat of queued) queueBeat(beat, now);
        }
        return;
      }
      if (message.type === "beat") {
        if (!Pulse.validBeat(message)) {
          freezeFeed();
          connection.close();
          return;
        }
        if (message.quality !== "rejected") queueBeat(message, now);
        return;
      }
      if (message.type !== "state" || !fieldView.onState(message, now)) {
        freezeFeed();
        connection.close();
        return;
      }
      lastStateAt = now;
      feedFrozen = false;
      setLinkState("connected", "BRIDGE CONNECTED");
      lastSegment = message.segment;
      const inLoad = stateGate.apply(message, now);
      if (inLoad || ["baseline", "regulate", "resolve"].includes(message.segment)) hideConnectionOverlay();
      else showWaitingOverlay();
    });
    connection.addEventListener("close", () => {
      if (socket !== connection) return;
      socket = null;
      linkConnected = false;
      globalThis.clearInterval(pingTimer);
      pingTimer = null;
      clockSync = null;
      freezeFeed();
      globalThis.clearTimeout(reconnectTimer);
      reconnectTimer = globalThis.setTimeout(connect, 1_000);
    });
    connection.addEventListener("error", () => {
      connection.close();
    });
  }

  function sendPing(connection) {
    if (socket === connection && connection.readyState === WebSocket.OPEN) {
      connection.send(JSON.stringify(clockSync.ping(performance.now())));
    }
  }

  function queueBeat(message, now) {
    if (feedFrozen) return;
    if (!clockSync.ready) {
      pendingBeats.push(message);
      if (pendingBeats.length > 64) pendingBeats.shift();
      return;
    }
    fieldView.onBeat(message, clockSync.toLocal(message.t_play), now);
  }

  function freezeFeed() {
    blockedConnection = socket;
    feedFrozen = true;
    pendingBeats = [];
    fieldView.freeze();
    stateGate.disconnect(performance.now());
    setLinkState("lost", "CONNECTION LOST");
    showLostOverlay();
  }

  function checkStateFreshness() {
    if (!linkConnected || lastStateAt === null) return;
    if (performance.now() - lastStateAt <= Field.STATE_STALE_MS) return;
    freezeFeed();
    socket?.close();
  }

  function socketUrl() {
    const configured = params.get("ws");
    if (configured) return configured;
    const protocol = globalThis.location.protocol === "https:" ? "wss:" : "ws:";
    const hostname = globalThis.location.hostname || "localhost";
    return `${protocol}//${hostname}:8787/live`;
  }

  function showWaitingOverlay() {
    if (!linkConnected && !standalone) return;
    connectionOverlay.hidden = false;
    connectionOverlay.dataset.lost = "false";
    overlayTitle.textContent = lastSegment === "reset" ? "SESSION RESETTING" : "WAITING FOR SESSION";
    overlayDetail.textContent = "The field follows the host. The task appears only in LOAD.";
  }

  function showLostOverlay() {
    connectionOverlay.hidden = false;
    connectionOverlay.dataset.lost = "true";
    overlayTitle.textContent = "CONNECTION LOST • DISPLAY FROZEN";
    overlayDetail.textContent = "The last host state is frozen. Reconnecting…";
  }

  function hideConnectionOverlay() {
    connectionOverlay.hidden = true;
    connectionOverlay.dataset.lost = "false";
  }

  function setLinkState(state, label) {
    linkDot.dataset.state = state;
    linkLabel.textContent = label;
  }

  function updateMotionLimit() {
    const width = Math.max(1, field.getBoundingClientRect().width);
    if (monitorWidthCm > 0 && monitorWidthPx > 0) {
      screenWidthCm = monitorWidthCm * width * (globalThis.devicePixelRatio || 1) / monitorWidthPx;
      geometry = Task.createGeometry(distanceCm, screenWidthCm);
    }
    geometryLabel.textContent = `${distanceCm.toFixed(1)} cm distance • ${screenWidthCm.toFixed(1)} cm screen width${
      defaultGeometry ? " • 27-inch 16:9 default" : " • configured"
    }`;
    fovLabel.textContent = `${Task.horizontalFovDeg(geometry).toFixed(2)}° horizontal field • ${
      monitorWidthCm > 0 && monitorWidthPx > 0 ? "viewport-scaled geometry" : "full-screen width assumed"
    }`;
    const margin = Math.max(70, targetSizePx() * 1.25);
    const limit = Math.abs(Task.pixelToAngle(width - margin, width, geometry));
    task.setMotionLimitDegrees(Math.max(3, limit));
  }

  function targetSizePx() {
    const raw = getComputedStyle(document.documentElement).getPropertyValue("--target-size");
    const parsed = Number.parseFloat(raw);
    return Number.isFinite(parsed) ? parsed : 58;
  }

  function positiveParameter(name, fallback) {
    if (!params.has(name)) return fallback;
    const value = Number(params.get(name));
    if (Number.isFinite(value) && value > 0) return value;
    console.warn(`Ignoring invalid ${name}=${JSON.stringify(params.get(name))}`);
    return fallback;
  }

  function requiredElement(id) {
    const element = document.getElementById(id);
    if (!element) throw new Error(`missing #${id}`);
    return element;
  }

  function PointerInput(element) {
    this.clientX = 0;
    this.clientY = 0;
    this.active = false;
    element.addEventListener("pointermove", (event) => {
      this.clientX = event.clientX;
      this.clientY = event.clientY;
      this.active = true;
    });
    element.addEventListener("pointerleave", () => {
      this.active = false;
    });
    globalThis.addEventListener("blur", () => {
      this.active = false;
    });
  }

  PointerInput.prototype.sample = function sample(rect) {
    return {
      active:
        this.active &&
        this.clientX >= rect.left &&
        this.clientX <= rect.right &&
        this.clientY >= rect.top &&
        this.clientY <= rect.bottom,
      x: this.clientX - rect.left,
      y: this.clientY - rect.top,
    };
  };
})();
