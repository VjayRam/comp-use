from fastapi.testclient import TestClient

from comp_use.cli import save_artifact
from comp_use.config import Settings
from comp_use.schemas import Artifact, Checkpoint, CheckpointType, InputParam, OutputParam
from comp_use.server.app import create_app


def _artifact(capability_name="lookup_member", version=1, status="approved", description="Looks up a member."):
    return Artifact(
        capability_name=capability_name,
        version=version,
        status=status,
        description=description,
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        input_schema=[InputParam(name="member_id", type="string", required=True)],
        output_schema=[OutputParam(name="balance", type="string")],
        steps=[],
        success_checkpoint=Checkpoint(type=CheckpointType.URL_MATCHES, url_pattern="member"),
        created_from_run_id="run_1",
    )


def _client(tmp_path):
    settings = Settings(artifacts_dir=tmp_path / "artifacts", evidence_dir=tmp_path / "evidence")
    app = create_app(settings)
    return TestClient(app), settings


def test_list_capabilities_returns_the_latest_approved_summary(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(version=1), settings.artifacts_dir)
    save_artifact(_artifact(version=2, status="draft"), settings.artifacts_dir)

    response = client.get("/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["capability_name"] == "lookup_member"
    assert body[0]["version"] == 1  # v2 is draft, skipped
    assert body[0]["has_pending_draft"] is True
    assert body[0]["input_schema"][0]["name"] == "member_id"


def test_list_capabilities_is_empty_when_no_artifacts_dir_exists(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/capabilities")
    assert response.status_code == 200
    assert response.json() == []


def test_get_capability_returns_full_schema(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)

    response = client.get("/capabilities/lookup_member")

    assert response.status_code == 200
    assert response.json()["description"] == "Looks up a member."


def test_get_capability_404s_when_none_approved(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="draft"), settings.artifacts_dir)

    response = client.get("/capabilities/lookup_member")

    assert response.status_code == 404


def test_get_capability_400s_on_a_path_traversal_attempt(tmp_path):
    client, _ = _client(tmp_path)
    # httpx (the TestClient's transport) normalizes a literal ".." out of the
    # URL client-side before the request is ever sent - "/capabilities/.."
    # collapses to "/" and never reaches the route at all. Percent-encoding
    # the traversal segment survives that normalization and reaches the app
    # as the literal string "..", which is what actually exercises
    # _validate_capability_name (and what a real attacker would have to send
    # to get a traversal payload past client-side dot-segment removal).
    response = client.get("/capabilities/%2e%2e")
    assert response.status_code == 400


def test_list_versions_reports_status_for_every_version(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(version=1, status="approved"), settings.artifacts_dir)
    save_artifact(_artifact(version=2, status="draft"), settings.artifacts_dir)

    response = client.get("/capabilities/lookup_member/versions")

    assert response.status_code == 200
    versions = {v["version"]: v["status"] for v in response.json()}
    assert versions == {1: "approved", 2: "draft"}
