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


# Timeout for every attempt in a fallback chain EXCEPT the last one. Playwright's
# actions/state-checks auto-wait up to a default ~30s for an element to appear before
# giving up - correct patience for the one locator you're relying on, but wrong for an
# attempt that HAS a fallback to try next: waiting the full default before even
# starting the fallback would make every drifted-locator replay needlessly slow. The
# terminal attempt (whichever locator has no further .fallback) gets no override and
# uses Playwright's normal default, since after that there's nothing left to try.
_FALLBACK_PROBE_TIMEOUT_MS = 3000


def _resolve_with_fallback(page: Page, locator: Locator, perform, is_success=lambda result: True):
    """Resolve `locator` and call `perform(resolved_locator, timeout_ms)` on it; if
    that raises, or `perform`'s own result doesn't satisfy `is_success` (Playwright's
    `is_visible()` returns False rather than raising when nothing matches, so a
    raised-exception check alone wouldn't catch a checkpoint miss), retry against
    `locator.fallback` - recursively, so a multi-level fallback chain is walked in
    full, not just one level deep. `timeout_ms` is short for every non-terminal
    attempt (see _FALLBACK_PROBE_TIMEOUT_MS) and None (Playwright's own default) for
    the last one in the chain.

    On total failure (primary AND the whole fallback chain), re-raises/returns the
    PRIMARY attempt's own outcome, not the fallback chain's - callers should see why
    the *recorded* locator failed, not a confusing error from a fallback that was
    never the artifact's real intent.

    This is what makes `Locator.fallback` (schemas.py) an actual robustness mechanism
    rather than a schema field nothing reads: a discovery run - or a human reviewer -
    can declare a second locator strategy for a control that's expected to be found
    differently across drift/tenant variance, and replay will transparently try it."""
    timeout = _FALLBACK_PROBE_TIMEOUT_MS if locator.fallback is not None else None
    try:
        result = perform(_resolve(page, locator), timeout)
        if is_success(result):
            return result
        primary_error = None
    except Exception as exc:
        result = None
        primary_error = exc

    if locator.fallback is not None:
        try:
            return _resolve_with_fallback(page, locator.fallback, perform, is_success)
        except Exception:
            pass  # the whole fallback chain failed too - surface the PRIMARY outcome below

    if primary_error is not None:
        raise primary_error
    return result


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

    def get_heading_text(self) -> str | None:
        """Best-effort: the current view's primary heading/title, if this surface
        can identify one. Used only to derive a generic success checkpoint after a
        discovery run (see cli._derive_success_checkpoint) - returning None just
        means the caller falls back to a literal target-identifier match instead.
        Deliberately its own method rather than something callers derive from
        observe()'s accessibility-tree text: "the main heading" is a different
        concept per surface (a DOM heading role vs. a native window title vs. a
        desktop control), so each Surface implementation decides how to answer it,
        the same way check_checkpoint() lets each surface interpret its own
        checkpoint types."""
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
            _resolve_with_fallback(self.page, locator, lambda l, timeout: l.click(timeout=timeout))
        elif action == ActionType.TYPE_TEXT:
            _resolve_with_fallback(self.page, locator, lambda l, timeout: l.fill(text, timeout=timeout))
        elif action == ActionType.SELECT_OPTION:
            _resolve_with_fallback(self.page, locator, lambda l, timeout: l.select_option(text, timeout=timeout))
        elif action == ActionType.EXTRACT:
            return _resolve_with_fallback(self.page, locator, lambda l, timeout: l.text_content(timeout=timeout))
        else:
            raise ValueError(f"unknown action: {action}")
        return None

    def check_checkpoint(self, checkpoint: Checkpoint) -> bool:
        if checkpoint.type == CheckpointType.ELEMENT_VISIBLE:
            return _resolve_with_fallback(
                self.page, checkpoint.locator,
                lambda l, timeout: l.is_visible(timeout=timeout),
                is_success=lambda result: result is True,
            )
        if checkpoint.type == CheckpointType.TEXT_PRESENT:
            return checkpoint.text in self.page.content()
        if checkpoint.type == CheckpointType.URL_MATCHES:
            return checkpoint.url_pattern in self.page.url
        raise ValueError(f"unknown checkpoint type: {checkpoint.type}")

    def screenshot(self) -> bytes:
        return self.page.screenshot()

    def current_url(self) -> str:
        return self.page.url or ""

    def get_heading_text(self) -> str | None:
        try:
            text = self.page.get_by_role("heading").first.text_content(timeout=2000)
        except Exception:
            return None
        return text.strip() if text and text.strip() else None
