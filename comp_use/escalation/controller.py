import difflib
from enum import Enum

from comp_use.evidence import EvidenceLogger
from comp_use.escalation.transport import ControlTransport
from comp_use.schemas import InterventionRequest


class ControlState(str, Enum):
    AGENT = "agent"
    HUMAN = "human"
    NONE = "none"


def _diff_trees(before: str, after: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(), after.splitlines(), lineterm="", n=0,
        fromfile="before_handoff", tofile="after_handoff",
    )
    return "\n".join(diff)


class EscalationController:
    def __init__(self, evidence_logger: EvidenceLogger, transport: ControlTransport, surface=None):
        self.evidence_logger = evidence_logger
        self.transport = transport
        self.surface = surface
        self.control = ControlState.AGENT

    def escalate(self, request: InterventionRequest) -> None:
        self.control = ControlState.HUMAN
        self.evidence_logger.log_event("escalation_requested", request.model_dump())

        before_tree = None
        before_screenshot_path = None
        if self.surface is not None:
            before_tree = self.surface.observe().accessibility_tree
            before_screenshot_path = self.evidence_logger.save_screenshot(
                self.surface.screenshot(), f"escalation_before_step{request.current_step}"
            )

        self.transport.notify(request)
        human_note = self.transport.wait_for_resume()
        self.evidence_logger.log_event("escalation_resumed", {"run_id": request.run_id})

        tree_diff = None
        after_screenshot_path = None
        if self.surface is not None:
            after_tree = self.surface.observe().accessibility_tree
            tree_diff = _diff_trees(before_tree, after_tree)
            after_screenshot_path = self.evidence_logger.save_screenshot(
                self.surface.screenshot(), f"escalation_after_step{request.current_step}"
            )

        self.evidence_logger.log_event(
            "escalation_human_action",
            {
                "run_id": request.run_id,
                "note": human_note or "",
                "tree_diff": tree_diff,
                "before_screenshot_path": before_screenshot_path,
                "after_screenshot_path": after_screenshot_path,
            },
        )
        self.control = ControlState.AGENT
