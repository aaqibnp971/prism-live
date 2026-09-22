"""Safe reference extraction and real-browser checks of every exact design endpoint."""

import asyncio
import json

import pytest

from tools.check_field_browser import comparison_page, extract_component
from tools.check_spectator_browser import browser_path


def test_extracts_original_field_component_only():
    component = extract_component()
    assert "class Component extends DCLogic" in component
    assert "const data = [" in component
    assert "Heartbeat at 95 bpm" in component and "Heartbeat at 62 bpm" in component
    assert "this.panel(d, true, size" in component
    assert "React" not in component and "<html" not in component
    page = comparison_page(component)
    assert "DIAGNOSTIC ONLY" in page
    assert "manifest" not in page
    assert "https://" not in page and "http://" not in page


def test_extraction_uses_last_script_not_an_earlier_decoy(tmp_path):
    path = tmp_path / "bundle.html"
    decoy = '<script type="text/x-dc" data-dc-script>WRONG</script>'
    expected = "class Component extends DCLogic { /* original */ }"
    correct = f'<script type="text/x-dc" data-dc-script>{expected}</script>'
    decoy_json = json.dumps(decoy).replace("</", "<\\/")
    correct_json = json.dumps(correct).replace("</", "<\\/")
    path.write_text(
        f"<script>{decoy_json}</script><script>{correct_json}</script>",
        encoding="utf-8",
    )
    assert extract_component(path) == expected


@pytest.mark.parametrize("content", ["", "<script>not JSON</script>", "<script>{}</script>"])
def test_malformed_reference_fails_closed(tmp_path, content):
    path = tmp_path / "bad.html"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        extract_component(path)


@pytest.mark.skipif(browser_path() is None, reason="An installed Chromium browser is required")
def test_all_ten_field_frames_render_in_browser():
    from tools.check_field_browser import check

    report = asyncio.run(check(browser_path()))
    assert len(report["frames"]) == 10
    assert sum(len(frame["differences"]) for frame in report["frames"]) == 12
    assert report["frames"][0]["tokens"] == {
        "fog": 0.26,
        "light": 0.62,
        "hue": 208,
        "sat": 0.12,
        "horizon": 0.50,
        "pulse": 0.10,
        "motion": 0.008,
    }
    assert not report["external_requests"]
    # Recorded first-run means were <=.0035 for rests and <=.0066 for peaks.
    # Leave modest cross-driver headroom, but reject a visibly different field.
    for frame in report["frames"]:
        for panel in frame["differences"]:
            assert panel["rgb_mae"] < (0.012 if panel["caption"] == "Rest" else 0.018)
            assert panel["rgb_max_difference"] < 0.07
    assert len(report["rendered_pulse_safety"]) == 31
    for case in report["rendered_pulse_safety"]:
        assert case["max_relative_delta"] <= 0.22 + 1e-6
        assert case["max_absolute_delta"] <= 0.09 + 1e-6
        assert case["unchanged_outside_pixels"] > 0
