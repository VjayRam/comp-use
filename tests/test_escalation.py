import json
from pathlib import Path

from comp_use.escalation.controller import ControlState, EscalationController
from comp_use.escalation.transport import ControlTransport
from comp_use.evidence import EvidenceLogger
from comp_use.config import load_settings
from comp_use.guardrail import Guardrail
from comp_use.schemas import InterventionRequest


class FakeTransport(ControlTransport):
    def __init__(self, human_note: str = "reviewed and approved the transfer"):
        self.notified = []
        self.resumed = False
        self.human_note = human_note

    def notify(self, request):
        self.notified.append(request)

    def wait_for_resume(self):
        self.resumed = True
        return self.human_note


class FakeSurfaceForDiff:
    def __init__(self, trees):
        self.trees = trees
        self.calls = 0

    def observe(self):
        from comp_use.surface import ObservedState
        tree = self.trees[min(self.calls, len(self.trees) - 1)]
        self.calls += 1
        return ObservedState(accessibility_tree=tree, url="http://localhost:5000/x")

    def screenshot(self):
        return b"fakepng"


def test_escalate_transitions_control_and_resumes(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="esc_run")
    transport = FakeTransport()
    controller = EscalationController(evidence, transport)

    assert controller.control == ControlState.AGENT

    request = InterventionRequest(
        run_id="esc_run", capability_or_goal="transfer funds",
        current_step=2, screenshot_path=None, reason="risky action needs confirmation",
    )
    controller.escalate(request)

    assert transport.notified == [request]
    assert transport.resumed is True
    assert controller.control == ControlState.AGENT


def test_escalate_logs_what_the_human_did(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="esc_run_note")
    transport = FakeTransport(human_note="confirmed the transfer manually")
    controller = EscalationController(evidence, transport)

    request = InterventionRequest(
        run_id="esc_run_note", capability_or_goal="transfer funds",
        current_step=2, screenshot_path=None, reason="risky action needs confirmation",
    )
    controller.escalate(request)

    log_path = evidence.run_dir / "log.jsonl"
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    human_action_events = [e for e in events if e["event_type"] == "escalation_human_action"]
    assert len(human_action_events) == 1
    assert human_action_events[0]["data"]["note"] == "confirmed the transfer manually"
    assert human_action_events[0]["data"]["run_id"] == "esc_run_note"
    # no surface was provided - diffing is opt-in, not required
    assert human_action_events[0]["data"]["tree_diff"] is None


def test_escalate_logs_accessibility_tree_diff_when_surface_provided(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="esc_run_diff")
    transport = FakeTransport(human_note="clicked confirm manually")
    surface = FakeSurfaceForDiff([
        "heading Confirm\nbutton Confirm Transfer",
        "heading Confirmation\ntext Transaction ID: TXN-000001",
    ])
    controller = EscalationController(evidence, transport, surface=surface)

    request = InterventionRequest(
        run_id="esc_run_diff", capability_or_goal="transfer funds",
        current_step=5, screenshot_path=None, reason="risky action needs confirmation",
    )
    controller.escalate(request)

    log_path = evidence.run_dir / "log.jsonl"
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    human_action_events = [e for e in events if e["event_type"] == "escalation_human_action"]
    assert len(human_action_events) == 1
    data = human_action_events[0]["data"]
    diff = data["tree_diff"]
    assert diff is not None
    # the diff is redacted the same as everything else logged - TXN-000001 matches
    # our own redaction_patterns, so it must not appear literally, but the line it
    # was on (and the changed "Confirm Transfer" line) must still show up as a diff.
    assert "TXN-000001" not in diff
    assert "[REDACTED]" in diff
    assert "Confirm Transfer" in diff
    assert surface.calls == 2
    assert data["before_screenshot_path"] is not None
    assert data["after_screenshot_path"] is not None
    assert Path(data["before_screenshot_path"]).exists()
    assert Path(data["after_screenshot_path"]).exists()
