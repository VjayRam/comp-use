import pytest
from pydantic import ValidationError

from comp_use.schemas import (
    Artifact, ActionType, Checkpoint, CheckpointType, InputParam, Locator,
    LocatorStrategy, OutcomeType, OutputParam, ReplayResult, RiskTier, Step,
    ValueSource,
)


def test_artifact_round_trips_through_json():
    artifact = Artifact(
        capability_name="open_sub_account",
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        description="Opens a new sub-account for an existing member.",
        input_schema=[
            InputParam(name="member_id", type="string", example="12345"),
        ],
        output_schema=[
            OutputParam(name="sub_account_id", type="string"),
        ],
        steps=[
            Step(
                action=ActionType.NAVIGATE,
                target="/member/search",
                risk_tier=RiskTier.SAFE,
            ),
            Step(
                action=ActionType.TYPE_TEXT,
                locator=Locator(
                    strategy=LocatorStrategy.ROLE,
                    value={"role": "textbox", "name": "Member ID"},
                ),
                value_source=ValueSource(
                    type="goal_parameter", param_name="member_id", param_type="string"
                ),
                risk_tier=RiskTier.SAFE,
            ),
        ],
        success_checkpoint=Checkpoint(
            type=CheckpointType.ELEMENT_VISIBLE,
            locator=Locator(
                strategy=LocatorStrategy.ROLE,
                value={"role": "heading", "name": "Confirmation"},
            ),
        ),
        created_from_run_id="run_abc123",
    )
    dumped = artifact.model_dump_json()
    restored = Artifact.model_validate_json(dumped)
    assert restored.capability_name == "open_sub_account"
    assert restored.steps[1].value_source.param_name == "member_id"


def test_step_requires_risk_tier_default_safe():
    step = Step(action=ActionType.CLICK, locator=Locator(
        strategy=LocatorStrategy.TEXT, value={"text": "Search"}
    ))
    assert step.risk_tier == RiskTier.SAFE


def test_replay_result_outcome_enum_rejects_invalid():
    with pytest.raises(ValidationError):
        ReplayResult(outcome="not_a_real_outcome")


def test_replay_result_valid_outcome():
    result = ReplayResult(outcome=OutcomeType.SUCCESS, outputs={"sub_account_id": "9"})
    assert result.outcome == OutcomeType.SUCCESS
    assert result.outputs["sub_account_id"] == "9"
