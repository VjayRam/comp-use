import json
from pathlib import Path

import pytest

from comp_use.config import load_settings
from comp_use.escalation.controller import EscalationController
from comp_use.escalation.transport import ControlTransport
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.replay.engine import ReplayEngine, validate_required_params
from comp_use.schemas import (
    ActionType, Artifact, Checkpoint, CheckpointType, InputParam, Locator,
    LocatorStrategy, OutcomePattern, OutcomeType, RiskTier, Step, ValueSource,
)


class FakeTransport(ControlTransport):
    def __init__(self):
        self.notified = []
        self.resumed = False

    def notify(self, request):
        self.notified.append(request)

    def wait_for_resume(self):
        self.resumed = True
        return "handled it"


class FakeSurface:
    def __init__(
        self,
        checkpoint_result=True,
        fail_on_step=None,
        matching_checkpoint_text=None,
        outcome_visible_after_step=None,
        raise_on_step=None,
        raise_on_screenshot=False,
    ):
        self.acted = []
        self.checkpoint_result = checkpoint_result
        self.fail_on_step = fail_on_step
        self.matching_checkpoint_text = matching_checkpoint_text
        self.outcome_visible_after_step = outcome_visible_after_step
        self.raise_on_step = raise_on_step
        self.raise_on_screenshot = raise_on_screenshot
        self.url = "http://localhost:5000/member/search"

    def act(self, action, locator, target, text):
        if self.raise_on_step is not None and len(self.acted) == self.raise_on_step:
            raise TimeoutError(f"element not found for step {len(self.acted)}")
        self.acted.append((action, target, text))
        if action == ActionType.NAVIGATE:
            self.url = target
        if action == ActionType.EXTRACT:
            return "CONF-000123"
        return None

    def observe(self):
        from comp_use.surface import ObservedState
        return ObservedState(accessibility_tree="tree", url=self.url)

    def check_checkpoint(self, checkpoint):
        if checkpoint.type == CheckpointType.TEXT_PRESENT and self.matching_checkpoint_text is not None:
            if self.outcome_visible_after_step is not None and len(self.acted) <= self.outcome_visible_after_step:
                return False
            return checkpoint.text == self.matching_checkpoint_text
        if self.fail_on_step is not None and len(self.acted) - 1 == self.fail_on_step:
            return False
        return self.checkpoint_result

    def screenshot(self):
        if self.raise_on_screenshot:
            raise TimeoutError("Page.screenshot: Timeout 30000ms exceeded.")
        return b"png"

    def current_url(self):
        return self.url


def _make_artifact():
    return Artifact(
        capability_name="lookup_member",
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        input_schema=[InputParam(name="member_id", type="string", required=True)],
        steps=[
            Step(action=ActionType.NAVIGATE, target="http://localhost:5000/member/search", risk_tier=RiskTier.SAFE),
            Step(
                action=ActionType.TYPE_TEXT,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "Member ID"}),
                value_source=ValueSource(type="goal_parameter", param_name="member_id", param_type="string"),
                risk_tier=RiskTier.SAFE,
            ),
        ],
        success_checkpoint=Checkpoint(
            type=CheckpointType.ELEMENT_VISIBLE,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Member Detail"}),
        ),
        created_from_run_id="run_x",
    )


def test_validate_required_params_is_shared_between_cli_and_engine():
    # cli.py's _run_replay and ReplayEngine.run() both call this exact function -
    # this test exists so a future change to validation logic can't accidentally
    # land in only one of the two call sites.
    artifact = _make_artifact()
    assert validate_required_params(artifact, {}) == "missing required param 'member_id'"
    assert validate_required_params(artifact, {"member_id": "12345"}) is None


def _make_engine(surface, tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="replay_run")
    return ReplayEngine(surface, guardrail, evidence)


def test_missing_required_param_is_validation_error(tmp_path):
    engine = _make_engine(FakeSurface(), tmp_path)
    result = engine.run(_make_artifact(), params={})
    assert result.outcome == OutcomeType.VALIDATION_ERROR
    assert not FakeSurface().acted  # no browser interaction on validation failure


def test_successful_replay_returns_success(tmp_path):
    engine = _make_engine(FakeSurface(checkpoint_result=True), tmp_path)
    result = engine.run(_make_artifact(), params={"member_id": "12345"})
    assert result.outcome == OutcomeType.SUCCESS


class _TableExtractSurface(FakeSurface):
    """Returns a MERIDIAN-shaped shares/balances table blob from any EXTRACT action,
    for testing derived-output summation - the source page this models has no total
    row of its own, only individual line items (see EXT_TASK_FIXES.md)."""

    def act(self, action, locator, target, text):
        if action == ActionType.EXTRACT:
            self.acted.append((action, target, text))
            return (
                "100234-S0001 Regular Shares $2,499.00 HOLD\n"
                "100234-S0070 Share Draft (Checking) $1,240.55 HOLD\n"
                "100234-S0001-6 Regular Shares $40.00 OPEN"
            )
        return super().act(action, locator, target, text)


