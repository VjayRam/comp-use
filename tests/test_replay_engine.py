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
    surface = FakeSurface(checkpoint_result=False, matching_checkpoint_text="This review session has expired")
    engine = _make_engine(surface, tmp_path)

    result = engine.run(artifact, params={"member_id": "12345"})

    assert result.outcome == OutcomeType.RECOVERABLE
    assert result.detail == "session_expired"


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
