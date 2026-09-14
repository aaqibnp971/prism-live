# miniaudio (vendored)

- **Version:** 0.11.25 (release 2026-03-03)
- **Source:** https://raw.githubusercontent.com/mackron/miniaudio/0.11.25/miniaudio.h
- **SHA-256:** `ac7af4de748b7e26b777f37e01cee313a308a7296a3eb080e2906b320cc55c89`
- **License:** dual public-domain / MIT No Attribution (see header)

Single-file library for audio device I/O and WAV/FLAC decode on every target platform
(desktop, Android/AAudio, iOS/CoreAudio, embedded Linux). The implementation is compiled
once in `miniaudio.c` (this directory) into the `miniaudio` static target.

To update: replace `miniaudio.h` with a newer pinned release, record the new version and
SHA-256 here, and re-run the full test suite. Updating is a reviewed change like any other
dependency change (CLAUDE.md: stack is locked).
