"""Portable field mapping, mandatory heartbeat rail, and both browser integrations."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node is needed for the shared field unit tests")
def test_shared_field_javascript():
    for script in ROOT.glob("web/shared/field_*.js"):
        subprocess.run([NODE, "--check", str(script)], check=True, capture_output=True, text=True)
    scripts = sorted(str(path) for path in ROOT.glob("tests/web_field*.test.cjs"))
    assert scripts
    result = subprocess.run(
        [NODE, "--test", *scripts], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
