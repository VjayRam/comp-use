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


def test_act_click_uses_locator_fallback_when_primary_does_not_resolve(surface, live_server):
    # Primary strategy names a button that doesn't exist on this page at all;
    # fallback names the real "Search" button. This is the concrete proof that
    # Locator.fallback (schemas.py) is an actual robustness mechanism, not an
    # unused schema field - see _resolve_with_fallback in comp_use/surface.py.
    surface.act(ActionType.NAVIGATE, locator=None, target=f"{live_server}/member/search", text=None)
    surface.act(
        ActionType.TYPE_TEXT,
        locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "Member ID"}),
        target=None,
        text="12345",
    )
    click_locator = Locator(
        strategy=LocatorStrategy.ROLE,
        value={"role": "button", "name": "Does Not Exist On This Page"},
        fallback=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Search"}),
    )
    surface.act(ActionType.CLICK, locator=click_locator, target=None, text=None)
    assert "/member/12345" in surface.current_url()


def test_check_checkpoint_element_visible_uses_fallback(surface, live_server):
    surface.act(ActionType.NAVIGATE, locator=None, target=f"{live_server}/member/12345", text=None)
    checkpoint = Checkpoint(
        type=CheckpointType.ELEMENT_VISIBLE,
        locator=Locator(
            strategy=LocatorStrategy.ROLE,
            value={"role": "heading", "name": "Not The Real Heading"},
            fallback=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Member Detail"}),
        ),
    )
    assert surface.check_checkpoint(checkpoint) is True


# The tests below exercise _resolve_with_fallback's own chain-walking/error-surfacing
# logic against a fake page (no real browser, no Playwright waits) - a real-browser
# equivalent of "primary AND fallback both fail" would cost the full default ~30s
# Playwright timeout on the terminal attempt, which isn't worth paying for a unit test
# of pure control flow.
class _FakePage:
    """Mimics just enough of Playwright's Page for _resolve() to build a strategy-
    tagged string instead of a real Locator - lets `perform` decide success/failure
    per-strategy without any real DOM or timeout involved."""

    def get_by_role(self, role, name=None, exact=False):
        return f"role:{role}:{name}"

    def get_by_text(self, text):
        return f"text:{text}"

    def locator(self, css):
        return f"css:{css}"


def _locator(name: str, fallback: Locator | None = None) -> Locator:
    return Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": name}, fallback=fallback)


def test_resolve_with_fallback_walks_a_multi_level_chain_in_order():
    from comp_use.surface import _resolve_with_fallback

    chain = _locator("primary", fallback=_locator("middle", fallback=_locator("last")))
    attempts = []

    def perform(resolved, timeout):
        attempts.append((resolved, timeout))
        raise TimeoutError(f"not found: {resolved}")

    with pytest.raises(TimeoutError, match="not found: role:button:primary"):
        _resolve_with_fallback(_FakePage(), chain, perform)

    # every level was actually tried, in order - and only the non-terminal attempts
    # (primary, middle) got the short probe timeout; the terminal one (last) got None
    # (Playwright's own default), since there's nothing left to fall back to after it.
    assert [a for a, _ in attempts] == ["role:button:primary", "role:button:middle", "role:button:last"]
    assert [t for _, t in attempts] == [3000, 3000, None]


def test_resolve_with_fallback_returns_the_fallback_result_when_primary_fails():
    from comp_use.surface import _resolve_with_fallback

    chain = _locator("primary", fallback=_locator("backup"))

    def perform(resolved, timeout):
        if resolved == "role:button:primary":
            raise TimeoutError("not found")
        return f"clicked {resolved}"

    assert _resolve_with_fallback(_FakePage(), chain, perform) == "clicked role:button:backup"


def test_resolve_with_fallback_falls_back_on_a_falsy_result_not_only_on_exceptions():
    # Mirrors is_visible(): Playwright returns False rather than raising when nothing
    # matches, so is_success must be able to trigger the fallback too, not just a
    # raised exception.
    from comp_use.surface import _resolve_with_fallback

    chain = _locator("primary", fallback=_locator("backup"))
    visible = {"role:button:primary": False, "role:button:backup": True}

    def perform(resolved, timeout):
        return visible[resolved]

    result = _resolve_with_fallback(_FakePage(), chain, perform, is_success=lambda r: r is True)
    assert result is True


def test_resolve_with_fallback_returns_the_falsy_primary_result_when_no_fallback_declared():
    # No fallback at all: a falsy is_success result (e.g. is_visible() -> False) must
    # come back as-is, not raise - matches check_checkpoint()'s pre-existing contract
    # of returning False rather than throwing for "nothing here."
    from comp_use.surface import _resolve_with_fallback

    solo = _locator("only")

    def perform(resolved, timeout):
        return False

    assert _resolve_with_fallback(_FakePage(), solo, perform, is_success=lambda r: r is True) is False


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
