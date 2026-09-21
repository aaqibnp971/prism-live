"""Layout and live wiring in a real browser; retained bounds regression test."""

import asyncio

import pytest

from tools.check_spectator_browser import browser_path, check


@pytest.mark.skipif(browser_path() is None, reason="An installed Chromium browser is required")
def test_spectator_live_layout_and_freeze():
    asyncio.run(check(browser_path()))