def test_derived_output_sums_currency_from_another_extracted_output(tmp_path):
    from comp_use.schemas import DerivedOutputSpec, OutputParam

    artifact = _make_artifact()
    artifact.steps.append(
        Step(
            action=ActionType.EXTRACT,
            locator=Locator(strategy=LocatorStrategy.CSS, value={"css": "table"}),
            extract_as="shares_balances_table",
            risk_tier=RiskTier.SAFE,
        )
    )
    artifact.output_schema = [
        OutputParam(
            name="total_balance", type="string",
            derive=DerivedOutputSpec(from_output="shares_balances_table"),
        )
    ]
    engine = _make_engine(_TableExtractSurface(checkpoint_result=True), tmp_path)
    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.SUCCESS
    assert result.outputs["shares_balances_table"].startswith("100234-S0001 ")
    # 2499.00 + 1240.55 + 40.00, including the two HOLD-status shares - the total
    # deliberately includes them since the money is still on the member's record,
    # just restricted (see the design discussion in EXT_TASK_FIXES.md).
    assert result.outputs["total_balance"] == "$3,779.55"


def test_derived_output_absent_when_source_output_missing(tmp_path):
    from comp_use.schemas import DerivedOutputSpec, OutputParam

    artifact = _make_artifact()
    artifact.output_schema = [
        OutputParam(
            name="total_balance", type="string",
            derive=DerivedOutputSpec(from_output="nonexistent_output"),
        )
    ]
    engine = _make_engine(FakeSurface(checkpoint_result=True), tmp_path)
    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.SUCCESS
    assert "total_balance" not in result.outputs


def test_checkpoint_failure_returns_hard_failure_with_step_detail(tmp_path):
    surface = FakeSurface(fail_on_step=1)
    engine = _make_engine(surface, tmp_path)
    result = engine.run(_make_artifact(), params={"member_id": "12345"})
    assert result.outcome == OutcomeType.HARD_FAILURE
    assert result.step_index == 1


def test_literal_step_replays_recorded_value_regardless_of_params(tmp_path):
    artifact = _make_artifact()
    artifact.steps.append(
        Step(
            action=ActionType.TYPE_TEXT,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "Note"}),
            value_source=None,
            value="Opened at teller request",
            risk_tier=RiskTier.SAFE,
        )
    )
    surface = FakeSurface()
    engine = _make_engine(surface, tmp_path)
    engine.run(artifact, params={"member_id": "12345"})

    literal_step_call = surface.acted[-1]
    assert literal_step_call[2] == "Opened at teller request"


def test_business_outcome_returned_when_outcome_pattern_matches_instead_of_hard_failure(tmp_path):
    artifact = _make_artifact()
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.BUSINESS_OUTCOME,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="Insufficient funds for this transfer."),
            detail="insufficient funds",
        )
    ]
    surface = FakeSurface(checkpoint_result=False, matching_checkpoint_text="Insufficient funds for this transfer.")
    engine = _make_engine(surface, tmp_path)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.BUSINESS_OUTCOME
    assert result.detail == "insufficient funds"


def test_recoverable_returned_when_a_dismiss_and_retry_pattern_matches(tmp_path):
    artifact = _make_artifact()
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.RECOVERABLE,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="This review session has expired"),
            detail="session_expired",
        )
    ]
    # This pattern never declares max_retries/recovery_action - it still gets the
    # default 3 retry attempts (a bare recheck, since there's no recovery_action to
    # perform) before giving up; the condition here never clears either way, so the
    # final reported outcome is unchanged from before retry support existed.
    surface = FakeSurface(checkpoint_result=False, matching_checkpoint_text="This review session has expired")
    engine = _make_engine(surface, tmp_path)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.RECOVERABLE
    assert result.detail == "session_expired"


