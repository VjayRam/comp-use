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


def test_checkpoint_failure_returns_hard_failure_with_step_detail(tmp_path):
    surface = FakeSurface(fail_on_step=1)
    engine = _make_engine(surface, tmp_path)
    result = engine.run(_make_artifact(), params={"member_id": "12345"})
    assert result.outcome == OutcomeType.HARD_FAILURE
    assert result.step_index == 1


def test_fixed_step_replays_recorded_literal_value_regardless_of_params(tmp_path):
    artifact = _make_artifact()
    artifact.steps.append(
        Step(
            action=ActionType.TYPE_TEXT,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "Note"}),
            value_source=ValueSource(type="fixed", reason="boilerplate note"),
            value="Opened at teller request",
            risk_tier=RiskTier.SAFE,
        )
    )
    surface = FakeSurface()
    engine = _make_engine(surface, tmp_path)
    engine.run(artifact, params={"member_id": "12345"})

    fixed_step_call = surface.acted[-1]
    assert fixed_step_call[2] == "Opened at teller request"


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
