import json

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
