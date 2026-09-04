import difflib
from enum import Enum

from comp_use.evidence import EvidenceLogger
from comp_use.escalation.transport import ControlTransport
from comp_use.schemas import InterventionRequest
from comp_use.surface import safe_screenshot


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


def _safe_observe_tree(surface) -> str | None:
    # Same reasoning as safe_screenshot: bringing a human into the loop is the
    # single most safety-critical path in the system and must never itself be
    # the thing that crashes, even if the page is in a broken enough state
    # that reading its own accessibility tree also fails.
    try:
        return surface.observe().accessibility_tree
    except Exception:
        return None


class EscalationController:
    def __init__(self, evidence_logger: EvidenceLogger, transport: ControlTransport, surface=None):
        self.evidence_logger = evidence_logger
        self.transport = transport
        self.surface = surface
        self.control = ControlState.AGENT

    def takeover_requested(self) -> bool:
        """Polled by DiscoveryAgent/ReplayEngine once per step - see
        ControlTransport.takeover_requested()'s docstring."""
        return self.transport.takeover_requested()

    def escalate(self, request: InterventionRequest) -> None:
        self.control = ControlState.HUMAN
        # Whatever triggered this escalate() call (a risky step, loop detection, or a
        # voluntary takeover request), any pending takeover request is now being
        # honored - clear it so it can't also fire a second, redundant escalation
        # right after this one resumes.
        self.transport.clear_takeover()
        self.evidence_logger.log_event("escalation_requested", request.model_dump())

        before_tree = None
        before_screenshot_path = None
        if self.surface is not None:
            before_tree = _safe_observe_tree(self.surface)
            before_screenshot_path = self.evidence_logger.save_screenshot(
                safe_screenshot(self.surface), f"escalation_before_step{request.current_step}"
            )

        self.transport.notify(request)
        human_note = self.transport.wait_for_resume()
        self.evidence_logger.log_event("escalation_resumed", {"run_id": request.run_id})

        tree_diff = None
        after_screenshot_path = None
        if self.surface is not None:
            after_tree = _safe_observe_tree(self.surface)
            if before_tree is not None and after_tree is not None:
                tree_diff = _diff_trees(before_tree, after_tree)
            after_screenshot_path = self.evidence_logger.save_screenshot(
                safe_screenshot(self.surface), f"escalation_after_step{request.current_step}"
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
