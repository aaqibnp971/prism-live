/*
 * Browser shell for prompt 3.1.
 *
 * The ramp was authored for eye control, but Quest 3S has no eye tracking. Pointer and later
 * head-reticle control use these provisional values until Week E. All timing and difficulty live
 * in task.js; this file only supplies pointer input, drawing, state gating and an event sink.
 */

(function runTaskScreen() {
  "use strict";

  const Task = globalThis.PrismLoadTask;
  if (!Task) throw new Error("task.js did not load");

  const params = new URLSearchParams(globalThis.location.search);
  const standalone = params.has("standalone") && params.get("standalone") !== "0";
  const distanceCm = positiveParameter("distance_cm", Task.DEFAULT_DISTANCE_CM);
  const screenWidthCm = positiveParameter("screen_width_cm", Task.DEFAULT_SCREEN_WIDTH_CM);
  const geometry = Task.createGeometry(distanceCm, screenWidthCm);

  const field = requiredElement("field");
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

  const task = new Task.LoadTask({ emit: deliverTaskEvent });
  const stateGate = new Task.StateGate(task);

  const defaultGeometry = !params.has("distance_cm") && !params.has("screen_width_cm");
  geometryLabel.textContent = `${distanceCm.toFixed(1)} cm distance • ${screenWidthCm.toFixed(1)} cm screen width${
    defaultGeometry ? " • 27-inch 16:9 default" : " • configured"
  }`;
  fovLabel.textContent = `${Task.horizontalFovDeg(geometry).toFixed(2)}° horizontal field • full-screen width assumed`;

  fullscreenButton.addEventListener("click", () => {
    field.requestFullscreen?.().catch((error) => console.warn("Fullscreen was refused", error));
  });
  restartButton.addEventListener("click", startStandalone);
  debugRestartButton.addEventListener("click", startStandalone);
  globalThis.addEventListener("keydown", (event) => {
    if (standalone && event.key.toLowerCase() === "r") startStandalone();
  });
  globalThis.addEventListener("resize", updateMotionLimit);

  if (standalone) {
    debugRestartButton.hidden = false;
    setLinkState("connected", "STANDALONE • EVENTS → CONSOLE");
    hideConnectionOverlay();
    startStandalone();
  } else {
    setLinkState("connecting", "CONNECTING");
    showWaitingOverlay();
    connect();
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
    task.start({ session: Task.STANDALONE_SESSION, elapsedMs: 0, nowMs: performance.now() });
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
    connection.addEventListener("open", () => {
      if (socket !== connection) return;
      linkConnected = true;
      connection.send(
        JSON.stringify({ type: "hello", v: Task.VERSION, client: "task-screen", build: Task.BUILD }),
      );
      setLinkState("connected", "BRIDGE CONNECTED");
      if (!task.active) showWaitingOverlay();
    });
    connection.addEventListener("message", (event) => {
      if (socket !== connection || typeof event.data !== "string") return;
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (error) {
        console.error("Bridge sent invalid JSON", error);
        connection.close();
        return;
      }
      if (message.type !== "state") return;
      lastSegment = message.segment;
      const inLoad = stateGate.apply(message, performance.now());
      if (inLoad) hideConnectionOverlay();
      else showWaitingOverlay();
    });
    connection.addEventListener("close", () => {
      if (socket !== connection) return;
      socket = null;
      linkConnected = false;
      stateGate.disconnect(performance.now());
      setLinkState("lost", "CONNECTION LOST");
      showLostOverlay();
      globalThis.clearTimeout(reconnectTimer);
      reconnectTimer = globalThis.setTimeout(connect, 1_000);
    });
    connection.addEventListener("error", () => {
      connection.close();
    });
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
    overlayTitle.textContent = "WAITING FOR LOAD";
    overlayDetail.textContent = "The task starts only when the host enters LOAD.";
  }

  function showLostOverlay() {
    connectionOverlay.hidden = false;
    connectionOverlay.dataset.lost = "true";
    overlayTitle.textContent = "CONNECTION LOST • TASK PAUSED";
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