class _RecoverableUntilDismissedSurface(FakeSurface):
    """A recoverable interstitial (TEXT_PRESENT "Interstitial") is visible until a
    CLICK on a button named "Dismiss" is performed, then it clears and the artifact's
    normal steps proceed. Used to prove ReplayEngine's retry loop actually performs
    the declared recovery_action and re-checks, rather than only labeling the state."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.dismissed = False

    def act(self, action, locator, target, text):
        result = super().act(action, locator, target, text)
        if action == ActionType.CLICK and locator is not None and locator.value.get("name") == "Dismiss":
            self.dismissed = True
        return result

    def check_checkpoint(self, checkpoint):
        if checkpoint.type == CheckpointType.TEXT_PRESENT and checkpoint.text == "Interstitial":
            return not self.dismissed
        return super().check_checkpoint(checkpoint)


def _dismiss_recovery_action(button_name: str = "Dismiss") -> Step:
    return Step(
        action=ActionType.CLICK,
        locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": button_name}),
        risk_tier=RiskTier.SAFE,
    )


def test_recoverable_pattern_retries_and_succeeds_once_recovery_action_clears_it(tmp_path):
    artifact = _make_artifact()
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.RECOVERABLE,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="Interstitial"),
            detail="dismissable_interstitial",
            max_retries=2,
            recovery_action=_dismiss_recovery_action(),
        )
    ]
    surface = _RecoverableUntilDismissedSurface(checkpoint_result=True)
    engine = _make_engine(surface, tmp_path)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.SUCCESS
    assert surface.dismissed is True
    # the recovery action really ran as a genuine surface.act() call, before the
    # artifact's own recorded steps (navigate, type_text) proceeded
    assert surface.acted[0] == (ActionType.CLICK, None, None)
    assert len(surface.acted) == 3


def test_recoverable_pattern_gives_up_after_max_retries_and_reports_recoverable(tmp_path):
    artifact = _make_artifact()
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.RECOVERABLE,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="Interstitial"),
            detail="dismissable_interstitial",
            max_retries=2,
            # clicks the wrong button - the interstitial never actually clears
            recovery_action=_dismiss_recovery_action(button_name="Not The Real Dismiss Button"),
        )
    ]
    surface = _RecoverableUntilDismissedSurface(checkpoint_result=True)
    engine = _make_engine(surface, tmp_path)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.RECOVERABLE
    assert result.detail == "dismissable_interstitial"
    assert surface.dismissed is False
    # exactly max_retries recovery attempts were made, then it gave up - no infinite
    # loop, and the artifact's own steps were never attempted
    assert len(surface.acted) == 2


def test_recoverable_retry_is_logged_as_evidence(tmp_path):
    artifact = _make_artifact()
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.RECOVERABLE,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="Interstitial"),
            detail="dismissable_interstitial",
            max_retries=1,
            recovery_action=_dismiss_recovery_action(),
        )
    ]
    surface = _RecoverableUntilDismissedSurface(checkpoint_result=True)
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="replay_retry_evidence")
    engine = ReplayEngine(surface, guardrail, evidence)

    engine.run(artifact, params={"member_id": "12345"})

    events = [json.loads(line) for line in (tmp_path / "evidence" / "replay_retry_evidence" / "log.jsonl").read_text().splitlines()]
    retry_events = [e for e in events if e["event_type"] == "recoverable_retry"]
    assert len(retry_events) == 1
    assert retry_events[0]["data"]["attempt"] == 1
    assert retry_events[0]["data"]["max_retries"] == 1
    assert retry_events[0]["data"]["detail"] == "dismissable_interstitial"


def test_max_retries_zero_opts_out_of_retrying_and_reports_immediately(tmp_path):
    artifact = _make_artifact()
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.RECOVERABLE,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="Interstitial"),
            detail="dismissable_interstitial",
            max_retries=0,
            recovery_action=_dismiss_recovery_action(),
        )
    ]
    surface = _RecoverableUntilDismissedSurface(checkpoint_result=True)
    engine = _make_engine(surface, tmp_path)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.RECOVERABLE
    assert surface.acted == []  # no recovery action attempted, no retry loop entered


def test_recoverable_pattern_retries_3_times_by_default_before_giving_up(tmp_path):
    # OutcomePattern.max_retries defaults to 3 - even a pattern that declares no
    # max_retries/recovery_action at all still gets a bounded, automatic retry budget.
    artifact = _make_artifact()
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.RECOVERABLE,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="Interstitial"),
            detail="transient",
        )
    ]
    surface = _RecoverableUntilDismissedSurface(checkpoint_result=True)  # never clears - no recovery_action declared
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="replay_default_retry")
    engine = ReplayEngine(surface, guardrail, evidence)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.RECOVERABLE
    events = [json.loads(line) for line in (tmp_path / "evidence" / "replay_default_retry" / "log.jsonl").read_text().splitlines()]
    retry_events = [e for e in events if e["event_type"] == "recoverable_retry"]
    assert len(retry_events) == 3
    assert [e["data"]["attempt"] for e in retry_events] == [1, 2, 3]


def test_recovery_action_performed_is_logged_as_its_own_evidence_event(tmp_path):
    artifact = _make_artifact()
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.RECOVERABLE,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="Interstitial"),
            detail="dismissable_interstitial",
            max_retries=1,
            recovery_action=_dismiss_recovery_action(),
        )
    ]
    surface = _RecoverableUntilDismissedSurface(checkpoint_result=True)
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="replay_recovery_action_logged")
    engine = ReplayEngine(surface, guardrail, evidence)

    engine.run(artifact, params={"member_id": "12345"})

    events = [json.loads(line) for line in (tmp_path / "evidence" / "replay_recovery_action_logged" / "log.jsonl").read_text().splitlines()]
    performed = [e for e in events if e["event_type"] == "recovery_action_performed"]
    assert len(performed) == 1
    assert performed[0]["data"] == {"step_index": 0, "action": "click"}


def test_recovery_action_that_violates_allowlist_hard_fails_instead_of_retrying_or_softening(tmp_path):
    # A recovery_action is a real action against the live surface - it must get the same
    # allowlist enforcement every other step gets. A policy breach here is NOT a transient
    # condition: it must hard-stop the run, never be silently retried and never softened
    # into a plain `recoverable` report.
    artifact = _make_artifact()
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.RECOVERABLE,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="Interstitial"),
            detail="dismissable_interstitial",
            max_retries=2,
            recovery_action=Step(
                action=ActionType.NAVIGATE, target="http://evil.example.com/x", risk_tier=RiskTier.SAFE
            ),
        )
    ]
    surface = _RecoverableUntilDismissedSurface(checkpoint_result=True)
    engine = _make_engine(surface, tmp_path)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.HARD_FAILURE
    assert "not in allowlist" in (result.detail or "")
    assert result.expected == "recovery_action to pass the URL/action allowlist"
    assert surface.acted == []  # the disallowed recovery navigation must never actually run


class TakeoverThenClearTransport(FakeTransport):
    """Reports a pending takeover exactly once - like the real QueueTransport,
    where clear_takeover() (called inside EscalationController.escalate()) turns
    it back off so it can't fire a second, redundant escalation."""

    def __init__(self):
        super().__init__()
        self._pending = True

    def takeover_requested(self):
        return self._pending

    def clear_takeover(self):
        self._pending = False


def test_takeover_request_pauses_before_the_next_step_with_a_manual_reason(tmp_path):
    surface = FakeSurface(checkpoint_result=True)
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="replay_takeover")
    transport = TakeoverThenClearTransport()
    escalation = EscalationController(evidence, transport)
    engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)

    result = engine.run(_make_artifact(), params={"member_id": "12345"})

    assert len(transport.notified) == 1
    assert transport.notified[0].reason == "manual takeover requested by operator"
    assert transport.notified[0].current_step == 0  # paused before the FIRST step ran
    assert transport.resumed is True
    assert result.outcome == OutcomeType.SUCCESS  # replay continued normally after handback


