# Fixtures

`synthetic-clean.jsonl` is the default input for `tools/fake_sender.py`: one four-and-a-half
minute session recorded by `python -m tools.record_fixture`, in the session log format
written by `bridge/logging.py`.

- **The beats are real** output of the beat scheduler, driven by the synthetic armband with no
  faults.
- **The state messages are a stand-in.** The state machine, PSV and authority modules do not
  exist yet. The values obey the contract, including the authority ceilings and valence at
  exactly 0, but build clients against the stream's shape and timing, not its PSV numbers.
- Idle 10 s, baseline 45 s, load 75 s, regulate 90 s against a nominal 75 s, resolve 45 s,
  then reset. Regulate overruns on purpose, so clients meet
  `segment_elapsed_ms > segment_nominal_ms`.

Re-record once the state machine lands (prompt 2.4). The canonical real session, recorded
from the armband, will sit beside this one.
