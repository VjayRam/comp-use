"""The host-wide error taxonomy for MERIDIAN CORE.

Every anchor here is a substring test against the page's RENDERED text, with
runs of whitespace collapsed (see PlaywrightSurface.check_checkpoint). Two
consequences shape these tests:

- A too-generic anchor is an outage, not a near-miss: it silently reclassifies
  a perfectly healthy page as an error and stops the run before it does
  anything, which is why test_healthy_pages_match_nothing exists.
- An anchor only has to survive the RENDERED text, so a sentence broken across
  tags or line breaks in the HTML source still matches. Matching the source was
  the older behaviour and it failed exactly there.

The page copy in these tests was captured from the live target.
"""
from comp_use.discovery.outcome_library import outcome_patterns_for_target
from comp_use.schemas import OutcomeType

MERIDIAN = {"app": "web-sample.interface-hiring.com", "base_url": "https://web-sample.interface-hiring.com"}

# Captured live. The Place Account Hold FORM - a page the Place Hold capability must
# walk straight through - carries a "supervisor override" warning label of its own,
# and does so even for super1, who IS authorized to submit it.
PLACE_HOLD_FORM = (
    "PLACE ACCOUNT HOLD RESTRICTED FUNCTION - SUPERVISOR OVERRIDE REQUIRED "
    "Share: 102777-S0001 - Regular Shares Reason Code: FRAUD Notes: Continue"
)
# Captured live: the real HTTP 403.
PERMISSION_DENIED = (
    "SUPERVISOR OVERRIDE REQUIRED Operator profile teller1 is not authorized to "
    "perform this function. A supervisor must sign on to complete this request."
)


def _match(page_text: str):
    """The pattern ReplayEngine would match against this page. `page_text` is the
    page as a reader sees it, which is what check_checkpoint now compares against;
    whitespace is collapsed on both sides, exactly as _text_is_present does."""
    needle_of = lambda s: " ".join(s.split())
    haystack = needle_of(page_text)
    for pattern in outcome_patterns_for_target(MERIDIAN):
        if needle_of(pattern.checkpoint.text) in haystack:
            return pattern
    return None


def test_place_hold_form_is_not_mistaken_for_a_permission_denial():
    # The bug this guards: the permission pattern was anchored on the banner
    # "SUPERVISOR OVERRIDE REQUIRED", which the legitimate form below also carries.
    # Every Place Account Hold replay therefore reported "operator is not authorized"
    # the moment it reached the form - signed on as a supervisor who was authorized -
    # and gave up before performing a single step of the flow.
    assert _match(PLACE_HOLD_FORM) is None


def test_real_permission_denial_is_still_classified_as_a_business_outcome():
    pattern = _match(PERMISSION_DENIED)
    assert pattern is not None
    assert pattern.outcome == OutcomeType.BUSINESS_OUTCOME
    assert "not authorized" in pattern.detail


def test_every_state_the_brief_names_is_classified():
    # §2.2's injected taxonomy and the natural errors alongside it, each keyed by the
    # page copy the live target actually renders for it.
    cases = {
        "TRANSACTION REJECTED The transaction could not be completed as entered.": OutcomeType.BUSINESS_OUTCOME,
        "FUNDS TRANSFER The transaction could not be validated: Insufficient available balance": OutcomeType.BUSINESS_OUTCOME,
        "RECORD NOT FOUND The requested member record could not be located": OutcomeType.BUSINESS_OUTCOME,
        "MEMBER INQUIRY No member records matched your search. Try member numbers": OutcomeType.BUSINESS_OUTCOME,
        "OPERATOR SIGN ON Invalid operator ID or password. Operator ID:": OutcomeType.BUSINESS_OUTCOME,
        PERMISSION_DENIED: OutcomeType.BUSINESS_OUTCOME,
        "SCHEDULED MAINTENANCE IN PROGRESS The host is temporarily unavailable": OutcomeType.RECOVERABLE,
        "YOUR SESSION HAS TIMED OUT For security, your session ended due to inactivity.": OutcomeType.RECOVERABLE,
        "APPLICATION ERROR An unexpected error occurred. Reference: ERR-3A0AEC77": OutcomeType.HARD_FAILURE,
    }
    for page_text, expected in cases.items():
        pattern = _match(page_text)
        assert pattern is not None, f"no pattern matched: {page_text[:60]}"
        assert pattern.outcome == expected, f"{page_text[:60]} -> {pattern.outcome}"


def test_healthy_pages_match_nothing():
    # The counterpart to the test above, and the one that catches an anchor drawn too
    # wide: every page a capability legitimately passes through must classify as
    # nothing at all.
    healthy = [
        PLACE_HOLD_FORM,
        "OPERATOR SIGN ON Operator ID: Password: Branch: MAIN-001 - Main Office "
        "Demo operators: teller1 / password super1 / password (supervisor)",
        "MAIN MENU Signed on as J. TELLER (TELLER) 1. Member Inquiry / Selection "
        "2. Funds Transfer 3. Open New Share 4. Update Member Information 5. Place Account Hold",
        "MEMBER RECORD Member No.: 100234 Name: Lovelace, Ada SHARES / BALANCES "
        "Share ID Type Balance Status 100234-S0001 Regular Shares $2,499.00 HOLD",
        "CONFIRM FUNDS TRANSFER Member: 100234 - Lovelace, Ada From: 100234-S0001-19 "
        "Amount: $1.00 This will post immediately and cannot be reversed from this screen. "
        "Post Transfer Cancel",
        "TRANSFER POSTED Confirmation: CN480322 Return to Funds Transfer",
        "MEMBER INQUIRY / SELECTION Search by: Member Number Last Name Value: Search",
    ]
    for page_text in healthy:
        assert _match(page_text) is None, f"healthy page misclassified: {page_text[:60]}"


def test_unknown_host_gets_no_patterns():
    assert outcome_patterns_for_target({"app": "some-other-app.example"}) == []


def test_field_level_validation_is_classified_and_names_the_failing_rule():
    # A3: the natural "invalid email/phone on update" case the brief names. This page is
    # NOT the TRANSACTION REJECTED screen - it is the edit form re-rendered with a list
    # of the rules that failed - so before this pattern existed the run walked every
    # step, failed its success checkpoint, and reported a bare hard_failure that never
    # mentioned the actual problem.
    page = (
        "UPDATE MEMBER INFORMATION Please correct the following: "
        "E-mail address is not in a valid format. Return to Update"
    )
    pattern = _match(page)
    assert pattern is not None
    assert pattern.outcome == OutcomeType.BUSINESS_OUTCOME
    # The reason itself is read live off the page, not restated here.
    assert pattern.detail_locator is not None


def test_the_two_rejection_banners_are_distinct_patterns():
    # Transaction-level and field-level rejections look alike and are easy to conflate
    # (an earlier probe did exactly that, by posting a wrongly named token field). They
    # are different screens reached by different flows; both must classify, and both
    # must read their reason off the page.
    transaction = _match("FUNDS TRANSFER The transaction could not be validated: Insufficient available balance")
    field = _match("UPDATE MEMBER INFORMATION Please correct the following: Phone number is not valid")
    assert transaction is not None and field is not None
    assert transaction.checkpoint.text != field.checkpoint.text
    assert transaction.detail_locator is not None and field.detail_locator is not None
