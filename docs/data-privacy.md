# Local-only physiological data

Real HR/PPI captures, booth session logs, per-person analyses, listening renders and
screenshots containing real readings must not be committed, even without names.
Use `private-data/physiology/` for captures and reports; `logs/` is also ignored.
Only the explicitly synthetic golden fixture belongs in `tools/fixtures/`.
MQTT tests generate invented packets in temporary directories, with representative
burst timing and quality faults; they require no private recording.

Enable the additional path guard in each clone:

```powershell
git config core.hooksPath .githooks
python -m tools.check_private_data
```

The pre-commit hook inspects the index, including force-added paths. The test suite
also checks the policy. Neither can identify physiological content pasted into an
arbitrary source/document file, and hooks can be bypassed. Do not force-add private
data or paste personal results into tracked documentation. Ignore rules and this
guard are safeguards, not an absolute content-leak guarantee.

## Published-history audit, 24 September 2026

After fetching the advertised remote refs, all 53 commits reachable from
`origin/main` at `7a10eb5` were checked, including deleted/renamed files and all
historical blob versions. No real physiological recordings or readings were found.
The existing golden fixture is reproducible synthetic bridge output. The design
bundles contain authored mock data, not recorded visitors; historical binaries
were fonts and built libraries. No tags were advertised.

This covers fetched, reachable remote history, not inaccessible/deleted remote
refs, third-party copies or caches. The new, unpushed capture-bearing commit was
rewritten before publication. Its recordings and derived reports were preserved
in private local storage. Git may retain that superseded local commit in its
reflog; do not publish it under another ref or share a repository backup containing
it. No already-pushed history needed rewriting.
