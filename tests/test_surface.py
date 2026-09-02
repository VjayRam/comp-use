import threading
import time

import pytest
from playwright.sync_api import sync_playwright

from comp_use.schemas import ActionType, Checkpoint, CheckpointType, Locator, LocatorStrategy
from comp_use.surface import PlaywrightSurface, safe_screenshot
from mock_app.app import create_app


@pytest.fixture(scope="module")
def live_server():
    app = create_app()
    server = threading.Thread(
        target=lambda: app.run(port=5099, use_reloader=False), daemon=True
    )
    server.start()
    time.sleep(0.5)
    yield "http://localhost:5099"


@pytest.fixture
def surface(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        yield PlaywrightSurface(page)
        browser.close()


def test_observe_returns_accessibility_tree_text(surface, live_server):
    surface.act(ActionType.NAVIGATE, locator=None, target=f"{live_server}/member/search", text=None)
    state = surface.observe()
    assert "textbox" in state.accessibility_tree.lower()
    assert state.url.endswith("/member/search")


def test_act_type_text_and_click_navigates_to_detail(surface, live_server):
    surface.act(ActionType.NAVIGATE, locator=None, target=f"{live_server}/member/search", text=None)
    surface.act(
        ActionType.TYPE_TEXT,
        locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "Member ID"}),
        target=None,
        text="12345",
    )
    surface.act(
        ActionType.CLICK,
        locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Search"}),
        target=None,
        text=None,
    )
    assert "/member/12345" in surface.current_url()


def test_check_checkpoint_element_visible(surface, live_server):
    surface.act(ActionType.NAVIGATE, locator=None, target=f"{live_server}/member/12345", text=None)
    checkpoint = Checkpoint(
        type=CheckpointType.ELEMENT_VISIBLE,
        locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Member Detail"}),
    )
    assert surface.check_checkpoint(checkpoint) is True


def test_get_heading_text_returns_the_page_heading(surface, live_server):
    surface.act(ActionType.NAVIGATE, locator=None, target=f"{live_server}/member/12345", text=None)
    assert surface.get_heading_text() == "Member Detail"


def test_get_heading_text_returns_none_when_no_heading_exists(surface, live_server):
    # /member/search has no <h1>/heading role - this is the path
    # cli._derive_success_checkpoint falls back to a literal URL match on.
    surface.act(ActionType.NAVIGATE, locator=None, target=f"{live_server}/member/search", text=None)
    assert surface.get_heading_text() is None


class _RaisingSurface:
    def screenshot(self):
        raise TimeoutError("Page.screenshot: Timeout 30000ms exceeded.")


class _WorkingSurface:
    def screenshot(self):
        return b"realpng"


def test_safe_screenshot_returns_none_when_capture_raises():
    # Reproduces a real live crash: Page.screenshot() itself can time out
    # (seen live, unrelated to any locator/action) - this call is best-effort
    # evidence/context and must never be allowed to crash an otherwise-healthy
    # or otherwise-failing run.
    assert safe_screenshot(_RaisingSurface()) is None


def test_safe_screenshot_returns_bytes_when_capture_succeeds():
    assert safe_screenshot(_WorkingSurface()) == b"realpng"