def test_risky_recovery_action_escalates_before_running(tmp_path):
    # A recovery_action tagged RISKY must pause for human confirmation before it runs,
    # exactly like a normal risky step does - never silently executed.
    artifact = _make_artifact()
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.RECOVERABLE,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="Interstitial"),
            detail="dismissable_interstitial",
            max_retries=1,
            recovery_action=Step(
                action=ActionType.CLICK,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Dismiss"}),
                risk_tier=RiskTier.RISKY,
            ),
        )
    ]
    surface = _RecoverableUntilDismissedSurface(checkpoint_result=True)
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="replay_risky_recovery_escalate")
    transport = FakeTransport()
    escalation = EscalationController(evidence, transport)
    engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)

    engine.run(artifact, params={"member_id": "12345"}, confirm_risky=False)

    assert len(transport.notified) == 1
    assert "recovery_action" in transport.notified[0].reason
    assert surface.dismissed is True  # confirmed via FakeTransport, then the click still ran


def test_risky_recovery_action_does_not_escalate_when_confirm_risky_is_true(tmp_path):
    artifact = _make_artifact()
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.RECOVERABLE,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="Interstitial"),
            detail="dismissable_interstitial",
            max_retries=1,
            recovery_action=Step(
                action=ActionType.CLICK,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Dismiss"}),
                risk_tier=RiskTier.RISKY,
            ),
        )
    ]
    surface = _RecoverableUntilDismissedSurface(checkpoint_result=True)
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="replay_risky_recovery_no_escalate")
    transport = FakeTransport()
    escalation = EscalationController(evidence, transport)
    engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)

    result = engine.run(artifact, params={"member_id": "12345"}, confirm_risky=True)

    assert transport.notified == []
    assert result.outcome == OutcomeType.SUCCESS


class _RaisingTransport(ControlTransport):
    """Reproduces a real, live-observed failure: LocalSharedBrowserTransport raises
    RuntimeError when stdin isn't interactive."""

    def notify(self, request):
        raise RuntimeError("stdin is closed/non-interactive")

    def wait_for_resume(self):
        raise AssertionError("should never be reached - notify() already raised")


def test_risky_step_escalation_transport_failure_returns_hard_failure_not_a_crash(tmp_path):
    artifact = _make_artifact()
    artifact.steps[1].risk_tier = RiskTier.RISKY
    surface = FakeSurface()
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="replay_risky_escalation_transport_fail")
    escalation = EscalationController(evidence, _RaisingTransport())
    engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)

    # Must return a structured HARD_FAILURE, never let the transport's RuntimeError
    # escape run() as a raw traceback.
    result = engine.run(artifact, params={"member_id": "12345"}, confirm_risky=False)

    assert result.outcome == OutcomeType.HARD_FAILURE
    assert "RuntimeError" in result.detail
    assert result.expected == "escalation to complete (human confirmation for a risky step)"
    assert surface.acted == [(ActionType.NAVIGATE, "http://localhost:5000/member/search", None)]  # the risky step never ran


class _RisksJustOneStepSurface(FakeSurface):
    """Raises exactly once, on the first TYPE_TEXT attempt (simulating a human
    having already handled that one risky step during a takeover), then
    behaves normally for everything else - unlike raise_on_step (keyed on
    len(acted), which stays frozen at the same count after a raise that never
    appends, so it would otherwise also raise on the NEXT action attempted).
    The artifact's own success checkpoint only reads True once a trailing safe
    step has also actually run - modeling a human who completed ONLY the risky
    step they were handed control for, not the whole remaining flow, so
    automation still has to carry out what comes after it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._raised_once = False

    def act(self, action, locator, target, text):
        if action == ActionType.TYPE_TEXT and not self._raised_once:
            self._raised_once = True
            raise TimeoutError("locator not found for type_text")
        super().act(action, locator, target, text)

    def check_checkpoint(self, checkpoint):
        return len(self.acted) >= 2


def test_replay_treats_risky_step_as_succeeded_when_the_action_fails_after_escalation(tmp_path):
    # Mirrors a fix already made in DiscoveryAgent (ENHANCEMENTS.md #16/#17) - ported
    # here because replay has the exact same gap: a human takes control during the
    # escalation pause and performs the risky action themselves (e.g. the real "Post
    # Transfer" click) - by the time replay's own act() call runs, the control it
    # targeted is already gone from the page, so it always raises. Before this fix,
    # that raise was reported as HARD_FAILURE for a step that, in fact, succeeded.
    artifact = _make_artifact()
    artifact.steps[1].risk_tier = RiskTier.RISKY
    # A trailing safe step the human did NOT also do - only the one risky step was
    # handled during the takeover, so the artifact's success state genuinely isn't
    # reached until automation carries out this last step itself. Without it, "the
    # human did exactly the risky step and nothing more" is indistinguishable from
    # "the human finished everything," and the broader _finish_if_already_succeeded
    # check (added alongside this one) would correctly short-circuit before this
    # specific action-failure-handling mechanism ever got exercised.
    artifact.steps.append(
        Step(action=ActionType.CLICK, locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Done"}))
    )
    # act call #0 = NAVIGATE (succeeds, acted len 1); the risky TYPE_TEXT at index 1
    # raises once (simulating the human having already handled it during the
    # escalation pause) - success only once the trailing CLICK also runs (len 2).
    surface = _RisksJustOneStepSurface()
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="replay_risky_manual_escalation")
    transport = FakeTransport()
    escalation = EscalationController(evidence, transport)
    engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)

    result = engine.run(artifact, params={"member_id": "12345"}, confirm_risky=False)

    assert len(transport.notified) == 1
    assert result.outcome == OutcomeType.SUCCESS

    log_path = evidence.run_dir / "log.jsonl"
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert not [e for e in events if e["event_type"] == "validation_error"]
    recorded = [e for e in events if e["event_type"] == "step_completed_during_escalation"]
    assert len(recorded) == 1


class _ResolvableTransport(FakeTransport):
    """wait_for_resume() clears the condition the surface below is presenting - the
    same shape as a real human, mid-takeover, doing something live that resolves an
    unexpected page state (or simply confirming they've looked and it's fine to
    retry) before hitting Resume."""

    def __init__(self, surface):
        super().__init__()
        self.surface = surface

    def wait_for_resume(self):
        self.surface.resolved = True
        return super().wait_for_resume()


class _BusinessOutcomeUntilResolvedSurface(FakeSurface):
    """The artifact's own success checkpoint (the generic branch below) only
    reads True once all 3 of the artifact's steps have actually run through
    act() - it does NOT defer to a fixed checkpoint_result. That matters here
    specifically: this surface also gates a SEPARATE business-outcome
    checkpoint (the supervisor-override page) that the first escalation
    resolves. If the generic branch instead returned a constructor-supplied
    checkpoint_result=True unconditionally, `_finish_if_already_succeeded`
    would see the run as already done right after that FIRST escalation and
    return SUCCESS before ever reaching the second, risky-step escalation
    this test exists to exercise - collapsing two distinct escalations into
    one and hiding a real gap."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.resolved = False

    def check_checkpoint(self, checkpoint):
        if checkpoint.type == CheckpointType.TEXT_PRESENT and checkpoint.text == "SUPERVISOR OVERRIDE REQUIRED":
            return not self.resolved
        return len(self.acted) >= 3


