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

Only synthetic data belongs here. Real armband captures, derived reports and listening
renders live in gitignored `private-data/physiology/`; booth session logs stay in ignored
`logs/` or private storage. Never force-add them. Other fixture files are ignored by default.

The MQTT tests generate invented packets with deterministic timing and quality patterns in
temporary directories. They do not require, reproduce or embed a real person's recording.

For each clone, enable the repository's path guard with
`git config core.hooksPath .githooks`. The pre-commit hook and the test suite reject private
capture/log paths, including force-added files. This does not inspect arbitrary prose for
personal readings, and hooks can be bypassed: never paste real physiological results into
tracked documentation. Keep derived reports with their private captures.
