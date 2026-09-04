from comp_use.chat.catalog import build_chat_catalog
from comp_use.cli import save_artifact
from comp_use.config import Settings
from comp_use.schemas import Artifact, Checkpoint, CheckpointType, InputParam


def _artifact(capability_name="lookup_member", version=1, status="approved", is_default=False, base_url="http://localhost:5000"):
    return Artifact(
        capability_name=capability_name,
        version=version,
        status=status,
        is_default=is_default,
        description="Looks up a member.",
        target={"app": "mock_bank", "base_url": base_url},
        input_schema=[InputParam(name="member_id", type="string", required=True)],
        steps=[],
        success_checkpoint=Checkpoint(type=CheckpointType.URL_MATCHES, url_pattern="member"),
        created_from_run_id="run_1",
    )


def test_build_chat_catalog_includes_approved_capabilities_with_their_site_and_schema(tmp_path):
    save_artifact(_artifact(), tmp_path)
    settings = Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence")

    catalog = build_chat_catalog(settings)

    assert catalog == [{
        "capability_name": "lookup_member",
        "description": "Looks up a member.",
        "target_base_url": "http://localhost:5000",
        "input_schema": [{"name": "member_id", "type": "string", "required": True}],
    }]


def test_build_chat_catalog_skips_a_capability_with_no_approved_version(tmp_path):
    save_artifact(_artifact(status="draft"), tmp_path)
    settings = Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence")

    assert build_chat_catalog(settings) == []


def test_build_chat_catalog_prefers_the_is_default_version_over_the_highest_number(tmp_path):
    save_artifact(_artifact(version=1, status="approved", is_default=True, base_url="http://a"), tmp_path)
    save_artifact(_artifact(version=2, status="approved", is_default=False, base_url="http://b"), tmp_path)
    settings = Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence")

    catalog = build_chat_catalog(settings)

    assert len(catalog) == 1
    assert catalog[0]["target_base_url"] == "http://a"


def test_build_chat_catalog_is_empty_when_no_capabilities_exist(tmp_path):
    settings = Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence")
    assert build_chat_catalog(settings) == []