def test_replay_escalates_before_accepting_a_business_outcome_ahead_of_a_known_risky_step(tmp_path):
    # The core of the fix: an outcome pattern matching BEFORE the run ever reaches a
    # step this artifact records as risky (i.e. a human already confirmed, during
    # discovery, that this exact action needs judgment) must not be accepted as an
    # unappealable dead end - a human gets a chance to look at the live page first.
    # If their intervention clears the condition (simulated here via the transport's
    # wait_for_resume, matching a real operator fixing something live), replay
    # continues normally instead of giving up.
    artifact = _make_artifact()
    artifact.steps.append(
        Step(
            action=ActionType.CLICK,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Post"}),
            risk_tier=RiskTier.RISKY,
        )
    )
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.BUSINESS_OUTCOME,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="SUPERVISOR OVERRIDE REQUIRED"),
            detail="supervisor override required",
        )
    ]
    surface = _BusinessOutcomeUntilResolvedSurface()
    transport = _ResolvableTransport(surface)
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="replay_business_outcome_before_risky")
    escalation = EscalationController(evidence, transport)
    engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)

    result = engine.run(artifact, params={"member_id": "12345"}, confirm_risky=False)

    # One escalation for the unexpected business-outcome page, one more for the
    # normal risky-step confirmation once the run actually reaches it.
    assert len(transport.notified) == 2
    assert "unexpected outcome" in transport.notified[0].reason
    assert "risk_tier=risky" in transport.notified[1].reason
    assert result.outcome == OutcomeType.SUCCESS


def test_replay_still_reports_business_outcome_if_it_persists_after_escalation(tmp_path):
    # If the human looks and confirms it's a genuine dead end (the condition is
    # still there after resume), the original business_outcome is reported exactly
    # as it would have been without this fix - a human's judgment that this really
    # is a dead end is still respected, not overridden.
    artifact = _make_artifact()
    artifact.steps.append(
        Step(
            action=ActionType.CLICK,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Post"}),
            risk_tier=RiskTier.RISKY,
        )
    )
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.BUSINESS_OUTCOME,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="SUPERVISOR OVERRIDE REQUIRED"),
            detail="supervisor override required",
        )
    ]
    surface = FakeSurface(checkpoint_result=False, matching_checkpoint_text="SUPERVISOR OVERRIDE REQUIRED")
    transport = FakeTransport()
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="replay_business_outcome_persists")
    escalation = EscalationController(evidence, transport)
    engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)

    result = engine.run(artifact, params={"member_id": "12345"}, confirm_risky=False)

    assert len(transport.notified) == 1
    assert result.outcome == OutcomeType.BUSINESS_OUTCOME
    assert result.detail == "supervisor override required"


def test_recovery_action_escalation_transport_failure_returns_hard_failure_not_a_crash(tmp_path):
    artifact = _make_artifact()
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.RECOVERABLE,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="Interstitial"),
            detail="dismissable_interstitial",
            max_retries=1,
            recovery_action=Step(
                action=ActionType.CLICK,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Dismiss"}),
                risk_tier=RiskTier.RISKY,
            ),
        )
    ]
    surface = _RecoverableUntilDismissedSurface(checkpoint_result=True)
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="replay_recovery_escalation_transport_fail")
    escalation = EscalationController(evidence, _RaisingTransport())
    engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)

    result = engine.run(artifact, params={"member_id": "12345"}, confirm_risky=False)

    assert result.outcome == OutcomeType.HARD_FAILURE
    assert "RuntimeError" in result.detail
    assert result.expected == "recovery_action's escalation to complete"
    assert surface.acted == []  # the recovery click never ran either


