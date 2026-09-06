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
            checkpoint=Checkpoint(
                type=CheckpointType.TEXT_PRESENT, text="No member records matched your search"
            ),
            detail="No member matched that search term.",
            max_retries=0,
        ),
        # Anchored on the denial page's SENTENCE, never on its "SUPERVISOR OVERRIDE
        # REQUIRED" banner: checkpoint matching is a substring test over page content,
        # and the legitimate Place Account Hold FORM carries that same banner as a
        # "RESTRICTED FUNCTION" warning label. Anchoring on the banner therefore matched
        # every Place Hold replay the moment it reached the form - reporting "not
        # authorized" even signed on as super1, who is authorized, and bailing out
        # before the flow ran at all. The sentence below appears only on the real 403.
        OutcomePattern(
            outcome=OutcomeType.BUSINESS_OUTCOME,
            checkpoint=Checkpoint(
                type=CheckpointType.TEXT_PRESENT, text="is not authorized to perform this function"
            ),
            detail=(
                "The signed-on operator is not authorized for this action and a "
                "supervisor override is required (natural permission denial, or the "
                "injected permission error - same page)."
            ),
            max_retries=0,
        ),
        OutcomePattern(
            outcome=OutcomeType.BUSINESS_OUTCOME,
            checkpoint=Checkpoint(
                type=CheckpointType.TEXT_PRESENT, text="Invalid operator ID or password"
            ),
            detail="Sign-on was rejected: invalid operator ID or password.",
            max_retries=0,
        ),
        # MERIDIAN reports a refused submission with one <font class="err"> banner and a
        # <ul> of the rules that actually failed. The banner names only the category; the
        # list is the half a caller can act on, hence detail_locator on both entries.
        # Two banners, two flows, same shape:
        #   transaction-level (Funds Transfer, Place Hold) -> "could not be validated"
        #   field-level       (Update Member Information)  -> "Please correct the following"
        OutcomePattern(
            outcome=OutcomeType.BUSINESS_OUTCOME,
            checkpoint=Checkpoint(
                type=CheckpointType.TEXT_PRESENT, text="The transaction could not be validated"
            ),
            detail="MERIDIAN rejected the transaction:",
            detail_locator=Locator(strategy=LocatorStrategy.CSS, value={"css": "font.err + ul"}),
            max_retries=0,
        ),
        # The natural per-field validation the brief names ("invalid email/phone on
        # update"). Without this the run walked every step, failed its success
        # checkpoint, and reported a bare hard_failure - never mentioning that the host
        # had said, in as many words, "E-mail address is not in a valid format."
        OutcomePattern(
            outcome=OutcomeType.BUSINESS_OUTCOME,
            checkpoint=Checkpoint(
                type=CheckpointType.TEXT_PRESENT, text="Please correct the following"
            ),
            detail="MERIDIAN rejected the submitted values:",
            detail_locator=Locator(strategy=LocatorStrategy.CSS, value={"css": "font.err + ul"}),
            max_retries=0,
        ),
        # Reached via ?inject=validation, and by a submission the host cannot parse at
        # all. NOT the natural field-validation page - that one is "Please correct the
        # following" above. (An earlier probe conflated the two by posting a wrongly
        # named token field, which the host rejects generically; the browser flow never
        # produces this page for a merely invalid e-mail.)
        OutcomePattern(
            outcome=OutcomeType.BUSINESS_OUTCOME,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="TRANSACTION REJECTED"),
            detail="The host rejected the transaction as entered (injected validation fault, or a submission it could not accept).",
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
        # Genuinely a hard failure - nothing to retry, and no human handoff fixes it -
        # but recognised, so it reports as the host's own application error (with its
        # ERR-... reference visible in the screenshot) instead of as whichever locator
        # happened to time out first on an error page the run never expected to see.
        OutcomePattern(
            outcome=OutcomeType.HARD_FAILURE,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="APPLICATION ERROR"),
            detail="MERIDIAN returned an application error (HTTP 500) and could not process the request.",
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
