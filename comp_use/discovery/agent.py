from __future__ import annotations

from dataclasses import dataclass, field

from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.llm_client import LLMClient
from comp_use.schemas import ActionType, Locator, RiskTier, Step, ValueSource

_LOCATOR_ACTIONS = {ActionType.CLICK, ActionType.TYPE_TEXT, ActionType.SELECT_OPTION}
_RISKY_TARGET_HINTS = ("transfer", "sub-account", "delete")


@dataclass
class RunTrace:
    run_id: str
    goal: str
    steps: list[Step] = field(default_factory=list)
    final_url: str = ""
    succeeded: bool = False


def _classify_risk(action: ActionType, target: str | None, locator) -> RiskTier:
    haystack = " ".join(filter(None, [target, str(locator.value) if locator else ""])).lower()
    if action in (ActionType.CLICK, ActionType.NAVIGATE) and any(h in haystack for h in _RISKY_TARGET_HINTS):
        return RiskTier.RISKY
    return RiskTier.SAFE


class DiscoveryAgent:
    def __init__(self, surface, llm_client: LLMClient, guardrail: Guardrail, evidence_logger: EvidenceLogger, max_steps: int):
        self.surface = surface
        self.llm_client = llm_client
        self.guardrail = guardrail
        self.evidence_logger = evidence_logger
        self.max_steps = max_steps

    def run(self, goal: str, start_url: str) -> RunTrace:
        trace = RunTrace(run_id=self.evidence_logger.run_id, goal=goal)
        self.surface.act(ActionType.NAVIGATE, locator=None, target=start_url, text=None)
        history: list[dict] = []

        for _ in range(self.max_steps):
            observed = self.surface.observe()
            decision = self.llm_client.decide_next_action(
                goal=goal, observed_tree=observed.accessibility_tree, screenshot_b64=None, history=history
            )
            self.evidence_logger.log_event("decision", decision)

            if decision.get("done") or decision.get("action") == "finish":
                trace.succeeded = True
                break

            try:
                action = ActionType(decision["action"])
            except ValueError:
                history.append({**decision, "error": f"unknown action '{decision.get('action')}'"})
                continue

            locator = Locator.model_validate(decision["locator"]) if decision.get("locator") else None
            target = decision.get("target")
            text = decision.get("text")
            value_source = ValueSource.model_validate(decision["value_source"]) if decision.get("value_source") else None

            if action in _LOCATOR_ACTIONS and locator is None:
                history.append({
                    **decision,
                    "error": "locator is required for click/type_text/select_option. "
                    'Example: {"strategy": "role", "value": {"role": "textbox", "name": "Member ID"}}',
                })
                self.evidence_logger.log_event("skipped_decision", {"reason": "missing_locator", "decision": decision})
                continue
            if action == ActionType.NAVIGATE and not target:
                history.append({**decision, "error": "target URL is required for navigate"})
                self.evidence_logger.log_event("skipped_decision", {"reason": "missing_target", "decision": decision})
                continue

            self.guardrail.check_allowlist(target or self.surface.current_url(), action.value)
            risk_tier = _classify_risk(action, target, locator)

            self.surface.act(action, locator=locator, target=target, text=text)
            trace.steps.append(
                Step(action=action, target=target, locator=locator, value_source=value_source, risk_tier=risk_tier)
            )
            history.append(decision)

        trace.final_url = self.surface.current_url()
        return trace
