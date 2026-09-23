import subprocess

import pytest

from tools.check_private_data import private_path, tracked_private_paths


@pytest.mark.parametrize("path", [
    "private-data/physiology/captures/raw.jsonl", "logs/booth/session.jsonl",
    "web/logs/person.csv", "captures/hr.csv", "nested/recordings/beats.wav",
    "sessions/visitor.json", "tools/fixtures/polar-mqtt-20260101/raw.jsonl",
    "tools/fixtures/verity-probe-report.md", "elsewhere/raw.jsonl",
    "Tools/Fixtures/private.csv", "archive/session.edf",
])
def test_private_paths_cannot_be_tracked_even_after_force_add(path):
    assert private_path(path)


@pytest.mark.parametrize("path", [
    "tools/fixtures/README.md", "tools/fixtures/synthetic-clean.jsonl",
    "tests/synthetic_ppi_capture.py", "tools/render_ppi_capture.py",
    "bridge/session.py", "assets/placeholders/bed.wav",
])
def test_code_and_explicit_synthetic_fixture_are_not_private(path):
    assert not private_path(path)


def test_index_contains_no_private_recording_paths():
    assert tracked_private_paths() == []


def test_ignore_rules_cover_future_captures_and_booth_session_logs():
    paths = ["private-data/new.json", "logs/booth/session.jsonl", "nested/captures/ppi.csv",
             "nested/recordings/heartbeat.wav", "session-logs/visitor.json",
             "tools/fixtures/new-person.json", "elsewhere/raw.jsonl"]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-z", "--stdin"],
        input=("\0".join(paths) + "\0").encode(), capture_output=True, check=True,
    )
    assert result.stdout.decode().rstrip("\0").split("\0") == paths
