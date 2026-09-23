"""Reject private data paths in Git's index (also used by the pre-commit hook).

This is a path guard, not a claim that arbitrary pasted physiological content can
be detected. Real captures, reports, rendered beats and booth logs stay local.
Only the explicitly known synthetic JSONL fixture may be published.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import PurePosixPath

PRIVATE_DIRS = {
    "private-data", "capture", "captures", "recording", "recordings", "logs",
    "session-logs", "session_logs", "sessions",
}
SYNTHETIC_JSONL = "tools/fixtures/synthetic-clean.jsonl"
FIXTURE_ALLOWLIST = {SYNTHETIC_JSONL, "tools/fixtures/README.md"}


def private_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    for part in parts[:-1]:
        lower = part.lower()
        if (lower in PRIVATE_DIRS or lower.startswith(("polar-mqtt-", "verity-probe-"))
            or re.search(r"-(capture|recording)-", lower)):
            return True
    lower = normalized.lower()
    if lower.startswith("tools/fixtures/") and normalized not in FIXTURE_ALLOWLIST:
        return True
    if lower.endswith(".jsonl") and normalized != SYNTHETIC_JSONL:
        return True
    return lower.endswith((".log", "/raw.json", ".edf", ".bdf", ".fit", ".tcx"))


def tracked_private_paths() -> list[str]:
    result = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True)
    return [p for p in result.stdout.decode("utf-8").split("\0") if p and private_path(p)]


def main() -> int:
    forbidden = tracked_private_paths()
    if forbidden:
        print("COMMIT BLOCKED: private recordings/session data must not be tracked:")
        for path in forbidden:
            print(f"  {path}")
        print("Keep files in private-data/physiology/ or logs/; unstage without deleting them.")
        return 1
    print("Private-data path check passed (synthetic fixture only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
