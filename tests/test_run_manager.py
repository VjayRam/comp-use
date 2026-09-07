import time

from comp_use.schemas import InterventionRequest, OutcomeType, ReplayResult
from comp_use.escalation.transport import RunInterrupted
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


def test_run_manager_request_takeover_flags_the_transport_while_running():
    manager = RunManager()
    saw_takeover = {}

    def target(transport):
        # Poll like DiscoveryAgent/ReplayEngine's loop does, without a real browser.
        deadline = time.time() + 1.0
        while time.time() < deadline and not transport.takeover_requested():
            time.sleep(0.01)
        saw_takeover["value"] = transport.takeover_requested()
        return ReplayResult(outcome=OutcomeType.SUCCESS)

    run_id = manager.start("invoke", "lookup_member", target)
    assert manager.request_takeover(run_id) is True

    assert _wait_until(lambda: manager.get(run_id).status == "done")
    assert saw_takeover["value"] is True


def test_run_manager_request_takeover_returns_false_for_unknown_run_id():
    manager = RunManager()
    assert manager.request_takeover("does_not_exist") is False


def test_run_manager_request_takeover_returns_false_once_run_is_done():
    manager = RunManager()
    run_id = manager.start("invoke", "lookup_member", lambda transport: ReplayResult(outcome=OutcomeType.SUCCESS))
    assert _wait_until(lambda: manager.get(run_id).status == "done")

    assert manager.request_takeover(run_id) is False


def test_run_manager_delete_removes_a_done_run():
    manager = RunManager()
    run_id = manager.start("invoke", "lookup_member", lambda transport: ReplayResult(outcome=OutcomeType.SUCCESS))
    assert _wait_until(lambda: manager.get(run_id).status == "done")

    assert manager.delete(run_id) is True
    assert manager.get(run_id) is None


def test_run_manager_delete_returns_false_for_unknown_run_id():
    manager = RunManager()
    assert manager.delete("does_not_exist") is False


def test_run_manager_delete_refuses_an_active_run():
    manager = RunManager()

    def target(transport):
        transport.notify(InterventionRequest(run_id="ignored", capability_or_goal="lookup_member", reason="risky step"))
        note = transport.wait_for_resume()
        return ReplayResult(outcome=OutcomeType.SUCCESS, detail=note)

    run_id = manager.start("invoke", "lookup_member", target)
    assert _wait_until(lambda: manager.get(run_id).status == "escalated")

    assert manager.delete(run_id) is False
    assert manager.get(run_id) is not None  # untouched, not silently removed

    manager.resume(run_id, "done")


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


def test_run_manager_interrupt_ends_a_running_run_as_interrupted():
    manager = RunManager()

    def target(transport):
        # Polls like DiscoveryAgent/ReplayEngine's step loop does, and raises the
        # same way EscalationController.raise_if_interrupted() would.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if transport.interrupt_requested():
                raise RunInterrupted("run was interrupted by an operator")
            time.sleep(0.01)
        return ReplayResult(outcome=OutcomeType.SUCCESS)

    run_id = manager.start("invoke", "lookup_member", target)
    assert manager.interrupt(run_id) is True

    assert _wait_until(lambda: manager.get(run_id).status == "interrupted", timeout=3.0)
    record = manager.get(run_id)
    assert record.error == "stopped by an operator"
    assert record.finished_at is not None
    # Not an error, and no result recorded - the capability never reported anything.
    assert record.result is None


def test_run_manager_interrupt_unblocks_a_run_parked_on_an_escalation():
    """An escalated run is blocked in wait_for_resume(), not at a step boundary, so
    the loop's interrupt poll can never fire for it - interrupt() has to wake it."""
    manager = RunManager()

    def target(transport):
        transport.notify(
            InterventionRequest(run_id="ignored", capability_or_goal="lookup_member", reason="risky step")
        )
        transport.wait_for_resume()
        return ReplayResult(outcome=OutcomeType.SUCCESS)

    run_id = manager.start("invoke", "lookup_member", target)
    assert _wait_until(lambda: manager.get(run_id).status == "escalated")

    assert manager.interrupt(run_id) is True
    assert _wait_until(lambda: manager.get(run_id).status == "interrupted", timeout=3.0)


def test_run_manager_interrupt_returns_false_for_unknown_or_finished_runs():
    manager = RunManager()
    assert manager.interrupt("does_not_exist") is False

    run_id = manager.start("invoke", "lookup_member", lambda transport: ReplayResult(outcome=OutcomeType.SUCCESS))
    assert _wait_until(lambda: manager.get(run_id).status == "done")
    assert manager.interrupt(run_id) is False


def test_an_interrupted_run_can_then_be_deleted():
    """delete() refuses 'running'/'escalated'. 'interrupted' is terminal, so the
    record must actually be removable afterwards rather than becoming a leak."""
    manager = RunManager()

    def target(transport):
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if transport.interrupt_requested():
                raise RunInterrupted("stopped")
            time.sleep(0.01)
        return ReplayResult(outcome=OutcomeType.SUCCESS)

    run_id = manager.start("invoke", "lookup_member", target)
    manager.interrupt(run_id)
    assert _wait_until(lambda: manager.get(run_id).status == "interrupted", timeout=3.0)

    assert manager.delete(run_id) is True
    assert manager.get(run_id) is None
