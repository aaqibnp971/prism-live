from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute the browser task unit tests")
def test_browser_task_javascript() -> None:
    for script in ("web/task/task.js", "web/task/app.js"):
        subprocess.run(
            [NODE, "--check", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    result = subprocess.run(
        [NODE, "--test", "tests/web_task.test.cjs"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_task_page_keeps_standalone_and_live_on_one_model() -> None:
    html = (ROOT / "web/task/index.html").read_text(encoding="utf-8")
    app = (ROOT / "web/task/app.js").read_text(encoding="utf-8")
    assert html.index('src="task.js"') < html.index('src="app.js"')
    assert app.count("new Task.LoadTask") == 1
    assert '?standalone=1' in (ROOT / "web/task/README.md").read_text(encoding="utf-8")
