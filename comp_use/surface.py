from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

from comp_use.schemas import ActionType, Checkpoint, CheckpointType, Locator, LocatorStrategy


@dataclass
class ObservedState:
    accessibility_tree: str
    url: str


def _resolve(page: Page, locator: Locator):
    if locator is None:
        raise ValueError("locator is required for this action")
    if locator.strategy == LocatorStrategy.ROLE:
        role = locator.value["role"]
        name = locator.value.get("name")
        if name:
            return page.get_by_role(role, name=name, exact=True)
        return page.get_by_role(role)
    if locator.strategy == LocatorStrategy.TEXT:
        return page.get_by_text(locator.value["text"])
    if locator.strategy == LocatorStrategy.CSS:
        return page.locator(locator.value["css"])
    raise ValueError(f"unknown locator strategy: {locator.strategy}")


def safe_screenshot(surface: "Surface") -> bytes | None:
    """Screenshot capture is best-effort evidence/context, never load-bearing
    for correctness - a Playwright screenshot call can itself time out (a real,
    live-observed failure: Page.screenshot() timing out independent of any
    locator/action problem) and must never be allowed to crash a run that
    would otherwise complete, fail cleanly, or escalate."""
    try:
        return surface.screenshot()
    except Exception:
        return None


class Surface:
    def observe(self) -> ObservedState:
        raise NotImplementedError

    def act(self, action: ActionType, locator: Locator | None, target: str | None, text: str | None) -> str | None:
        raise NotImplementedError

    def check_checkpoint(self, checkpoint: Checkpoint) -> bool:
        raise NotImplementedError

    def screenshot(self) -> bytes:
        raise NotImplementedError

    def current_url(self) -> str:
        raise NotImplementedError


class PlaywrightSurface(Surface):
    def __init__(self, page: Page):
        self.page = page

    def observe(self) -> ObservedState:
        body = self.page.locator("body")
        if hasattr(body, "aria_snapshot"):
            snapshot = body.aria_snapshot()
        else:
            # Playwright 1.47 (pinned in requirements.txt) predates Locator.aria_snapshot();
            # page.accessibility.snapshot() is the equivalent API on this version.
            snapshot = str(self.page.accessibility.snapshot())
        return ObservedState(accessibility_tree=snapshot, url=self.page.url)

    def act(self, action: ActionType, locator: Locator | None, target: str | None, text: str | None) -> str | None:
        if action == ActionType.NAVIGATE:
            self.page.goto(target)
        elif action == ActionType.CLICK:
            _resolve(self.page, locator).click()
        elif action == ActionType.TYPE_TEXT:
            _resolve(self.page, locator).fill(text)
        elif action == ActionType.SELECT_OPTION:
            _resolve(self.page, locator).select_option(text)
        elif action == ActionType.EXTRACT:
            return _resolve(self.page, locator).text_content()
        else:
            raise ValueError(f"unknown action: {action}")
        return None

    def check_checkpoint(self, checkpoint: Checkpoint) -> bool:
        if checkpoint.type == CheckpointType.ELEMENT_VISIBLE:
            return _resolve(self.page, checkpoint.locator).is_visible()
        if checkpoint.type == CheckpointType.TEXT_PRESENT:
            return checkpoint.text in self.page.content()
        if checkpoint.type == CheckpointType.URL_MATCHES:
            return checkpoint.url_pattern in self.page.url
        raise ValueError(f"unknown checkpoint type: {checkpoint.type}")

    def screenshot(self) -> bytes:
        return self.page.screenshot()

    def current_url(self) -> str:
        return self.page.url or ""
