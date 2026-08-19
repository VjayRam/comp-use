import pytest

from comp_use.config import load_settings
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.replay.engine import ReplayEngine
from comp_use.schemas import (
    ActionType, Artifact, Checkpoint, CheckpointType, InputParam, Locator,
    LocatorStrategy, OutcomeType, RiskTier, Step, ValueSource,
)


class FakeSurface:
    def __init__(self, checkpoint_result=True, fail_on_step=None):
        self.acted = []
        self.checkpoint_result = checkpoint_result
        self.fail_on_step = fail_on_step
        self.url = "http://localhost:5000/member/search"

    def act(self, action, locator, target, text):
        self.acted.append((action, target, text))
        if action == ActionType.NAVIGATE:
            self.url = target

    def observe(self):
        from comp_use.surface import ObservedState
        return ObservedState(accessibility_tree="tree", url=self.url)

    def check_checkpoint(self, checkpoint):
        if self.fail_on_step is not None and len(self.acted) - 1 == self.fail_on_step:
            return False
        return self.checkpoint_result

    def screenshot(self):
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


def test_goal_parameter_step_still_prefers_caller_param_over_recorded_value(tmp_path):
    artifact = _make_artifact()
    artifact.steps[1].value = "12345"  # value recorded at discovery time
    surface = FakeSurface()
    engine = _make_engine(surface, tmp_path)
    engine.run(artifact, params={"member_id": "67890"})

    type_text_call = surface.acted[1]
    assert type_text_call[2] == "67890"
