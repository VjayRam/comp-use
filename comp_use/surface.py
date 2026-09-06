from __future__ import annotations

import re
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

# Split budget for each of select_option's match attempts (see
# _select_option_robust) when no shorter fallback-probe timeout applies. Kept
# below Playwright's own ~30s default so a genuinely wrong value still fails in
# roughly one normal timeout, not several.
_SELECT_OPTION_ATTEMPT_TIMEOUT_MS = 15000

# Matches a trailing "($13.00)" / "($1,204.55)" style balance shown inline in an
# option's label (e.g. "100234-S0001-13 - Regular Shares ($13.00)"). Some legacy
# forms render a share/account's CURRENT BALANCE as part of the dropdown option
# text itself - a live, mutable value with no stable counterpart anywhere else on
# the option (unlike a form field's value, this can't be swapped for a stable
# name/id attribute; the option's only identifying text already contains it).
# Between the moment discovery/a caller recorded this label and the moment
# replay runs, that balance can have moved (interest posted, another transfer
# happened), leaving neither the recorded label NOR its value attribute matching
# any live option. See _select_option_robust.
_TRAILING_BALANCE_RE = re.compile(r"\s*\(\$[\d,]+\.\d{2}\)\s*$")


def _select_option_robust(l, text: str, timeout: float | None) -> None:
    """Discovery picks a <select> option by its visible label - the only
    representation the model's accessibility-tree view exposes - and records
    that label text as the step's value. Playwright's `select_option(str)`
    matches the option's raw HTML `value` attribute, not its label. The two
    happen to coincide for some fields (e.g. a branch code baked into its
    label) but diverge for others (e.g. a share/account dropdown, where the
    value attribute is an internal id distinct from its human-readable label),
    causing a spurious "did not find some options" timeout on a step that is
    otherwise entirely correct. Try matching by label first (what discovery
    actually saw), then by raw value, then - if the text carries a trailing
    balance suffix - by every live option's label with that same suffix
    stripped from both sides, so a moved balance doesn't sink an otherwise
    exact match on the share/account's stable identifying text."""
    attempt_timeout = timeout if timeout is not None else _SELECT_OPTION_ATTEMPT_TIMEOUT_MS
    try:
        l.select_option(label=text, timeout=attempt_timeout)
        return
    except Exception as exc:
        last_error = exc
    try:
        l.select_option(text, timeout=attempt_timeout)
        return
    except Exception as exc:
        last_error = exc

    stripped_target = _TRAILING_BALANCE_RE.sub("", text).strip()
    if stripped_target != text.strip():
        try:
            option_labels = l.evaluate("el => Array.from(el.options).map(o => o.label)")
        except Exception:
            option_labels = []
        for option_label in option_labels:
            if _TRAILING_BALANCE_RE.sub("", option_label).strip() == stripped_target:
                l.select_option(label=option_label, timeout=attempt_timeout)
                return

    raise last_error


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


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def _text_is_present(page: Page, needle: str) -> bool:
    """Is `needle` visible on the page, as a reader would see it?

    Matches the page's RENDERED text, not its HTML source, and compares with runs
    of whitespace collapsed on both sides. Source matching is a trap that fails
    silently and looks like a missing error state: MERIDIAN's permission page
    carries the sentence

        Operator profile <b>teller1</b> is not authorized to perform this
                     function.

    so `"is not authorized to perform this function" in page.content()` is False -
    the source has a newline and indentation between "this" and "function", and a
    tag mid-sentence. Two live edge cases (an injected 403, and a teller attempting
    a supervisor-only Place Hold) were reported as bare hard_failures for exactly
    this reason, and every future anchor crossing a tag, entity or line wrap would
    have failed the same way.

    Falls back to source matching only if the rendered text can't be read at all,
    so a page mid-navigation degrades to the old behaviour rather than raising."""
    try:
        rendered = page.inner_text("body")
    except Exception:
        try:
            return needle in page.content()
        except Exception:
            return False
    return _collapse_whitespace(needle) in _collapse_whitespace(rendered)


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
        field_hints = self._form_field_hints()
        if field_hints:
            snapshot = (
                f"{snapshot}\n\nForm field attributes (stable handles for building "
                f"locators - prefer these over matching a field's current value, "
                f"which changes):\n{field_hints}"
            )
        return ObservedState(accessibility_tree=snapshot, url=self.page.url)

    def _form_field_hints(self) -> str:
        # aria_snapshot() exposes role/accessible-name/value but never raw HTML
        # attributes - on a legacy table-based form where inputs have no accessible
        # name (label text sits in an adjacent cell, not a real <label for=...>), the
        # model's only remaining handle for an ambiguous field becomes its CURRENT
        # VALUE, which produces a locator that breaks the moment that value changes
        # (see EXT_TASK_FIXES.md #2). This supplements the tree with name/id/type only
        # - never the field's actual value, so this stays safe to run through the same
        # tokenize() pipeline as the rest of the tree, and never leaks real content
        # (an email, a phone number, ...) through this second channel.
        try:
            fields = self.page.eval_on_selector_all(
                "input, select, textarea",
                "els => els.map(el => ({tag: el.tagName.toLowerCase(), type: el.type || '', "
                "name: el.name || '', id: el.id || ''}))",
            )
        except Exception:
            return ""
        lines = [
            f"- {f['tag']}[type={f['type']!r} name={f['name']!r} id={f['id']!r}]"
            for f in fields
            if f.get("name") or f.get("id")
        ]
        return "\n".join(lines)

    def act(self, action: ActionType, locator: Locator | None, target: str | None, text: str | None) -> str | None:
        if action == ActionType.NAVIGATE:
            self.page.goto(target)
        elif action == ActionType.CLICK:
            _resolve_with_fallback(self.page, locator, lambda l, timeout: l.click(timeout=timeout))
        elif action == ActionType.TYPE_TEXT:
            # `text or ""` because an empty value is a real intention - clearing a
            # pre-filled field, or leaving an optional memo blank. Passing None through
            # instead raised "Frame.fill() missing 1 required positional argument:
            # 'value'", which reads like an internal bug rather than anything about the
            # page: live-observed on a funds-transfer recording, where the model asked
            # for an empty memo, got that TypeError, retried with a space, got it
            # again, and burned its whole loop-detector budget on one optional field.
            _resolve_with_fallback(
                self.page, locator, lambda l, timeout: l.fill(text or "", timeout=timeout)
            )
        elif action == ActionType.SELECT_OPTION:
            _resolve_with_fallback(self.page, locator, lambda l, timeout: _select_option_robust(l, text, timeout))
        elif action == ActionType.EXTRACT:
            # inner_text(), not text_content(): text_content() concatenates raw text
            # nodes with nothing between them, so a shares table comes back as
            # "103001-S0001Regular Shares$760.50HOLD103001-MMKT-2Money Market$4.00..."
            # - technically the right characters, unreadable as an answer. inner_text()
            # returns it as the screen lays it out, rows on their own lines and cells
            # tab-separated, which is what a caller asked to "read the balances" wants.
            return _resolve_with_fallback(self.page, locator, lambda l, timeout: l.inner_text(timeout=timeout))
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
            return _text_is_present(self.page, checkpoint.text)
        if checkpoint.type == CheckpointType.URL_MATCHES:
            # url_pattern may contain "*" wildcard segments (see
            # cli._derive_success_checkpoint's numeric-segment substitution) - convert
            # to a regex and search, rather than a plain substring check, so a pattern
            # like "members/*/hold/review" matches any member's URL. A pattern with no
            # "*" behaves identically to the old plain substring check, since
            # re.escape(pattern) with nothing to un-escape into ".*" is just the
            # literal string.
            regex = re.escape(checkpoint.url_pattern).replace(r"\*", ".*")
            return re.search(regex, self.page.url) is not None
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
