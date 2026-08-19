from __future__ import annotations

import json
from dataclasses import dataclass, field

from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import AllowlistViolation, Guardrail
from comp_use.llm_client import LLMClient
from comp_use.schemas import ActionType, Locator, RiskTier, Step, ValueSource

def _optional_str(raw) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raw = str(raw)
    stripped = raw.strip()
    if stripped.lower() in ("", "none", "null"):
        return None
    return stripped


_LOCATOR_ACTIONS = {ActionType.CLICK, ActionType.TYPE_TEXT, ActionType.SELECT_OPTION}
# Match the control that actually commits an irreversible change (a "Confirm ..."
# button), not navigation toward it - a link that just opens a form isn't risky.
_RISKY_TARGET_HINTS = ("confirm", "delete")


def _locator_from_decision(raw) -> Locator | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict) or not raw.get("strategy") or not raw.get("value"):
        return None
    try:
        return Locator.model_validate(raw)
    except Exception:
        return None


def _value_source_from_decision(raw) -> ValueSource | None:
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict):
        return None
    try:
        return ValueSource.model_validate(raw)
    except Exception:
        return None


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
                print("[discover] finish", flush=True)
                trace.succeeded = True
                break

            try:
                action = ActionType(decision["action"])
            except ValueError:
                history.append({**decision, "error": f"unknown action '{decision.get('action')}'"})
                continue

            locator = _locator_from_decision(decision.get("locator"))
            target = _optional_str(decision.get("target"))
            text = _optional_str(decision.get("text"))
            value_source = _value_source_from_decision(decision.get("value_source"))

            if action in _LOCATOR_ACTIONS and locator is None:
                print(
                    f"[discover] skipped {action.value}: locator must look like "
                    '{"strategy":"role","value":{"role":"textbox","name":"Member ID"}} '
                    f"(model sent {decision.get('locator')!r})",
                    flush=True,
                )
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

            print(f"[discover] {action.value} locator={locator} target={target} text={text}", flush=True)
            url = target or self.surface.current_url() or start_url
            try:
                self.guardrail.check_allowlist(url, action.value)
            except AllowlistViolation as exc:
                print(f"[discover] skipped {action.value}: {exc}", flush=True)
                history.append({**decision, "error": str(exc)})
                self.evidence_logger.log_event("skipped_decision", {"reason": "allowlist", "decision": decision})
                continue
            risk_tier = _classify_risk(action, target, locator)

            self.surface.act(action, locator=locator, target=target, text=text)
            # Only persist the literal text for steps that AREN'T a goal_parameter -
            # a goal_parameter's discovery-time example (a real member ID, account
            # number, amount, ...) has no business being baked into the artifact;
            # replay always substitutes the caller's own params for those anyway.
            is_goal_parameter = value_source is not None and value_source.type == "goal_parameter"
            recorded_value = (
                text
                if action in (ActionType.TYPE_TEXT, ActionType.SELECT_OPTION) and not is_goal_parameter
                else None
            )
            trace.steps.append(
                Step(
                    action=action,
                    target=target,
                    locator=locator,
                    value_source=value_source,
                    value=recorded_value,
                    risk_tier=risk_tier,
                )
            )
            history.append(decision)

        trace.final_url = self.surface.current_url()
        return trace
