import time

from fastapi.testclient import TestClient

from comp_use.cli import save_artifact
from comp_use.config import Settings
from comp_use.schemas import (
    Artifact,
    Checkpoint,
    CheckpointType,
    InputParam,
    InterventionRequest,
    OutcomeType,
    OutputParam,
    ReplayResult,
)
from comp_use.server.app import create_app
import comp_use.server.app as app_module


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


def _wait_for_status(client, run_id, status, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body["status"] == status:
            return body
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} never reached status={status!r}, last body: {body}")


def test_invoke_400s_on_missing_required_params_without_starting_a_run(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)

    called = []
    monkeypatch.setattr(app_module, "run_replay", lambda *a, **k: called.append(1))

    response = client.post("/capabilities/lookup_member/invoke", json={"params": {}})

    assert response.status_code == 400
    assert called == []


def test_invoke_404s_when_no_approved_version_exists(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="draft"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/invoke", json={"params": {"member_id": "12345"}})

    assert response.status_code == 404


def test_invoke_runs_in_the_background_and_reports_success(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)
    monkeypatch.setattr(
        app_module, "run_replay",
        lambda capability_name, params, confirm_risky, diagnose_drift_on_failure, transport:
            ReplayResult(outcome=OutcomeType.SUCCESS, outputs={"balance": "100"}),
    )

    response = client.post("/capabilities/lookup_member/invoke", json={"params": {"member_id": "12345"}})
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    body = _wait_for_status(client, run_id, "done")
    assert body["result"]["outcome"] == "success"
    assert body["result"]["outputs"]["balance"] == "100"


def test_discover_runs_in_the_background_and_reports_the_new_draft_version(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    monkeypatch.setattr(
        app_module, "run_discover",
        lambda goal, start_url, capability_name, confirm_risky, transport, interactive:
            _artifact(capability_name=capability_name, version=1, status="draft"),
    )

    response = client.post(
        "/capabilities/new_capability/discover",
        json={"goal": "look up a member", "start_url": "http://localhost:5000/member/search"},
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    body = _wait_for_status(client, run_id, "done")
    assert body["discover_result"] == {"succeeded": True, "artifact_version": 1}


def test_discover_reports_failure_when_run_discover_returns_none(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    monkeypatch.setattr(
        app_module, "run_discover",
        lambda goal, start_url, capability_name, confirm_risky, transport, interactive: None,
    )

    response = client.post(
        "/capabilities/new_capability/discover",
        json={"goal": "an impossible goal", "start_url": "http://localhost:5000/member/search"},
    )
    run_id = response.json()["run_id"]

    body = _wait_for_status(client, run_id, "done")
    assert body["discover_result"] == {"succeeded": False, "artifact_version": None}


def test_run_escalates_and_resumes_over_http(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)

    def fake_run_replay(capability_name, params, confirm_risky, diagnose_drift_on_failure, transport):
        transport.notify(InterventionRequest(run_id="ignored", capability_or_goal=capability_name, reason="risky step"))
        note = transport.wait_for_resume()
        return ReplayResult(outcome=OutcomeType.SUCCESS, detail=note)

    monkeypatch.setattr(app_module, "run_replay", fake_run_replay)

    response = client.post("/capabilities/lookup_member/invoke", json={"params": {"member_id": "12345"}})
    run_id = response.json()["run_id"]

    escalated = _wait_for_status(client, run_id, "escalated")
    assert escalated["escalation"]["reason"] == "risky step"

    resume_response = client.post(f"/runs/{run_id}/resume", json={"note": "confirmed manually"})
    assert resume_response.status_code == 202

    done = _wait_for_status(client, run_id, "done")
    assert done["result"]["detail"] == "confirmed manually"


def test_resume_409s_when_run_is_not_escalated(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)
    monkeypatch.setattr(
        app_module, "run_replay",
        lambda capability_name, params, confirm_risky, diagnose_drift_on_failure, transport:
            ReplayResult(outcome=OutcomeType.SUCCESS),
    )

    response = client.post("/capabilities/lookup_member/invoke", json={"params": {"member_id": "12345"}})
    run_id = response.json()["run_id"]
    _wait_for_status(client, run_id, "done")

    resume_response = client.post(f"/runs/{run_id}/resume", json={"note": "too late"})
    assert resume_response.status_code == 409


def test_get_run_404s_for_unknown_run_id(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/runs/does_not_exist")
    assert response.status_code == 404


def test_approve_endpoint_flips_draft_to_approved_and_is_visible_in_catalog(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="draft"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/versions/1/approve")

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert client.get("/capabilities/lookup_member").status_code == 200


def test_approve_endpoint_409s_on_a_version_that_is_not_a_draft(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="approved"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/versions/1/approve")

    assert response.status_code == 409


def test_approve_endpoint_404s_on_an_unknown_version(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="draft"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/versions/99/approve")

    assert response.status_code == 404


def test_reject_endpoint_flips_draft_to_rejected(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="draft"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/versions/1/reject")

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_retire_endpoint_flips_approved_to_rejected(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="approved"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/versions/1/retire")

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    # retired means no longer picked up as the default
    assert client.get("/capabilities/lookup_member").status_code == 404
