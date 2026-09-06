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
from comp_use.chat.agent import ChatAgent
from comp_use.llm_client import FakeLLMClient


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


def test_invoke_409s_when_pinned_version_is_not_approved_and_never_starts_a_run(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(version=1, status="approved"), settings.artifacts_dir)
    save_artifact(_artifact(version=2, status="draft"), settings.artifacts_dir)

    called = []
    monkeypatch.setattr(app_module, "run_replay", lambda *a, **k: called.append(1))

    response = client.post(
        "/capabilities/lookup_member/invoke",
        json={"params": {"member_id": "12345"}, "version": 2},
    )

    assert response.status_code == 409
    assert "version 2" in response.json()["detail"]
    assert "draft" in response.json()["detail"]
    assert called == []  # no background run was ever started


def test_invoke_accepts_a_pinned_version_that_is_approved(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(version=1, status="approved"), settings.artifacts_dir)
    save_artifact(_artifact(version=2, status="approved"), settings.artifacts_dir)

    seen_versions = []

    def fake_run_replay(capability_name, params, confirm_risky, diagnose_drift_on_failure, transport, version=None, run_id=None):
        seen_versions.append(version)
        return ReplayResult(outcome=OutcomeType.SUCCESS, outputs={"balance": "100"})

    monkeypatch.setattr(app_module, "run_replay", fake_run_replay)

    response = client.post(
        "/capabilities/lookup_member/invoke",
        json={"params": {"member_id": "12345"}, "version": 1},
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    body = _wait_for_status(client, run_id, "done")
    assert body["result"]["outcome"] == "success"
    assert seen_versions == [1]  # the pin was honored end to end, all the way to run_replay


def test_invoke_defaults_to_latest_approved_version_when_unpinned(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(version=1, status="approved"), settings.artifacts_dir)
    save_artifact(_artifact(version=2, status="draft"), settings.artifacts_dir)

    seen_versions = []

    def fake_run_replay(capability_name, params, confirm_risky, diagnose_drift_on_failure, transport, version=None, run_id=None):
        seen_versions.append(version)
        return ReplayResult(outcome=OutcomeType.SUCCESS)

    monkeypatch.setattr(app_module, "run_replay", fake_run_replay)

    response = client.post("/capabilities/lookup_member/invoke", json={"params": {"member_id": "12345"}})
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    _wait_for_status(client, run_id, "done")
    assert seen_versions == [None]  # unpinned - run_replay itself resolves "latest approved"


def test_invoke_runs_in_the_background_and_reports_success(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)
    monkeypatch.setattr(
        app_module, "run_replay",
        lambda capability_name, params, confirm_risky, diagnose_drift_on_failure, transport, version=None, run_id=None:
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
        lambda goal, start_url, capability_name, confirm_risky, transport, interactive, param_hints=None, run_id=None:
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
        lambda goal, start_url, capability_name, confirm_risky, transport, interactive, param_hints=None, run_id=None: None,
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

    def fake_run_replay(capability_name, params, confirm_risky, diagnose_drift_on_failure, transport, version=None, run_id=None):
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


def test_takeover_endpoint_flags_the_run_and_agent_escalates_with_a_manual_reason(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)

    def fake_run_replay(capability_name, params, confirm_risky, diagnose_drift_on_failure, transport, version=None, run_id=None):
        # Mirrors the poll DiscoveryAgent/ReplayEngine's real loop does once per step.
        deadline = time.time() + 1.0
        while time.time() < deadline and not transport.takeover_requested():
            time.sleep(0.01)
        transport.notify(
            InterventionRequest(run_id="ignored", capability_or_goal=capability_name, reason="manual takeover requested by operator")
        )
        note = transport.wait_for_resume()
        return ReplayResult(outcome=OutcomeType.SUCCESS, detail=note)

    monkeypatch.setattr(app_module, "run_replay", fake_run_replay)

    response = client.post("/capabilities/lookup_member/invoke", json={"params": {"member_id": "12345"}})
    run_id = response.json()["run_id"]

    takeover_response = client.post(f"/runs/{run_id}/takeover")
    assert takeover_response.status_code == 202

    escalated = _wait_for_status(client, run_id, "escalated")
    assert escalated["escalation"]["reason"] == "manual takeover requested by operator"

    resume_response = client.post(f"/runs/{run_id}/resume", json={"note": "handed back"})
    assert resume_response.status_code == 202
    done = _wait_for_status(client, run_id, "done")
    assert done["result"]["detail"] == "handed back"


def test_takeover_endpoint_404s_for_an_unknown_run_id(tmp_path):
    client, _ = _client(tmp_path)
    response = client.post("/runs/does_not_exist/takeover")
    assert response.status_code == 404


def test_takeover_endpoint_409s_once_the_run_is_done(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)
    monkeypatch.setattr(
        app_module, "run_replay",
        lambda capability_name, params, confirm_risky, diagnose_drift_on_failure, transport, version=None, run_id=None:
            ReplayResult(outcome=OutcomeType.SUCCESS),
    )

    response = client.post("/capabilities/lookup_member/invoke", json={"params": {"member_id": "12345"}})
    run_id = response.json()["run_id"]
    _wait_for_status(client, run_id, "done")

    takeover_response = client.post(f"/runs/{run_id}/takeover")
    assert takeover_response.status_code == 409


def test_resume_409s_when_run_is_not_escalated(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)
    monkeypatch.setattr(
        app_module, "run_replay",
        lambda capability_name, params, confirm_risky, diagnose_drift_on_failure, transport, version=None, run_id=None:
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


def test_get_run_falls_back_to_postgres_for_a_run_from_before_the_last_restart(tmp_path, monkeypatch):
    # RunManager only knows about runs started by THIS process - a run recorded by
    # a previous server process (before a restart) only lives in Postgres. Without
    # this fallback, GET /runs/{run_id} 404s for it even though GET /runs already
    # lists it fine (list_runs has its own Postgres fallback) - observed live as
    # the dashboard's run detail panel spinning on "Loading..." forever.
    client, _ = _client(tmp_path)
    monkeypatch.setattr(app_module.pg_store, "db_enabled", lambda: True)
    monkeypatch.setattr(
        app_module.pg_store, "get_run",
        lambda run_id: {
            "id": run_id, "kind": "invoke", "status": "done", "novnc_url": None,
            "result": {"outcome": "success", "outputs": {"balance": "100"}, "detail": ""},
        } if run_id == "invoke_from_a_prior_process" else None,
    )

    response = client.get("/runs/invoke_from_a_prior_process")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done"
    assert body["result"]["outcome"] == "success"
    assert body["discover_result"] is None
    assert body["escalation"] is None


def test_get_run_still_404s_when_postgres_also_has_nothing(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    monkeypatch.setattr(app_module.pg_store, "db_enabled", lambda: True)
    monkeypatch.setattr(app_module.pg_store, "get_run", lambda run_id: None)

    response = client.get("/runs/does_not_exist")

    assert response.status_code == 404


def test_get_run_maps_a_discover_run_from_postgres_into_discover_result_not_result(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    monkeypatch.setattr(app_module.pg_store, "db_enabled", lambda: True)
    monkeypatch.setattr(
        app_module.pg_store, "get_run",
        lambda run_id: {
            "id": run_id, "kind": "discover", "status": "done", "novnc_url": None,
            "result": {"succeeded": True, "artifact_version": 2, "artifact_status": "draft"},
        },
    )

    response = client.get("/runs/discover_from_a_prior_process")

    assert response.status_code == 200
    body = response.json()
    assert body["result"] is None
    assert body["discover_result"] == {"succeeded": True, "artifact_version": 2, "artifact_status": "draft"}


def test_list_runs_falls_back_to_in_memory_records_when_db_not_configured(tmp_path, monkeypatch):
    # COMP_USE_DB_URL is unset in the test environment, so this exercises the
    # RunManager fallback path in GET /runs, not the Postgres one.
    monkeypatch.delenv("COMP_USE_DB_URL", raising=False)
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)
    monkeypatch.setattr(
        app_module, "run_replay",
        lambda capability_name, params, confirm_risky, diagnose_drift_on_failure, transport, version=None, run_id=None:
            ReplayResult(outcome=OutcomeType.SUCCESS, outputs={}),
    )

    response = client.post("/capabilities/lookup_member/invoke", json={"params": {"member_id": "12345"}})
    run_id = response.json()["run_id"]
    _wait_for_status(client, run_id, "done")

    runs = client.get("/runs").json()
    assert any(r["run_id"] == run_id for r in runs)
    matched = next(r for r in runs if r["run_id"] == run_id)
    assert matched["capability_name"] == "lookup_member"
    assert matched["status"] == "done"


def test_delete_run_endpoint_removes_a_done_run_and_its_evidence_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("COMP_USE_DB_URL", raising=False)
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)
    monkeypatch.setattr(
        app_module, "run_replay",
        lambda capability_name, params, confirm_risky, diagnose_drift_on_failure, transport, version=None, run_id=None:
            ReplayResult(outcome=OutcomeType.SUCCESS),
    )

    response = client.post("/capabilities/lookup_member/invoke", json={"params": {"member_id": "12345"}})
    run_id = response.json()["run_id"]
    _wait_for_status(client, run_id, "done")

    # The fake run_replay above never touches EvidenceLogger - simulate the
    # on-disk evidence dir a real run would have left, so deletion has something
    # real to clean up.
    run_dir = settings.evidence_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "log.jsonl").write_text('{"event_type": "finish", "data": {}}\n', encoding="utf-8")

    delete_response = client.delete(f"/runs/{run_id}")

    assert delete_response.status_code == 200
    assert delete_response.json() == {"run_id": run_id, "deleted": True}
    assert client.get(f"/runs/{run_id}").status_code == 404
    assert not run_dir.exists()


def test_delete_run_endpoint_404s_for_an_unknown_run_id(tmp_path):
    client, _ = _client(tmp_path)
    response = client.delete("/runs/does_not_exist")
    assert response.status_code == 404


def test_delete_run_endpoint_409s_for_an_active_run_and_leaves_it_untouched(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)

    def fake_run_replay(capability_name, params, confirm_risky, diagnose_drift_on_failure, transport, version=None, run_id=None):
        transport.notify(InterventionRequest(run_id="ignored", capability_or_goal=capability_name, reason="risky step"))
        note = transport.wait_for_resume()
        return ReplayResult(outcome=OutcomeType.SUCCESS, detail=note)

    monkeypatch.setattr(app_module, "run_replay", fake_run_replay)
    response = client.post("/capabilities/lookup_member/invoke", json={"params": {"member_id": "12345"}})
    run_id = response.json()["run_id"]
    _wait_for_status(client, run_id, "escalated")

    delete_response = client.delete(f"/runs/{run_id}")
    assert delete_response.status_code == 409
    assert client.get(f"/runs/{run_id}").status_code == 200  # still there

    client.post(f"/runs/{run_id}/resume", json={"note": "done"})


def test_get_run_events_falls_back_to_jsonl_when_db_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("COMP_USE_DB_URL", raising=False)
    client, settings = _client(tmp_path)
    run_dir = settings.evidence_dir / "discover_test123"
    run_dir.mkdir(parents=True)
    (run_dir / "log.jsonl").write_text(
        '{"event_type": "decision", "data": {"action": "click"}}\n'
        '{"event_type": "finish", "data": {}}\n',
        encoding="utf-8",
    )

    response = client.get("/runs/discover_test123/events")

    assert response.status_code == 200
    events = response.json()
    assert [e["event_type"] for e in events] == ["decision", "finish"]
    assert events[0]["data"]["action"] == "click"


def test_get_run_events_is_empty_for_a_run_with_no_evidence(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/runs/never_happened/events")
    assert response.status_code == 200
    assert response.json() == []


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


def test_retire_endpoint_flips_approved_to_retired(tmp_path):
    # "retired" is distinct from "rejected" - a retired version was live in
    # production and was deliberately withdrawn, not a draft that never made it.
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="approved"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/versions/1/retire")

    assert response.status_code == 200
    assert response.json()["status"] == "retired"
    # retired means no longer picked up as the default
    assert client.get("/capabilities/lookup_member").status_code == 404


def test_set_default_endpoint_makes_an_older_version_win_unpinned_resolution(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(version=1, status="approved"), settings.artifacts_dir)
    save_artifact(_artifact(version=2, status="approved"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/versions/1/set-default")
    assert response.status_code == 200
    assert response.json()["is_default"] is True

    assert client.get("/capabilities/lookup_member").json()["version"] == 1
    versions = client.get("/capabilities/lookup_member/versions").json()
    is_default_by_version = {v["version"]: v["is_default"] for v in versions}
    assert is_default_by_version == {1: True, 2: False}


def test_set_default_endpoint_409s_on_a_draft_version(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(version=1, status="draft"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/versions/1/set-default")

    assert response.status_code == 409


def test_set_default_endpoint_404s_on_an_unknown_version(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(version=1, status="approved"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/versions/99/set-default")

    assert response.status_code == 404


def test_clear_default_endpoint_reverts_to_highest_version_wins(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(version=1, status="approved"), settings.artifacts_dir)
    save_artifact(_artifact(version=2, status="approved"), settings.artifacts_dir)
    client.post("/capabilities/lookup_member/versions/1/set-default")

    response = client.post("/capabilities/lookup_member/versions/1/clear-default")

    assert response.status_code == 200
    assert response.json()["is_default"] is False
    assert client.get("/capabilities/lookup_member").json()["version"] == 2


def test_clear_default_endpoint_404s_on_an_unknown_version(tmp_path):
    client, _ = _client(tmp_path)
    response = client.post("/capabilities/lookup_member/versions/99/clear-default")
    assert response.status_code == 404


def test_delete_endpoint_removes_every_version(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(version=1, status="approved"), settings.artifacts_dir)
    save_artifact(_artifact(version=2, status="draft"), settings.artifacts_dir)

    response = client.delete("/capabilities/lookup_member")

    assert response.status_code == 200
    assert response.json() == {"capability_name": "lookup_member", "versions_deleted": 2}
    assert client.get("/capabilities/lookup_member/versions").status_code == 404


def test_delete_endpoint_404s_for_an_unknown_capability(tmp_path):
    client, _ = _client(tmp_path)
    response = client.delete("/capabilities/does_not_exist")
    assert response.status_code == 404


def test_create_chat_session_returns_a_session_id(tmp_path):
    client, _ = _client(tmp_path)
    response = client.post("/chat/sessions")
    assert response.status_code == 201
    assert response.json()["session_id"]


def test_send_chat_message_404s_for_an_unknown_session_id(tmp_path):
    client, _ = _client(tmp_path)
    response = client.post("/chat/sessions/does_not_exist/message", json={"message": "hi"})
    assert response.status_code == 404


def test_send_chat_message_asks_for_a_target_site_on_the_first_turn(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="approved"), settings.artifacts_dir)
    client.app.state.chat_agent = ChatAgent(FakeLLMClient())  # target-site turns never call the LLM

    session_id = client.post("/chat/sessions").json()["session_id"]
    response = client.post(f"/chat/sessions/{session_id}/message", json={"message": "check a balance"})

    assert response.status_code == 200
    assert "mock_bank" not in response.json()["reply"]  # sanity: not echoing internal target.app label
    assert "http://localhost:5000" in response.json()["reply"]
    assert response.json()["run_id"] is None


def test_send_chat_message_starts_a_run_on_confirm(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="approved"), settings.artifacts_dir)
    monkeypatch.setattr(
        app_module, "run_replay",
        lambda capability_name, params, confirm_risky, diagnose_drift_on_failure, transport, version=None, run_id=None:
            ReplayResult(outcome=OutcomeType.SUCCESS, outputs={"balance": "100"}),
    )
    fake_llm = FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke", "arguments": {"capability_name": "lookup_member", "params": {"member_id": "12345"}}},
        {"type": "tool_call", "name": "resolve_pending", "arguments": {"decision": "confirm"}},
    ])
    client.app.state.chat_agent = ChatAgent(fake_llm)

    session_id = client.post("/chat/sessions").json()["session_id"]
    client.post(f"/chat/sessions/{session_id}/message", json={"message": "http://localhost:5000"})  # pick the site
    client.post(f"/chat/sessions/{session_id}/message", json={"message": "check member 12345"})
    response = client.post(f"/chat/sessions/{session_id}/message", json={"message": "yes"})

    assert response.status_code == 200
    run_id = response.json()["run_id"]
    assert run_id is not None
    body = _wait_for_status(client, run_id, "done")
    assert body["result"]["outcome"] == "success"


def test_send_chat_message_starts_a_discovery_run_on_confirm(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="approved"), settings.artifacts_dir)
    started_with = {}

    def fake_run_discover(goal, start_url, capability_name, confirm_risky, transport, interactive, param_hints=None, run_id=None):
        started_with["start_url"] = start_url
        started_with["goal"] = goal
        return None  # discovery "failed" - fine, this test only checks the run got started with the right args

    monkeypatch.setattr(app_module, "run_discover", fake_run_discover)
    fake_llm = FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_discovery", "arguments": {"capability_name": "close_a_share", "goal": "close a share"}},
        {"type": "tool_call", "name": "resolve_pending", "arguments": {"decision": "confirm"}},
    ])
    client.app.state.chat_agent = ChatAgent(fake_llm)

    session_id = client.post("/chat/sessions").json()["session_id"]
    client.post(f"/chat/sessions/{session_id}/message", json={"message": "http://localhost:5000"})
    client.post(f"/chat/sessions/{session_id}/message", json={"message": "close a share"})
    response = client.post(f"/chat/sessions/{session_id}/message", json={"message": "yes"})

    run_id = response.json()["run_id"]
    assert run_id is not None
    _wait_for_status(client, run_id, "done")
    assert started_with["start_url"] == "http://localhost:5000"
    assert started_with["goal"] == "close a share"


def test_send_chat_message_confirm_re_validates_and_skips_the_run_if_capability_was_deleted(tmp_path, monkeypatch):
    # Final-review finding: the confirm path went straight to run_manager.start()
    # with no re-check that the capability still exists - unlike invoke_capability,
    # which 404s before ever starting a background run. This mirrors that guard at
    # the chat endpoint level.
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="approved"), settings.artifacts_dir)
    called = []
    monkeypatch.setattr(app_module, "run_replay", lambda *a, **k: called.append(1))
    fake_llm = FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke", "arguments": {"capability_name": "lookup_member", "params": {"member_id": "12345"}}},
        {"type": "tool_call", "name": "resolve_pending", "arguments": {"decision": "confirm"}},
    ])
    client.app.state.chat_agent = ChatAgent(fake_llm)

    session_id = client.post("/chat/sessions").json()["session_id"]
    client.post(f"/chat/sessions/{session_id}/message", json={"message": "http://localhost:5000"})
    client.post(f"/chat/sessions/{session_id}/message", json={"message": "check member 12345"})

    # The capability is deleted mid-conversation, between the proposal and the confirm.
    delete_response = client.delete("/capabilities/lookup_member")
    assert delete_response.status_code == 200

    response = client.post(f"/chat/sessions/{session_id}/message", json={"message": "yes"})

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] is None
    assert "lookup_member" in body["reply"]
    assert "no longer exists" in body["reply"]
    assert called == []  # no background run was ever started