def test_hard_failure_still_returned_when_no_outcome_pattern_matches(tmp_path):
    artifact = _make_artifact()
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.BUSINESS_OUTCOME,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="Insufficient funds for this transfer."),
            detail="insufficient funds",
        )
    ]
    surface = FakeSurface(checkpoint_result=False, matching_checkpoint_text="some other unrelated text")
    engine = _make_engine(surface, tmp_path)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.HARD_FAILURE


def test_business_outcome_detected_mid_sequence_before_next_step_is_attempted(tmp_path):
    # Simulates: the app diverges onto a business-outcome page after step 3, and the
    # artifact's recorded step 4+ (e.g. a "Confirm" button on a page that was never
    # reached) must not be blindly attempted.
    artifact = _make_artifact()
    artifact.steps.append(
        Step(
            action=ActionType.CLICK,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Confirm"}),
            risk_tier=RiskTier.SAFE,
        )
    )
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.BUSINESS_OUTCOME,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="Insufficient funds for this transfer."),
            detail="insufficient_funds",
        )
    ]
    surface = FakeSurface(
        matching_checkpoint_text="Insufficient funds for this transfer.",
        outcome_visible_after_step=1,  # becomes visible only after the 2nd acted step
    )
    engine = _make_engine(surface, tmp_path)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.BUSINESS_OUTCOME
    assert result.detail == "insufficient_funds"
    # the extra "Confirm" step must never have been attempted
    assert len(surface.acted) == 2


def test_action_exception_returns_hard_failure_instead_of_crashing(tmp_path):
    surface = FakeSurface(raise_on_step=1)
    engine = _make_engine(surface, tmp_path)

    result = engine.run(_make_artifact(), params={"member_id": "12345"})

    assert result.outcome == OutcomeType.HARD_FAILURE
    assert result.step_index == 1
    assert "element not found" in (result.detail or "")
    # the exception's type name must be visible too - a bare message alone
    # doesn't distinguish an expected environmental timeout from a genuine bug.
    assert result.detail.startswith("TimeoutError:")


def test_disallowed_url_step_returns_hard_failure_instead_of_crashing(tmp_path):
    artifact = _make_artifact()
    artifact.steps[0].target = "http://evil.example.com/x"
    surface = FakeSurface()
    engine = _make_engine(surface, tmp_path)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.HARD_FAILURE
    assert result.step_index == 0
    assert "not in allowlist" in (result.detail or "")
    assert len(surface.acted) == 0  # the disallowed action must never actually run


class _ClickNavigatesOffAllowlistSurface(FakeSurface):
    """A CLICK on the recorded locator silently navigates off the allowlisted domain -
    exactly the case the PRE-action allowlist check (which only validates the url we
    were on BEFORE the click) can't catch, since a click has no explicit `target`."""

    def act(self, action, locator, target, text):
        result = super().act(action, locator, target, text)
        if action == ActionType.CLICK:
            self.url = "http://evil.example.com/hijacked"
        return result


def test_click_that_navigates_off_allowlist_is_caught_by_the_post_action_check(tmp_path):
    artifact = _make_artifact()
    artifact.steps.append(
        Step(
            action=ActionType.CLICK,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Search"}),
            risk_tier=RiskTier.SAFE,
        )
    )
    surface = _ClickNavigatesOffAllowlistSurface()
    engine = _make_engine(surface, tmp_path)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.HARD_FAILURE
    assert result.step_index == 2  # the CLICK step - navigate(0), type_text(1), click(2)
    assert "not in allowlist" in (result.detail or "")
    assert result.expected == "click result to stay within the URL/action allowlist"
    # the click itself DID run (this isn't a pre-action block) - the point is that its
    # off-allowlist result is caught immediately after, not silently missed
    assert len(surface.acted) == 3


class _RecoveryClickNavigatesOffAllowlistSurface(FakeSurface):
    """Same idea as _ClickNavigatesOffAllowlistSurface, but for a recovery_action's own
    click rather than a normal recorded step's."""

    def act(self, action, locator, target, text):
        result = super().act(action, locator, target, text)
        if action == ActionType.CLICK and locator is not None and locator.value.get("name") == "Dismiss":
            self.url = "http://evil.example.com/hijacked"
        return result

    def check_checkpoint(self, checkpoint):
        if checkpoint.type == CheckpointType.TEXT_PRESENT and checkpoint.text == "Interstitial":
            return True  # always "visible" - forces a recovery attempt every time
        return super().check_checkpoint(checkpoint)


def test_recovery_action_click_that_navigates_off_allowlist_hard_fails(tmp_path):
    artifact = _make_artifact()
    artifact.outcome_patterns = [
        OutcomePattern(
            outcome=OutcomeType.RECOVERABLE,
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, text="Interstitial"),
            detail="dismissable_interstitial",
            max_retries=2,
            recovery_action=_dismiss_recovery_action(),
        )
    ]
    surface = _RecoveryClickNavigatesOffAllowlistSurface()
    engine = _make_engine(surface, tmp_path)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.HARD_FAILURE
    assert "not in allowlist" in (result.detail or "")


