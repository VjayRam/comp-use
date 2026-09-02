"""Live demonstration of two replay-time robustness features against the REAL mock
bank app (no fakes/mocks) - referenced from README.md's "Exercising every outcome"
section. Requires the mock app running on :5000 (`python run_mock_app.py` in another
terminal) and no LLM/API key (neither feature demonstrated here involves discovery).

Part A - Locator.fallback (comp_use/surface.py): a locator whose primary strategy
doesn't resolve falls back to a second, working locator instead of failing outright.

Part B - OutcomePattern retry (comp_use/replay/engine.py): a RECOVERABLE outcome now
gets a bounded, automatic retry budget (default 3) instead of only ever being
detected-and-reported. Manufactures the mock app's real "stale review token" /
"Session Expired" condition (the same one a real accidental double-submit produces)
by confirming a sub-account once, then re-visiting the same now-consumed review URL.

  B1. The checked-in `open_sub_account` artifact, completely unmodified - proves the
      new default retry budget (max_retries=3) applies automatically even though this
      artifact's `outcome_patterns` entry predates the field and never sets it.
  B2. The same real page, this time with a `recovery_action` pointed at the app's own
      real "Start over" link - proves a configured recovery_action really clicks a
      real element and really clears the matched condition, not just in a unit test.

Run: `python demo_edge_cases.py` (from the repo root, mock app already running).
"""
import time

from playwright.sync_api import sync_playwright

from comp_use.cli import load_artifact
from comp_use.config import load_settings
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.replay.engine import ReplayEngine
from comp_use.schemas import ActionType, Locator, LocatorStrategy, OutcomeType, RiskTier, Step
from comp_use.surface import PlaywrightSurface

BASE = "http://localhost:5000"


def demo_locator_fallback(settings, guardrail) -> None:
    print("=" * 70)
    print("PART A - Locator.fallback against the real mock app")
    print("=" * 70)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        surface = PlaywrightSurface(page)

        surface.act(ActionType.NAVIGATE, locator=None, target=f"{BASE}/member/search", text=None)
        surface.act(
            ActionType.TYPE_TEXT,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "Member ID"}),
            target=None, text="12345",
        )
        bogus_with_real_fallback = Locator(
            strategy=LocatorStrategy.ROLE,
            value={"role": "button", "name": "This Button Does Not Exist"},
            fallback=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Search"}),
        )
        surface.act(ActionType.CLICK, locator=bogus_with_real_fallback, target=None, text=None)
        ok = "/member/12345" in surface.current_url()
        print(f"  primary locator was bogus, fallback resolved to the real Search button: {'PASS' if ok else 'FAIL'}")
        print(f"  final url: {surface.current_url()}")
        browser.close()
    print()


def demo_recoverable_retry(settings, guardrail) -> None:
    print("=" * 70)
    print("PART B - RECOVERABLE retry against a REAL stale review token")
    print("=" * 70)

    artifact = load_artifact("open_sub_account", settings.artifacts_dir)
    print(f"  loaded artifact open_sub_account v{artifact.version} (status={artifact.status})")
    pattern = next(p for p in artifact.outcome_patterns if p.outcome == OutcomeType.RECOVERABLE)
    print(f"  checked-in recoverable pattern: detail={pattern.detail!r} "
          f"max_retries={pattern.max_retries} recovery_action={pattern.recovery_action}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        surface = PlaywrightSurface(page)

        # Drive the real flow up to (and past) confirming once, exactly like the
        # artifact's own recorded steps, to get a real review token that then gets
        # genuinely consumed - a real double-submit, not a simulated one.
        surface.act(ActionType.NAVIGATE, locator=None, target=f"{BASE}/member/search", text=None)
        surface.act(ActionType.TYPE_TEXT, locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "Member ID"}), target=None, text="12345")
        surface.act(ActionType.CLICK, locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Search"}), target=None, text=None)
        surface.act(ActionType.CLICK, locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "link", "name": "Open Sub-Account"}), target=None, text=None)
        surface.act(ActionType.TYPE_TEXT, locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "Deposit Amount"}), target=None, text="500")
        surface.act(ActionType.CLICK, locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Open Account"}), target=None, text=None)

        review_url = surface.current_url()
        print(f"  reached real review page: {review_url}")

        surface.act(ActionType.CLICK, locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Confirm Sub-Account"}), target=None, text=None)
        print(f"  confirmed once (token consumed) -> {surface.current_url()}")

        surface.act(ActionType.NAVIGATE, locator=None, target=review_url, text=None)
        print(f"  re-visited the same review url -> {surface.current_url()}")

        run_id = f"demo_recoverable_default_{int(time.time())}"
        evidence = EvidenceLogger(settings, guardrail, run_id=run_id)
        engine = ReplayEngine(surface, guardrail, evidence)

        matched = engine._match_outcome_pattern(artifact)
        print(f"  _match_outcome_pattern found it live: {matched is not None} "
              f"(outcome={matched.outcome if matched else None}, detail={matched.detail if matched else None})")

        print("  B1: exercising the CHECKED-IN pattern as-is (no recovery_action, default max_retries=3)")
        retries_used: dict = {}
        attempt = 0
        while True:
            p_now = engine._match_outcome_pattern(artifact)
            if p_now is None:
                print("    condition cleared unexpectedly")
                break
            if not engine._should_retry(p_now, index=6, retries_used=retries_used):
                print(f"    gave up after {attempt} retries -> outcome={p_now.outcome.value} detail={p_now.detail!r}")
                break
            attempt += 1
            print(f"    retry #{attempt} attempted (recovery_action=None -> just a re-check, budget={p_now.max_retries})")
        print(f"  evidence log: {evidence.run_dir / 'log.jsonl'}")

        browser.close()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        surface = PlaywrightSurface(page)
        surface.act(ActionType.NAVIGATE, locator=None, target=review_url, text=None)
        print(f"\n  B2: fresh page re-navigated to the same stale review url -> {surface.current_url()}")

        real_recovery_pattern = pattern.model_copy(update={
            "max_retries": 1,
            "recovery_action": Step(
                action=ActionType.CLICK,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "link", "name": "Start over"}),
                risk_tier=RiskTier.SAFE,
            ),
        })
        patched_artifact = artifact.model_copy(update={"outcome_patterns": [real_recovery_pattern]})

        run_id2 = f"demo_recoverable_real_action_{int(time.time())}"
        evidence2 = EvidenceLogger(settings, guardrail, run_id=run_id2)
        engine2 = ReplayEngine(surface, guardrail, evidence2)

        matched2 = engine2._match_outcome_pattern(patched_artifact)
        print(f"  condition matched before recovery: {matched2 is not None}")

        retried = engine2._should_retry(matched2, index=6, retries_used={})
        print(f"  _should_retry performed the REAL recovery_action (clicked the real 'Start over' link): {retried}")
        print(f"  url after recovery_action ran: {surface.current_url()}")

        matched_after = engine2._match_outcome_pattern(patched_artifact)
        print(f"  condition still matches after recovery: {matched_after is not None}  (expect False - it genuinely cleared)")
        print(f"  evidence log: {evidence2.run_dir / 'log.jsonl'}")

        browser.close()
    print()


def main() -> None:
    settings = load_settings()
    guardrail = Guardrail(settings)
    demo_locator_fallback(settings, guardrail)
    demo_recoverable_retry(settings, guardrail)
    print("=" * 70)
    print("DONE - see the printed evidence log paths above for the raw JSONL events")
    print("=" * 70)


if __name__ == "__main__":
    main()
