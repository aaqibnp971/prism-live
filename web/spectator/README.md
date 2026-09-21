# Spectator screen

Open `index.html` full-screen on the spectator display while the bridge is running. It connects to
`ws://localhost:8787/live` by default. To point it at another live bridge, use only the transport
override, for example:

```text
index.html?ws=ws://192.168.1.20:8787/live
```

There is deliberately no simulation or standalone mode. For development, run
`python -m tools.fake_sender --loop`; the page still receives a real, contract-checked WebSocket
feed and shows its connection state.

The page has two idle views:

- Cold idle shows a quiet ready state, with no progress, readings or trace.
- After a completed 20-second reset, the completed beat trace remains through idle and is cleared
  only when the host announces the next baseline. A 3-second stopped/failed reset is not presented
  as a completed session.

The held trace exists only in the browser's memory because the frozen contract has no history
message. Reloading the page in idle therefore returns to cold idle. During a session, a page opened
late begins its trace at the next live beat and never invents earlier samples.

If the socket closes, a message is invalid, or no state arrives for 2.5 seconds (including the
first state), the last verified view freezes under a full-width red `LIVE FEED LOST` banner. With
no verified view yet, the banner says `LIVE FEED UNAVAILABLE`. Reconnection happens once per second;
the banner leaves only after a valid host state arrives.
