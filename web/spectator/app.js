/* Live-only browser shell for the Prism spectator screen (prompts 3.3 / 3.5). */

(function runSpectator() {
  "use strict";

  const Spectator = globalThis.PrismSpectator;
  const Clock = globalThis.PrismClockSync;
  const Field = globalThis.PrismFieldView;
  if (!Spectator) throw new Error("spectator model did not load");
  if (!Clock) throw new Error("clock synchroniser did not load");
  if (!Field) throw new Error("shared field did not load");

  const model = new Spectator.SpectatorModel();
  const params = new URLSearchParams(globalThis.location.search);
  const elements = collectElements();
  const fieldView = new Field.FieldView(required("field-canvas"), {
    onError(error) {
      required("field-error").hidden = false;
      console.error("FIELD UNAVAILABLE", error);
    },
  });
  const scheduledBeats = new Set();
  const stageSegments = ["baseline", "load", "regulate", "resolve"];
  let pendingBeats = [];
  let socket = null;
  let clockSync = null;
  let pingTimer = null;
  let reconnectTimer = null;
  let lastStateAt = null;
  let hasState = false;
  let feedFrozen = false;
  let failedOnce = false;
  let blockedConnection = null;

  elements.fullscreenButton.addEventListener("click", () => {
    document.documentElement.requestFullscreen?.().catch((error) => {
      console.warn("Fullscreen was refused", error);
    });
  });
  globalThis.addEventListener("resize", render);
  globalThis.addEventListener("beforeunload", cleanupConnection);

  render();
  connect();
  globalThis.setInterval(checkStateFreshness, 200);

  function connect() {
    if (socket !== null) return;
    globalThis.clearTimeout(reconnectTimer);
    if (failedOnce) {
      setLostConnection(
        "RECONNECTING • LIVE FEED LOST",
        hasState
          ? "The last verified host state remains frozen."
          : "No verified host state is available yet.",
      );
    } else {
      setWaitingConnection("WAITING FOR LIVE BRIDGE");
    }

    const connection = new WebSocket(socketUrl());
    socket = connection;
    blockedConnection = null;
    clockSync = new Clock.ClockSync();
    pendingBeats = [];

    connection.addEventListener("open", () => {
      if (socket !== connection) return;
      feedFrozen = hasState;
      if (!hasState) lastStateAt = performance.now(); // first-state deadline, not a fake state
      connection.send(
        JSON.stringify({
          type: "hello",
          v: Spectator.VERSION,
          client: "spectator",
          build: Spectator.BUILD,
        }),
      );
      sendPing(connection);
      pingTimer = globalThis.setInterval(
        () => sendPing(connection),
        Clock.PING_INTERVAL_MS,
      );
      updateConnectionDisplay();
    });

    connection.addEventListener("message", (event) => {
      if (
        socket !== connection ||
        blockedConnection === connection ||
        typeof event.data !== "string"
      ) {
        return;
      }
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (error) {
        failConnection(connection, "INVALID LIVE DATA • DISPLAY FROZEN", error);
        return;
      }

      if (message.type === "clock") {
        if (!Spectator.validPong(message)) {
          failConnection(connection, "INVALID CLOCK DATA • DISPLAY FROZEN");
          return;
        }
        clockSync.onPong(message, performance.now());
        if (clockSync.ready) flushPendingBeats(connection);
        return;
      }

      if (message.type === "state") {
        if (!model.onState(message)) {
          failConnection(connection, "INVALID STATE DATA • DISPLAY FROZEN");
          return;
        }
        lastStateAt = performance.now();
        fieldView.onState(message, lastStateAt);
        hasState = true;
        feedFrozen = false;
        failedOnce = false;
        render();
        updateConnectionDisplay();
        return;
      }

      if (message.type === "beat") {
        if (!Spectator.validBeat(message)) {
          failConnection(connection, "INVALID BEAT DATA • DISPLAY FROZEN");
          return;
        }
        if (message.quality !== "rejected") queueBeat(connection, message);
        return;
      }

      failConnection(connection, "UNKNOWN LIVE DATA • DISPLAY FROZEN");
    });

    connection.addEventListener("close", () => {
      if (socket !== connection) return;
      model.markInterrupted();
      socket = null;
      failedOnce = true;
      feedFrozen = hasState;
      cleanupConnection();
      setLostConnection(
        hasState ? "LIVE FEED LOST • DISPLAY FROZEN" : "LIVE FEED UNAVAILABLE",
        hasState
          ? "The last verified host state is frozen. Reconnecting…"
          : "No verified host state is available. Reconnecting…",
      );
      reconnectTimer = globalThis.setTimeout(connect, 1_000);
    });

    connection.addEventListener("error", () => {
      connection.close();
    });
  }

  function sendPing(connection) {
    if (socket !== connection || connection.readyState !== WebSocket.OPEN) return;
    connection.send(JSON.stringify(clockSync.ping(performance.now())));
  }

  function queueBeat(connection, message) {
    if (!clockSync.ready) {
      pendingBeats.push(message);
      if (pendingBeats.length > 64) pendingBeats.shift();
      return;
    }
    scheduleBeat(connection, message);
  }

  function flushPendingBeats(connection) {
    const queued = pendingBeats;
    pendingBeats = [];
    for (const message of queued) scheduleBeat(connection, message);
  }

  function scheduleBeat(connection, message) {
    const localPlay = clockSync.toLocal(message.t_play);
    const receivedAt = performance.now();
    const delay = localPlay - receivedAt;
    if (delay < 0) return; // The contract says a late beat is dropped, never caught up.
    if (!feedFrozen) fieldView.onBeat(message, localPlay, receivedAt);
    const timer = globalThis.setTimeout(() => {
      scheduledBeats.delete(timer);
      const link = Spectator.linkState(
        socket === connection && connection.readyState === WebSocket.OPEN,
        hasState,
        lastStateAt,
        performance.now(),
      );
      if (feedFrozen || link !== "live") return;
      if (model.onBeat(message)) render();
    }, delay);
    scheduledBeats.add(timer);
  }

  function checkStateFreshness() {
    const connection = socket;
    if (connection === null || feedFrozen) return;
    const state = Spectator.linkState(
      connection.readyState === WebSocket.OPEN,
      hasState,
      lastStateAt,
      performance.now(),
    );
    if (state !== "lost") return;
    feedFrozen = true;
    blockedConnection = connection;
    clearScheduledBeats();
    setLostConnection(
      hasState ? "LIVE FEED LOST • DISPLAY FROZEN" : "LIVE FEED UNAVAILABLE",
      hasState
        ? "No host state arrived for 2.5 seconds. Reconnecting…"
        : "The bridge opened but sent no state for 2.5 seconds. Reconnecting…",
    );
    connection.close();
  }

  function failConnection(connection, title, error = null) {
    if (error) console.error(title, error);
    else console.error(title);
    feedFrozen = hasState;
    blockedConnection = connection;
    clearScheduledBeats();
    setLostConnection(
      title,
      hasState
        ? "The last verified host state is frozen. Reconnecting…"
        : "No verified host state is available. Reconnecting…",
    );
    connection.close();
  }

  function cleanupConnection() {
    globalThis.clearInterval(pingTimer);
    pingTimer = null;
    pendingBeats = [];
    clearScheduledBeats();
    clockSync = null;
  }

  function clearScheduledBeats() {
    fieldView.freeze();
    for (const timer of scheduledBeats) globalThis.clearTimeout(timer);
    scheduledBeats.clear();
  }

  function render() {
    elements.stage.style.setProperty("--stage-scale", Math.min(innerWidth / 1920, innerHeight / 1080));
    const view = model.view();
    document.body.dataset.view = view.kind;
    elements.idleCold.hidden = view.kind !== "idle-cold" && view.kind !== "waiting";
    elements.resettingPanel.hidden = view.kind !== "resetting";
    const held = view.kind === "idle-trace" || view.kind === "trace-hold";
    elements.traceHold.hidden = !held && !view.reveal;
    elements.activeDashboard.hidden = view.kind !== "active" || view.reveal;
    elements.segmentStrip.hidden = view.kind !== "active";
    elements.segmentTiming.hidden = view.kind !== "active";

    elements.headerSegment.textContent = headerFor(view);
    elements.headerSession.textContent =
      (view.kind === "idle-trace" || view.kind === "trace-hold"
        ? view.traceSession
        : view.session) || "WAITING FOR DATA";

    elements.idleCopy.textContent =
      view.kind === "idle-cold"
        ? "The screen is connected. Your four minutes begin when the attendant starts the session."
        : "Waiting for a verified state from the live bridge.";
    elements.idleFoot.textContent =
      view.kind === "idle-cold" ? "NO PROGRESS YET • NO PREVIOUS READINGS" : "NO LIVE DATA YET";

    if (view.kind === "active") renderActive(view);
    else {
      elements.segmentTitle.textContent = view.kind === "resetting" ? "RESETTING" : "READY";
      elements.segmentVerb.textContent = view.kind === "waiting" ? "WAITING FOR LIVE DATA" : "WAITING FOR YOUR NEXT SESSION";
      elements.sessionTime.textContent = "—:—";
    }
    if (held || view.reveal) renderReveal(view, held);
  }

  function renderActive(view) {
    elements.segmentTitle.textContent = view.segment.toUpperCase();
    elements.headerSegment.textContent = `SEGMENT ${String(stageSegments.indexOf(view.segment) + 1).padStart(2, "0")} / 4`;
    elements.segmentVerb.textContent = view.verb;
    const progress = view.progress;
    elements.sessionTime.textContent = `T+${duration(view.sessionElapsedMs)}`;
    elements.segmentTime.textContent = duration(progress.elapsedMs);
    elements.segmentNominal.textContent = `OF ${duration(progress.nominalMs)}`;
    const currentSegment = stageSegments.indexOf(view.segment);
    for (const [index, segment] of stageSegments.entries()) {
      const fill = index < currentSegment ? 1 : index === currentSegment ? progress.fraction : 0;
      const bar = elements.segmentProgress[segment];
      bar.style.width = `${fill * 100}%`;
      bar.parentElement.parentElement.setAttribute("aria-current", index === currentSegment ? "step" : "false");
    }
    elements.progressNote.textContent = progress.overrunMs
      ? `· HOLD +${(progress.overrunMs / 1000).toFixed(0)} S`
      : "· SEGMENT";
    elements.baselineLearning.hidden = view.segment !== "baseline";
    elements.baselineLearningFill.style.width = `${progress.fraction * 100}%`;
    elements.railNote.textContent = view.segment === "baseline"
      ? "Learning your baseline. The hatch shows uncertainty in each reading."
      : "Your measured pulse and task inputs. Under each: how far it may move your world.";

    const readings = view.readings;
    elements.heartBpm.textContent = bpm(readings.heartBpm);
    elements.restingBpm.textContent = bpm(readings.restingBpm);
    elements.signalQuality.textContent = `${Math.round(readings.acceptedFraction * 100)}% ACCEPTED`;

    for (const dimension of readings.dimensions) {
      const value = elements.dimensions[dimension.key];
      const treatment = Spectator.readingTreatment(dimension);
      value.reading.textContent = dimension.key === "valence" || treatment.readable ? dimension.value.toFixed(2) : "—";
      value.confidence.textContent = dimension.confidence.toFixed(2);
      value.authority.textContent = dimension.authority.toFixed(2);
      value.readingBar.style.width = `${treatment.fill * 100}%`;
      value.uncertainty.style.left = `${treatment.left * 100}%`;
      value.uncertainty.style.width = `${treatment.width * 100}%`;
      value.marker.hidden = !treatment.readable;
      value.marker.style.left = `${dimension.value * 100}%`;
      value.authorityBar.style.width = `${dimension.authority * 100}%`;
    }

    elements.traceCount.textContent = view.trace.length
      ? `${view.trace.length} BEATS SHOWN`
      : "WAITING FOR YOUR FIRST BEAT";
    elements.traceEmpty.hidden = view.trace.length > 0;
    renderTrace("live", view.trace, 1_000, 210, readings.restingBpm);
  }

  function renderReveal(view, held) {
    const summary = view.summary;
    elements.revealStart.textContent = preciseBpm(summary.satDownAt);
    elements.revealPeak.textContent = preciseBpm(summary.peakedAt);
    elements.revealEnd.textContent = preciseBpm(summary.leftAt);
    elements.revealEquation.textContent = `${preciseBpm(summary.peakedAt)} − ${preciseBpm(summary.leftAt)} = `;
    elements.revealDifference.textContent = preciseBpm(summary.difference);
    elements.revealStatus.textContent = (summary.partialHistory ? "PARTIAL TRACE · MISSING SESSION HISTORY · " : "") +
      (held ? "YOUR SESSION TRACE · HELD UNTIL THE NEXT BASELINE" : "YOUR SESSION TRACE · STILL RECORDING");
    elements.revealEndNote.textContent = held ? "YOUR FINAL OBSERVED RESOLVE BEAT" : "YOUR LATEST RESOLVE BEAT · UPDATING";
    elements.revealEmpty.hidden = view.trace.length > 0;
    for (const key of Spectator.DIMENSIONS) {
      const authority = summary.authority[key]; // Observed maximum, never a client ceiling.
      const target = elements.authorityHistory[key];
      target.value.textContent = authority.toFixed(2);
      target.bar.style.width = `${authority * 100}%`;
      if (target.status) target.status.textContent = authority > 0 ? "HAD AUTHORITY" : "NONE OBSERVED";
    }
    renderTrace("held", view.trace, 1_000, 420, summary.restingBpm);
  }

  function preciseBpm(value) {
    return value === null ? "—" : value.toFixed(1).replace("-", "−");
  }

  function renderTrace(which, trace, width, height, restingBpm = null) {
    const target = which === "held" ? elements.heldTrace : elements.liveTrace;
    const scale = Spectator.traceScale(trace, restingBpm);
    const points = Spectator.tracePoints(trace, width, height, 12, restingBpm);
    const path = points.length
      ? points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(" ")
      : "";
    target.line.setAttribute("d", path);
    target.glow.setAttribute("d", path);
    target.maximum.textContent = scale ? Math.round(scale.maximum) : "—";
    target.minimum.textContent = scale ? Math.round(scale.minimum) : "—";
    const referenceY = Spectator.referenceY(trace, restingBpm, height);
    target.restingReference.toggleAttribute("hidden", referenceY === null);
    target.restingLabel.hidden = referenceY === null;
    if (referenceY !== null) {
      target.restingLine.setAttribute("d", `M0 ${referenceY.toFixed(2)}H${width}`);
      target.restingLabel.style.top = `${referenceY / height * 100}%`;
      target.restingLabel.style.transform = referenceY < 36 ? "translateY(4px)" : "translateY(calc(-100% - 4px))";
      target.restingLabel.textContent = `YOUR RESTING RATE ${preciseBpm(restingBpm)}`;
    } else {
      target.restingLine.setAttribute("d", "");
      target.restingLabel.textContent = "";
    }
    target.end.toggleAttribute("hidden", points.length === 0);
    if (points.length) {
      const last = points.at(-1);
      target.end.setAttribute("cx", last.x.toFixed(2));
      target.end.setAttribute("cy", last.y.toFixed(2));
    }
  }

  function updateConnectionDisplay() {
    if (feedFrozen) return;
    const open = socket?.readyState === WebSocket.OPEN;
    const state = Spectator.linkState(open, hasState, lastStateAt, performance.now());
    if (state === "live") {
      elements.connectionBanner.hidden = true;
      elements.linkBadge.dataset.state = "live";
      elements.linkLabel.textContent = "LIVE FEED";
    } else if (failedOnce) {
      setLostConnection(
        "RECONNECTING • LIVE FEED LOST",
        hasState
          ? "The last verified host state remains frozen."
          : "No verified host state is available yet.",
      );
    } else {
      setWaitingConnection(open ? "WAITING FOR LIVE DATA" : "WAITING FOR LIVE BRIDGE");
    }
  }

  function setWaitingConnection(title) {
    elements.connectionBanner.hidden = false;
    elements.connectionBanner.dataset.mode = "waiting";
    elements.connectionTitle.textContent = title;
    elements.connectionDetail.textContent = "The display will use only verified host messages.";
    elements.linkBadge.dataset.state = "waiting";
    elements.linkLabel.textContent = failedOnce ? "RECONNECTING" : "CONNECTING";
  }

  function setLostConnection(title, detail) {
    elements.connectionBanner.hidden = false;
    elements.connectionBanner.dataset.mode = "lost";
    elements.connectionTitle.textContent = title;
    elements.connectionDetail.textContent = detail;
    elements.linkBadge.dataset.state = "lost";
    elements.linkLabel.textContent = "FEED LOST";
  }

  function socketUrl() {
    const configured = params.get("ws");
    if (configured) return configured;
    const protocol = globalThis.location.protocol === "https:" ? "wss:" : "ws:";
    const hostname = globalThis.location.hostname || "localhost";
    return `${protocol}//${hostname}:8787/live`;
  }

  function headerFor(view) {
    if (view.kind === "active") return view.title;
    if (view.kind === "idle-trace" || view.kind === "trace-hold") return "LAST SESSION TRACE";
    if (view.kind === "resetting") return "SYSTEM RESET";
    return "LIVE SYSTEM";
  }

  function bpm(value) {
    return value === null ? "—" : Math.round(value).toString();
  }

  function duration(milliseconds) {
    const seconds = Math.max(0, Math.round(milliseconds / 1000));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function required(id) {
    const element = document.getElementById(id);
    if (!element) throw new Error(`missing #${id}`);
    return element;
  }

  function traceElements(prefix) {
    return {
      line: required(`${prefix}-trace-line`),
      glow: required(`${prefix}-trace-glow`),
      end: required(`${prefix}-trace-end`),
      maximum: required(`${prefix}-trace-max`),
      minimum: required(`${prefix}-trace-min`),
      restingReference: required(`${prefix}-resting-reference`),
      restingLine: required(`${prefix}-resting-line`),
      restingLabel: required(`${prefix}-resting-label`),
    };
  }

  function collectElements() {
    const dimensions = {};
    const authorityHistory = {};
    for (const key of Spectator.DIMENSIONS) {
      authorityHistory[key] = {
        value: required(`history-${key}-value`),
        bar: required(`history-${key}-bar`),
        status: key === "valence" ? null : required(`history-${key}-status`),
      };
      dimensions[key] = {
        reading: required(`${key}-value`),
        confidence: required(`${key}-confidence`),
        authority: required(`${key}-authority`),
        readingBar: required(`${key}-reading-bar`),
        uncertainty: required(`${key}-uncertainty`),
        marker: required(`${key}-marker`),
        authorityBar: required(`${key}-authority-bar`),
      };
    }
    return {
      activeDashboard: required("active-dashboard"),
      stage: required("spectator-stage"),
      baselineLearning: required("baseline-learning"),
      baselineLearningFill: required("baseline-learning-fill"),
      connectionBanner: required("connection-banner"),
      connectionDetail: required("connection-detail"),
      connectionTitle: required("connection-title"),
      dimensions,
      authorityHistory,
      fullscreenButton: required("fullscreen-button"),
      headerSegment: required("header-segment"),
      headerSession: required("header-session"),
      heartBpm: required("heart-bpm"),
      heldTrace: traceElements("held"),
      idleCold: required("idle-cold"),
      idleCopy: required("idle-copy"),
      idleFoot: required("idle-foot"),
      linkBadge: required("link-badge"),
      linkLabel: required("link-label"),
      liveTrace: traceElements("live"),
      segmentStrip: required("segment-strip"),
      segmentTiming: required("segment-timing"),
      segmentProgress: Object.fromEntries(["baseline", "load", "regulate", "resolve"].map((key) => [key, required(`${key}-progress`)])),
      sessionTime: required("session-time"),
      railNote: required("rail-note"),
      progressNote: required("progress-note"),
      resettingPanel: required("resetting-panel"),
      restingBpm: required("resting-bpm"),
      segmentNominal: required("segment-nominal"),
      segmentTime: required("segment-time"),
      segmentTitle: required("segment-title"),
      segmentVerb: required("segment-verb"),
      signalQuality: required("signal-quality"),
      traceCount: required("trace-count"),
      traceEmpty: required("trace-empty"),
      traceHold: required("trace-hold"),
      revealStart: required("reveal-start"),
      revealPeak: required("reveal-peak"),
      revealEnd: required("reveal-end"),
      revealEndNote: required("reveal-end-note"),
      revealDifference: required("reveal-difference"),
      revealEquation: required("reveal-equation"),
      revealStatus: required("reveal-status"),
      revealEmpty: required("reveal-empty"),
    };
  }
})();
