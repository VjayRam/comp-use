from comp_use.drift import propose_drift_patch
from comp_use.llm_client import FakeLLMClient
from comp_use.schemas import (
    ActionType, Artifact, Checkpoint, CheckpointType, InputParam, Locator,
    LocatorStrategy, RiskTier, Step,
)


class FakeSurfaceForDrift:
    def __init__(self, png=b"fakepng"):
        self._png = png

    def screenshot(self):
        return self._png


def _make_artifact():
    return Artifact(
        capability_name="transfer_funds",
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        input_schema=[InputParam(name="member_id", type="string", required=True)],
        steps=[
            Step(action=ActionType.NAVIGATE, target="http://localhost:5000/member/12345/transfer", risk_tier=RiskTier.SAFE),
            Step(
                action=ActionType.CLICK,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Confirm Transfer"}),
                risk_tier=RiskTier.RISKY,
            ),
        ],
        success_checkpoint=Checkpoint(
            type=CheckpointType.ELEMENT_VISIBLE,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Confirmation"}),
        ),
        created_from_run_id="run_x",
    )


def test_propose_drift_patch_returns_patched_artifact_when_found():
    artifact = _make_artifact()
    llm = FakeLLMClient(
        scripted_actions=[],
        scripted_drift_diagnoses=[
            {
                "found": True,
                "locator": {"strategy": "role", "value": {"role": "button", "name": "Submit Transfer"}},
                "reasoning": "same position, relabeled",
            }
        ],
    )
    surface = FakeSurfaceForDrift()

    diagnosis = propose_drift_patch(llm, surface, artifact, failed_step_index=1)

    assert diagnosis.patched_artifact is not None
    assert diagnosis.patched_artifact.steps[1].locator.value["name"] == "Submit Transfer"
    assert diagnosis.reasoning == "same position, relabeled"
    # the original artifact object must not be mutated
    assert artifact.steps[1].locator.value["name"] == "Confirm Transfer"
    # nothing else about the step should change
    assert diagnosis.patched_artifact.steps[1].risk_tier == RiskTier.RISKY
    assert diagnosis.patched_artifact.steps[0].target == artifact.steps[0].target


def test_propose_drift_patch_returns_no_patch_when_not_found():
    artifact = _make_artifact()
    llm = FakeLLMClient(
        scripted_actions=[],
        scripted_drift_diagnoses=[{"found": False, "reasoning": "no similar control visible"}],
    )
    surface = FakeSurfaceForDrift()

    diagnosis = propose_drift_patch(llm, surface, artifact, failed_step_index=1)

    assert diagnosis.patched_artifact is None
    assert diagnosis.reasoning == "no similar control visible"


def test_propose_drift_patch_returns_no_patch_when_found_but_locator_invalid():
    artifact = _make_artifact()
    llm = FakeLLMClient(
        scripted_actions=[],
        scripted_drift_diagnoses=[{"found": True, "locator": {"strategy": "not_a_real_strategy", "value": {}}}],
    )
    surface = FakeSurfaceForDrift()

    diagnosis = propose_drift_patch(llm, surface, artifact, failed_step_index=1)

    assert diagnosis.patched_artifact is None


class _ExplodingLLMClient:
    def diagnose_drift(self, expected_locator, screenshot_b64):
        raise AssertionError("should never be called when the failed step has no locator")


def test_propose_drift_patch_skips_llm_call_when_step_has_no_locator():
    artifact = _make_artifact()  # step 0 is NAVIGATE, no locator
    surface = FakeSurfaceForDrift()

    diagnosis = propose_drift_patch(_ExplodingLLMClient(), surface, artifact, failed_step_index=0)

    assert diagnosis.patched_artifact is None
