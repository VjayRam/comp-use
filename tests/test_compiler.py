from comp_use.discovery.agent import RunTrace
from comp_use.discovery.compiler import compile_artifact
from comp_use.schemas import ActionType, Checkpoint, CheckpointType, Locator, LocatorStrategy, RiskTier, Step, ValueSource


def test_compile_artifact_derives_input_schema_from_value_sources():
    trace = RunTrace(
        run_id="run_abc",
        goal="Open sub-account for member 12345",
        steps=[
            Step(action=ActionType.NAVIGATE, target="/member/search", risk_tier=RiskTier.SAFE),
            Step(
                action=ActionType.TYPE_TEXT,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "Member ID"}),
                value_source=ValueSource(type="goal_parameter", param_name="member_id", param_type="string"),
                risk_tier=RiskTier.SAFE,
            ),
            Step(
                action=ActionType.CLICK,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Search"}),
                risk_tier=RiskTier.SAFE,
            ),
        ],
        final_url="http://localhost:5000/member/12345",
        succeeded=True,
    )
    success_checkpoint = Checkpoint(
        type=CheckpointType.ELEMENT_VISIBLE,
        locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Member Detail"}),
    )

    artifact = compile_artifact(
        trace,
        capability_name="lookup_member",
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        success_checkpoint=success_checkpoint,
        output_schema=[],
    )

    assert artifact.capability_name == "lookup_member"
    assert len(artifact.input_schema) == 1
    assert artifact.input_schema[0].name == "member_id"
    assert artifact.input_schema[0].type == "string"
    assert artifact.created_from_run_id == "run_abc"
    assert len(artifact.steps) == 3
