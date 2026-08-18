import json

from comp_use.cli import load_artifact, save_artifact
from comp_use.schemas import ActionType, Artifact, Checkpoint, CheckpointType, Locator, LocatorStrategy, RiskTier, Step


def _artifact(version=1):
    return Artifact(
        capability_name="lookup_member",
        version=version,
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        steps=[Step(action=ActionType.NAVIGATE, target="/member/search", risk_tier=RiskTier.SAFE)],
        success_checkpoint=Checkpoint(
            type=CheckpointType.ELEMENT_VISIBLE,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Member Detail"}),
        ),
        created_from_run_id="run_1",
    )


def test_save_artifact_writes_versioned_json(tmp_path):
    path = save_artifact(_artifact(version=1), tmp_path)
    assert path == tmp_path / "lookup_member" / "v1.json"
    assert json.loads(path.read_text())["capability_name"] == "lookup_member"


def test_load_artifact_loads_latest_version_when_unspecified(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)
    save_artifact(_artifact(version=2), tmp_path)
    loaded = load_artifact("lookup_member", tmp_path)
    assert loaded.version == 2


def test_load_artifact_loads_specific_version(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)
    save_artifact(_artifact(version=2), tmp_path)
    loaded = load_artifact("lookup_member", tmp_path, version=1)
    assert loaded.version == 1
