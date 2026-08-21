import time

from comp_use.schemas import InterventionRequest, OutcomeType, ReplayResult
from comp_use.server.run_manager import RunManager


def _wait_until(predicate, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_run_manager_completes_a_run_and_stores_the_result():
    manager = RunManager()
    run_id = manager.start("invoke", "lookup_member", lambda transport: ReplayResult(outcome=OutcomeType.SUCCESS))

    assert _wait_until(lambda: manager.get(run_id).status == "done")
    record = manager.get(run_id)
    assert record.kind == "invoke"
    assert record.result.outcome == OutcomeType.SUCCESS


def test_run_manager_stores_discover_result_under_discover_result_not_result():
    manager = RunManager()
    run_id = manager.start("discover", "lookup_member", lambda transport: {"succeeded": True, "artifact_version": 3})

    assert _wait_until(lambda: manager.get(run_id).status == "done")
    record = manager.get(run_id)
    assert record.discover_result == {"succeeded": True, "artifact_version": 3}
    assert record.result is None


def test_run_manager_escalates_then_resumes_to_completion():
    manager = RunManager()

    def target(transport):
        transport.notify(InterventionRequest(run_id="ignored", capability_or_goal="lookup_member", reason="risky step"))
        note = transport.wait_for_resume()
        return ReplayResult(outcome=OutcomeType.SUCCESS, detail=note)

    run_id = manager.start("invoke", "lookup_member", target)

    assert _wait_until(lambda: manager.get(run_id).status == "escalated")
    assert manager.get(run_id).escalation.reason == "risky step"

    assert manager.resume(run_id, "handled it via the API") is True

    assert _wait_until(lambda: manager.get(run_id).status == "done")
    assert manager.get(run_id).result.detail == "handled it via the API"


def test_run_manager_records_error_on_unhandled_exception_without_crashing():
    manager = RunManager()

    def target(transport):
        raise RuntimeError("boom")

    run_id = manager.start("invoke", "lookup_member", target)

    assert _wait_until(lambda: manager.get(run_id).status == "error")
    assert "boom" in manager.get(run_id).error


def test_run_manager_resume_returns_false_when_not_currently_escalated():
    manager = RunManager()
    run_id = manager.start("invoke", "lookup_member", lambda transport: ReplayResult(outcome=OutcomeType.SUCCESS))

    assert manager.resume(run_id, "too early") is False


def test_run_manager_get_returns_none_for_unknown_run_id():
    manager = RunManager()
    assert manager.get("does_not_exist") is None


def test_run_manager_concurrent_runs_get_distinct_ids_and_dont_share_state():
    manager = RunManager()
    run_id_a = manager.start("invoke", "lookup_member", lambda t: ReplayResult(outcome=OutcomeType.SUCCESS, detail="a"))
    run_id_b = manager.start("invoke", "transfer_funds", lambda t: ReplayResult(outcome=OutcomeType.SUCCESS, detail="b"))

    assert run_id_a != run_id_b
    assert _wait_until(lambda: manager.get(run_id_a).status == "done" and manager.get(run_id_b).status == "done")
    assert manager.get(run_id_a).result.detail == "a"
    assert manager.get(run_id_b).result.detail == "b"