def test_disallowed_url_step_is_not_mistaken_for_a_drifted_locator(tmp_path):
    # cli.py's _is_action_locator_failure() only compares result.expected against
    # f"{action} to succeed" to decide whether --diagnose-drift-on-failure should
    # spend an LLM/vision call proposing a patch. An allowlist rejection is a policy
    # decision, not a UI control that moved - it must produce a distinct `expected`
    # string so it's never mistaken for one.
    from comp_use.cli import _is_action_locator_failure

    artifact = _make_artifact()
    artifact.steps[0].target = "http://evil.example.com/x"
    surface = FakeSurface()
    engine = _make_engine(surface, tmp_path)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.expected != f"{artifact.steps[0].action.value} to succeed"
    assert _is_action_locator_failure(artifact, result) is False


def test_goal_parameter_step_with_unprovided_param_is_validation_error_not_crash(tmp_path):
    # validate_required_params only checks input_schema entries marked required=True;
    # a step can still reference a param name that's missing from `params` (an
    # optional-and-omitted param, or a stale/typo'd artifact). This must be reported
    # as a controlled ReplayResult, not an unhandled KeyError.
    artifact = _make_artifact()
    artifact.steps[1].value_source = ValueSource(
        type="goal_parameter", param_name="deposit_amount", param_type="string"
    )
    surface = FakeSurface()
    engine = _make_engine(surface, tmp_path)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.VALIDATION_ERROR
    assert "deposit_amount" in result.detail
    assert len(surface.acted) == 1  # only the earlier NAVIGATE step ran


def test_goal_parameter_step_still_prefers_caller_param_over_recorded_value(tmp_path):
    artifact = _make_artifact()
    artifact.steps[1].value = "12345"  # value recorded at discovery time
    surface = FakeSurface()
    engine = _make_engine(surface, tmp_path)
    engine.run(artifact, params={"member_id": "67890"})

    type_text_call = surface.acted[1]
    assert type_text_call[2] == "67890"


def test_extract_step_populates_replay_result_outputs(tmp_path):
    artifact = _make_artifact()
    artifact.steps.append(
        Step(
            action=ActionType.EXTRACT,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "generic", "name": "confirmation-number"}),
            extract_as="confirmation_number",
            risk_tier=RiskTier.SAFE,
        )
    )
    surface = FakeSurface()
    engine = _make_engine(surface, tmp_path)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.SUCCESS
    assert result.outputs == {"confirmation_number": "CONF-000123"}


def test_escalation_carries_a_real_screenshot_path(tmp_path):
    artifact = _make_artifact()
    artifact.steps[1].risk_tier = RiskTier.RISKY
    surface = FakeSurface()
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="replay_run_escalate")
    transport = FakeTransport()
    escalation = EscalationController(evidence, transport)
    engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)

    engine.run(artifact, params={"member_id": "12345"}, confirm_risky=False)

    assert len(transport.notified) == 1
    request = transport.notified[0]
    assert request.screenshot_path is not None
    assert Path(request.screenshot_path).exists()


def test_escalation_survives_screenshot_capture_failure(tmp_path):
    # Same live crash as DiscoveryAgent's - Page.screenshot() timing out on
    # its own must not crash a risky-step escalation, arguably the single
    # most safety-critical path in the system.
    artifact = _make_artifact()
    artifact.steps[1].risk_tier = RiskTier.RISKY
    surface = FakeSurface(raise_on_screenshot=True)
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="replay_run_escalate_screenshot_fail")
    transport = FakeTransport()
    escalation = EscalationController(evidence, transport)
    engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)

    result = engine.run(artifact, params={"member_id": "12345"}, confirm_risky=False)

    assert result.outcome == OutcomeType.SUCCESS
    assert len(transport.notified) == 1
    assert transport.notified[0].screenshot_path is None


def _engine_for(tmp_path, surface, run_id, transport=None):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id=run_id)
    escalation = EscalationController(evidence, transport) if transport is not None else None
    return ReplayEngine(surface, guardrail, evidence, escalation=escalation), evidence


class _MeridianPageSurface(FakeSurface):
    """Reports whatever page copy it was given for TEXT_PRESENT checks, so a test can
    put the run on a real MERIDIAN error page and let the host-wide library classify
    it - rather than hand-authoring the pattern into the artifact, which is precisely
    what stopped working when a shared pattern needed fixing."""

    def __init__(self, page_text, reason=None, **kwargs):
        super().__init__(**kwargs)
        self.page_text = page_text
        self.reason = reason

    def check_checkpoint(self, checkpoint):
        if checkpoint.type == CheckpointType.TEXT_PRESENT:
            return checkpoint.text in self.page_text
        return super().check_checkpoint(checkpoint)

    def act(self, action, locator, target, text):
        if action == ActionType.EXTRACT and self.reason is not None:
            return self.reason
        return super().act(action, locator, target, text)


def _meridian_artifact():
    artifact = _make_artifact()
    artifact.target = {"app": "web-sample.interface-hiring.com", "base_url": "https://web-sample.interface-hiring.com"}
    return artifact


def test_host_taxonomy_classifies_an_artifact_that_carries_no_patterns_of_its_own(tmp_path):
    # Artifacts no longer snapshot the shared library (compile_artifact leaves the
    # field empty), so this is the normal case, not an edge case: a capability
    # recorded months ago still gets today's taxonomy.
    artifact = _meridian_artifact()
    assert artifact.outcome_patterns == []
    surface = _MeridianPageSurface("RECORD NOT FOUND The requested member record could not be located")
    engine, _ = _engine_for(tmp_path, surface, "replay_host_taxonomy")

    result = engine.run(artifact, params={"member_id": "999999"})

    assert result.outcome == OutcomeType.BUSINESS_OUTCOME
    assert "could not be located" in result.detail


