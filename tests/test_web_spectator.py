from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute spectator unit tests")
def test_spectator_javascript() -> None:
    for script in (
        "web/shared/clock_sync_core.js",
        "web/spectator/model.js",
        "web/spectator/app.js",
    ):
        subprocess.run(
            [NODE, "--check", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    result = subprocess.run(
        [NODE, "--test", "tests/web_spectator.test.cjs"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_spectator_page_contains_only_live_honest_labels() -> None:
    html = (ROOT / "web/spectator/index.html").read_text(encoding="utf-8")
    app = (ROOT / "web/spectator/app.js").read_text(encoding="utf-8")
    combined = f"{html}\n{app}"

    for required in (
        "YOUR HEART",
        "YOUR RESTING RATE",
        "YOUR FOUR MINUTES",
        "THE SAME NUMBERS, DRAWN HERE",
        "NOT READABLE",
        "LIVE FEED LOST • DISPLAY FROZEN",
    ):
        assert required in combined

    for forbidden in (
        "NO SIGNAL",
        "LIVE FROM THE HEADSET",
        "GUIDING HER DOWN",
        "simulation",
        "standalone",
    ):
        assert forbidden.lower() not in combined.lower()

    assert 'client: "spectator"' in app
    assert "new WebSocket" in app
    assert "STATE_STALE_MS" not in app  # the tested model owns the timeout
    assert "Math.sin" not in combined
