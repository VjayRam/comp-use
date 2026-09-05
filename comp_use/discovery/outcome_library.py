from comp_use.schemas import (
    ActionType,
    Checkpoint,
    CheckpointType,
    Locator,
    LocatorStrategy,
    OutcomePattern,
    OutcomeType,
    RiskTier,
    Step,
)

# Confirmed live against MERIDIAN CORE - see EXT_TASK_FIXES.md section 9 for the
# stress test that captured this exact page copy, both as a natural error and via
# the injected-error taxonomy (?inject=notfound|permission|maintenance|timeout).
_MERIDIAN_HOST = "web-sample.interface-hiring.com"


def _meridian_patterns() -> list[OutcomePattern]:
    return [
        OutcomePattern(
            outcome=OutcomeType.BUSINESS_OUTCOME,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="RECORD NOT FOUND"),
            detail=(
                "The requested member record could not be located on this host "
                "(natural 'no such member', or the injected notfound error - same page)."
            ),
            max_retries=0,
        ),
        OutcomePattern(
            outcome=OutcomeType.BUSINESS_OUTCOME,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="SUPERVISOR OVERRIDE REQUIRED"),
            detail=(
                "The signed-on operator is not authorized for this action and a "
                "supervisor override is required (natural permission denial, or the "
                "injected permission error - same page)."
            ),
            max_retries=0,
        ),
        OutcomePattern(
            outcome=OutcomeType.RECOVERABLE,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="SCHEDULED MAINTENANCE IN PROGRESS"),
            detail="A scheduled-maintenance interstitial (injected or natural) is dismissible via its own 'Continue' link.",
            max_retries=3,
            recovery_action=Step(
                action=ActionType.CLICK,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "link", "name": "Continue"}),
                risk_tier=RiskTier.SAFE,
            ),
        ),
        OutcomePattern(
            outcome=OutcomeType.RECOVERABLE,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="YOUR SESSION HAS TIMED OUT"),
            detail=(
                "Session expired mid-flow (natural idle timeout, or the injected timeout "
                "error). Recovering means re-authenticating and resuming the original "
                "capability's remaining steps, which OutcomePattern.recovery_action (a "
                "single UI step) cannot express - reported as recoverable with no "
                "automatic recovery attempted, rather than misclassified as a hard "
                "failure. See EXT_TASK_FIXES.md #9.5 for the full architectural gap."
            ),
            max_retries=0,
        ),
    ]


# Keyed on target["app"] (the artifact's recorded hostname - see compile_artifact's
# `target` dict) rather than capability name: MERIDIAN CORE renders the exact same
# interstitial/denial pages regardless of which business flow reached them, so one
# library entry per host condition covers every capability recorded against it.
_LIBRARY_BY_HOST = {
    _MERIDIAN_HOST: _meridian_patterns,
}


def outcome_patterns_for_target(target: dict) -> list[OutcomePattern]:
    """A hand-authored OutcomePattern requires someone to have already watched the
    exact business/recoverable state happen live - backwards for a demo, since the
    injected-error taxonomy and the natural business outcomes it mirrors (member not
    found, permission denial, maintenance interstitial, session timeout) are the
    SAME handful of pages on a given target no matter which capability reached them.
    Rather than requiring each capability to be hand-fixed one at a time only after
    someone happens to trigger the state live, attach the whole confirmed library for
    this target's host to every artifact compiled against it - see compile_artifact().
    Returns an empty list for a target this library doesn't recognize (e.g. the mock
    Flask bank), same as before this existed."""
    factory = _LIBRARY_BY_HOST.get(target.get("app", ""))
    return factory() if factory else []