def test_send_chat_message_confirm_re_validates_missing_required_params(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="approved"), settings.artifacts_dir)
    called = []
    monkeypatch.setattr(app_module, "run_replay", lambda *a, **k: called.append(1))
    fake_llm = FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke", "arguments": {"capability_name": "lookup_member", "params": {"member_id": "12345"}}},
        {"type": "tool_call", "name": "resolve_pending", "arguments": {"decision": "confirm"}},
    ])
    client.app.state.chat_agent = ChatAgent(fake_llm)

    session_id = client.post("/chat/sessions").json()["session_id"]
    client.post(f"/chat/sessions/{session_id}/message", json={"message": "http://localhost:5000"})
    client.post(f"/chat/sessions/{session_id}/message", json={"message": "check member 12345"})

    # Simulate the pending action's stored params going stale by clearing the
    # required member_id param directly on the session before confirming.
    session = client.app.state.chat_sessions.get(session_id)
    session.pending_action.params = {}

    response = client.post(f"/chat/sessions/{session_id}/message", json={"message": "yes"})

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] is None
    assert "lookup_member" in body["reply"]
    assert "member_id" in body["reply"]
    assert called == []  # no background run was ever started


def test_delete_endpoint_leaves_run_history_intact(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="approved"), settings.artifacts_dir)
    monkeypatch.setattr(
        app_module, "run_replay",
        lambda capability_name, params, confirm_risky, diagnose_drift_on_failure, transport, version=None, run_id=None:
            ReplayResult(outcome=OutcomeType.SUCCESS, outputs={}),
    )
    response = client.post("/capabilities/lookup_member/invoke", json={"params": {"member_id": "12345"}})
    run_id = response.json()["run_id"]
    _wait_for_status(client, run_id, "done")

    client.delete("/capabilities/lookup_member")

    # the capability is gone, but the run it created stays visible
    assert client.get("/capabilities/lookup_member").status_code == 404
    assert client.get(f"/runs/{run_id}").status_code == 200


