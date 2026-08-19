from enum import Enum

from comp_use.evidence import EvidenceLogger
from comp_use.escalation.transport import ControlTransport
from comp_use.schemas import InterventionRequest


class ControlState(str, Enum):
    AGENT = "agent"
    HUMAN = "human"
    NONE = "none"


class EscalationController:
    def __init__(self, evidence_logger: EvidenceLogger, transport: ControlTransport):
        self.evidence_logger = evidence_logger
        self.transport = transport
        self.control = ControlState.AGENT

    def escalate(self, request: InterventionRequest) -> None:
        self.control = ControlState.HUMAN
        self.evidence_logger.log_event("escalation_requested", request.model_dump())
        self.transport.notify(request)
        human_note = self.transport.wait_for_resume()
        self.evidence_logger.log_event("escalation_resumed", {"run_id": request.run_id})
        self.evidence_logger.log_event(
            "escalation_human_action", {"run_id": request.run_id, "note": human_note or ""}
        )
        self.control = ControlState.AGENT
