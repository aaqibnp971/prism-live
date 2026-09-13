# Fixtures

`synthetic-clean.jsonl` is the default input for `tools/fake_sender.py`: one session recorded by
`python -m tools.record_fixture`, in the session log format written by `bridge/logging.py`, the
session's own events included. `fake_sender` replays only the `beat` and `state` messages.

- **Every message is real output of the bridge**: the beat scheduler, the PSV model, and the
  session state machine with authority from `bridge/authority.py`, driven by the synthetic
  armband with no faults.
- **The heart is synthetic**: a profile of rates with white-noise variability, no breathing and
  no real person. Build clients against the stream's shape and timing, not its PSV numbers.
- Idle 10 s, then baseline, held about 6 s past its 45 s for `hr_base`, load 75 s, regulate 90 s
  against a nominal 75 s, resolve 45 s, and reset 20 s. Baseline and regulate both overrun, so
  clients meet `segment_elapsed_ms > segment_nominal_ms`.

The canonical real session, recorded from the armband, will sit beside this one.