def _draft_with_examples(capability_name="lookup_member", version=1):
    artifact = _artifact(capability_name=capability_name, version=version, status="draft")
    artifact.input_schema = [
        InputParam(name="member_id", type="string", required=True, example="103001"),
        InputParam(name="operator_id", type="string", required=True, example="operator_id"),
    ]
    return artifact


def test_a_draft_version_can_be_read_in_full(tmp_path):
    # GET /capabilities/{name} resolves to the latest APPROVED version, so before this
    # endpoint a draft was visible only as a name and a badge: the dashboard could show
    # that one existed but not its goal, its inputs, or the defaults recorded for them -
    # exactly what a reviewer needs in order to approve it.
    client, settings = _client(tmp_path)
    save_artifact(_draft_with_examples(), settings.artifacts_dir)

    # The approved-only view still refuses it...
    assert client.get("/capabilities/lookup_member").status_code == 404
    # ...while the per-version view returns everything.
    r = client.get("/capabilities/lookup_member/versions/1")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "draft"
    assert body["description"] == "Looks up a member."
    assert [(p["name"], p["example"]) for p in body["input_schema"]] == [
        ("member_id", "103001"),
        ("operator_id", "operator_id"),
    ]


def test_approve_saves_corrected_defaults_before_flipping_status(tmp_path):
    # Reviewing a draft is exactly when a wrong recorded default gets noticed - here a
    # discovery run captured the literal placeholder "operator_id" as the operator's
    # example. The correction and the approval are one call so a caller can never see an
    # approved capability still carrying the un-reviewed value.
    client, settings = _client(tmp_path)
    save_artifact(_draft_with_examples(), settings.artifacts_dir)

    r = client.post(
        "/capabilities/lookup_member/versions/1/approve",
        json={"input_examples": {"operator_id": "teller1"}},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    body = client.get("/capabilities/lookup_member/versions/1").json()
    assert {p["name"]: p["example"] for p in body["input_schema"]} == {
        "member_id": "103001",      # untouched
        "operator_id": "teller1",   # corrected
    }


def test_approve_still_works_with_no_body(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_draft_with_examples(), settings.artifacts_dir)

    r = client.post("/capabilities/lookup_member/versions/1/approve")

    assert r.status_code == 200
    assert r.json()["status"] == "approved"


def test_approve_rejects_a_default_for_an_unknown_param(tmp_path):
    # Silently dropping an unrecognised name is how a reviewer ends up believing they
    # fixed something they did not - and the draft stays a draft, so nothing is half-done.
    client, settings = _client(tmp_path)
    save_artifact(_draft_with_examples(), settings.artifacts_dir)

    r = client.post(
        "/capabilities/lookup_member/versions/1/approve",
        json={"input_examples": {"operatorid": "teller1"}},
    )

    assert r.status_code == 409
    assert "operatorid" in r.json()["detail"]
    assert client.get("/capabilities/lookup_member/versions/1").json()["status"] == "draft"