def test_a_rejection_reports_the_reason_the_page_gives_not_just_the_category(tmp_path):
    # "The transaction could not be validated" only names the category. The rule that
    # actually failed sits in a list beneath it, and is the half a caller can act on.
    artifact = _meridian_artifact()
    surface = _MeridianPageSurface(
        "FUNDS TRANSFER The transaction could not be validated:",
        reason="Insufficient available balance in the source share.",
    )
    engine, _ = _engine_for(tmp_path, surface, "replay_reason_detail")

    result = engine.run(artifact, params={"member_id": "100234"})

    assert result.outcome == OutcomeType.BUSINESS_OUTCOME
    assert "Insufficient available balance in the source share." in result.detail


def test_a_recognised_rejection_beats_the_assumption_that_a_human_did_the_step(tmp_path):
    # Live-observed: a transfer whose source share was on HOLD escalated at its risky
    # step, the human resumed without fixing anything, and the "Post Transfer" click
    # then failed only because the rejection page has no such button. Assuming "the
    # human must have done it" logged a success-shaped event for a transfer that never
    # posted. The rejection page is the answer and must win.
    artifact = _meridian_artifact()
    artifact.steps[1].risk_tier = RiskTier.RISKY
    surface = _MeridianPageSurface(
        "FUNDS TRANSFER The transaction could not be validated:",
        reason="Source share is HOLD and cannot be debited.",
        raise_on_step=1,
        # A rejection page is not the success page: the checkpoint _finish_if_already_
        # succeeded consults right after the human resumes has to read False here, or
        # the run would report SUCCESS for a transfer that was refused.
        checkpoint_result=False,
    )
    transport = FakeTransport()
    engine, evidence = _engine_for(tmp_path, surface, "replay_rejection_beats_assumption", transport)

    result = engine.run(artifact, params={"member_id": "100234"}, confirm_risky=False)

    assert len(transport.notified) == 1
    assert result.outcome == OutcomeType.BUSINESS_OUTCOME
    assert "Source share is HOLD" in result.detail

    events = [
        json.loads(line)
        for line in (evidence.run_dir / "log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert not [e for e in events if e["event_type"] == "step_completed_during_escalation"]


def test_an_application_error_page_is_reported_in_those_words(tmp_path):
    # Still a hard failure - but named, rather than surfacing as whichever locator
    # timed out first on a page the run never expected to be looking at.
    artifact = _meridian_artifact()
    surface = _MeridianPageSurface("APPLICATION ERROR An unexpected error occurred. Reference: ERR-3A0AEC77")
    engine, _ = _engine_for(tmp_path, surface, "replay_application_error")

    result = engine.run(artifact, params={"member_id": "100234"})

    assert result.outcome == OutcomeType.HARD_FAILURE
    assert "application error" in result.detail.lower()


class _MaintenanceThenSignedOutSurface(_MeridianPageSurface):
    """Models the live A2 shape: an interstitial appears, its Continue link is clicked
    (dismissing it), and the host comes back SIGNED OUT - so the page that follows shows
    no error at all and every later step fails against it."""

    def __init__(self, **kwargs):
        super().__init__("SCHEDULED MAINTENANCE IN PROGRESS The host is temporarily unavailable", **kwargs)
        self.dismissed = False

    def act(self, action, locator, target, text):
        # The recovery click is the one that clears the interstitial.
        if not self.dismissed and action == ActionType.CLICK:
            self.dismissed = True
            self.page_text = "OPERATOR SIGN ON Operator ID: Password: Branch:"
            return None
        if self.dismissed:
            raise TimeoutError("element not found: the session was lost with the interstitial")
        return super().act(action, locator, target, text)


def test_a_dismissed_interstitial_that_cost_the_session_reports_recoverable(tmp_path):
    # A2, observed live: the maintenance interstitial WAS matched and its Continue link
    # WAS clicked - classification and recovery both worked. But dismissing it returned
    # the host to a signed-out page, and replay carried on at the step it was on rather
    # than the sign-on it had lost. Reporting that as hard_failure blames the automation
    # for a transient host condition it recognised and handled correctly; the caller
    # needs to be told to re-invoke instead.
    artifact = _meridian_artifact()
    surface = _MaintenanceThenSignedOutSurface(checkpoint_result=False)
    engine, _ = _engine_for(tmp_path, surface, "replay_interstitial_lost_session")

    result = engine.run(artifact, params={"member_id": "103001"})

    assert result.outcome == OutcomeType.RECOVERABLE
    assert "re-invoke" in result.detail.lower()


def test_outputs_survive_a_failed_run(tmp_path):
    # A4: a run that read three values and then died still knows those three values.
    # Dropping them made a partial answer indistinguishable from no answer.
    artifact = _meridian_artifact()
    artifact.steps.append(
        Step(
            action=ActionType.EXTRACT,
            locator=Locator(strategy=LocatorStrategy.CSS, value={"css": "td.balance"}),
            extract_as="balance",
        )
    )
    # Succeeds through the extract, then the final success checkpoint never passes and
    # no outcome pattern explains it.
    surface = _MeridianPageSurface("MEMBER RECORD nothing unusual here", checkpoint_result=False)
    engine, _ = _engine_for(tmp_path, surface, "replay_outputs_on_failure")

    result = engine.run(artifact, params={"member_id": "103001"})

    assert result.outcome == OutcomeType.HARD_FAILURE
    assert result.outputs.get("balance") == "CONF-000123"
