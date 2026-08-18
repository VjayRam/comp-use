from comp_use.escalation.controller import ControlState, EscalationController
from comp_use.escalation.transport import ControlTransport
from comp_use.evidence import EvidenceLogger
from comp_use.config import load_settings
from comp_use.guardrail import Guardrail
from comp_use.schemas import InterventionRequest


class FakeTransport(ControlTransport):
    def __init__(self):
        self.notified = []
        self.resumed = False

    def notify(self, request):
        self.notified.append(request)

    def wait_for_resume(self):
        self.resumed = True


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
