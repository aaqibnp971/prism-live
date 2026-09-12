// The client side of the clock exchange (docs/message-contract-v1.md §2, `clock`).
//
// A line-for-line port of bridge/clock_sync.py, which explains the two rules from contract
// v1.1 that keep the estimate from getting stuck. tests/test_clock_sync.py checks that both
// give identical offsets from the same pongs, so change them together.
//
// An estimate belongs to one connection. The bridge's T_engine starts again at 0 when it
// restarts, so build a new ClockSync on every connect; never carry one across a reconnect.
//
//   import { ClockSync, PING_INTERVAL_MS } from "../shared/clock_sync.js";
//   let sync, pinger;
//   socket.onopen = () => {
//     sync = new ClockSync();
//     socket.send(JSON.stringify(hello));
//     const ping = () => socket.send(JSON.stringify(sync.ping(performance.now())));
//     ping();
//     pinger = setInterval(ping, PING_INTERVAL_MS);
//   };
//   socket.onclose = () => clearInterval(pinger);
//   // a clock pong:  sync.onPong(msg, performance.now());
//   // a beat:        if (msg.quality !== "rejected" && sync.ready) renderAt(sync.toLocal(msg.t_play));
//   // renderAt takes a performance.now() time. A beat whose time has passed is dropped (contract §2).

export const WINDOW = 9;
export const PING_INTERVAL_MS = 2000;
export const RTT_FLOOR_MS = 2;
export const MAX_RTT_MS = 5000;

export class ClockSync {
  #offsets = [];
  #rtts = [];

  ping(tClientNow) {
    return { type: "clock", v: 1, role: "ping", t_client_sent: tClientNow };
  }

  // Take one pong, received at tClientNow on this client's clock. True if it was used.
  onPong(pong, tClientNow) {
    const rtt = tClientNow - pong.t_client_sent;
    if (!(rtt >= 0 && rtt <= MAX_RTT_MS)) return false;
    const limit = this.#rtts.length ? 2 * Math.max(median(this.#rtts), RTT_FLOOR_MS) : Infinity;
    keepLast(this.#rtts, rtt);
    if (rtt > limit) return false;
    keepLast(this.#offsets, pong.t_engine + rtt / 2 - tClientNow);
    return true;
  }

  get ready() {
    return this.#offsets.length > 0;
  }

  get samples() {
    return this.#offsets.length;
  }

  // T_engine minus this client's clock, in ms. null until the first pong is used.
  get offset() {
    return this.#offsets.length ? median(this.#offsets) : null;
  }

  // A laptop time, such as t_play, on this client's clock.
  toLocal(tEngine) {
    return tEngine - this.#requireOffset();
  }

  toEngine(tLocal) {
    return tLocal + this.#requireOffset();
  }

  #requireOffset() {
    if (!this.#offsets.length) throw new Error("no clock sample yet: ping, and wait for a pong");
    return median(this.#offsets);
  }
}

function keepLast(values, value) {
  values.push(value);
  if (values.length > WINDOW) values.shift();
}

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}
