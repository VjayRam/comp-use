from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

from comp_use.escalation.controller import EscalationController
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import AllowlistViolation, Guardrail
from comp_use.llm_client import LLMClient
from comp_use.schemas import ActionType, InterventionRequest, Locator, RiskTier, Step, ValueSource
from comp_use.surface import safe_screenshot

def _optional_str(raw) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raw = str(raw)
    stripped = raw.strip()
    if stripped.lower() in ("", "none", "null"):
        return None
    return stripped


_LOCATOR_ACTIONS = {ActionType.CLICK, ActionType.TYPE_TEXT, ActionType.SELECT_OPTION, ActionType.EXTRACT}
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


_DEAD_END_THRESHOLD = 3


class DiscoveryAgent:
    def __init__(
        self,
        surface,
        llm_client: LLMClient,
        guardrail: Guardrail,
        evidence_logger: EvidenceLogger,
        max_steps: int,
        escalation: EscalationController | None = None,
        confirm_risky: bool = False,
    ):
        self.surface = surface
        self.llm_client = llm_client
        self.guardrail = guardrail
        self.evidence_logger = evidence_logger
        self.max_steps = max_steps
        self.escalation = escalation
        self.confirm_risky = confirm_risky

    def _escalate(self, goal: str, current_step: int, reason: str) -> None:
        if self.escalation is None:
            return
        self.escalation.escalate(
            InterventionRequest(
                run_id=self.evidence_logger.run_id,
                capability_or_goal=goal,
                current_step=current_step,
                screenshot_path=self.evidence_logger.save_screenshot(
                    safe_screenshot(self.surface), f"escalation_step{current_step}"
                ),
                reason=reason,
            )
        )

    def _print(self, message: str) -> None:
        # Console output isn't the evidence JSONL (which is already redacted via
        # EvidenceLogger) - without this, real typed values print to the
        # terminal in the clear, e.g. into CI logs or a shared screen recording.
        print(self.guardrail.redact(message), flush=True)

    def _note_skip(self, goal: str, step_index: int, consecutive_skips: int) -> int:
        consecutive_skips += 1
        if consecutive_skips >= _DEAD_END_THRESHOLD:
            self._escalate(goal, step_index, f"dead_end: {consecutive_skips} consecutive skipped/invalid decisions")
            consecutive_skips = 0
        return consecutive_skips

    def run(self, goal: str, start_url: str) -> RunTrace:
        trace = RunTrace(run_id=self.evidence_logger.run_id, goal=goal)
        self.surface.act(ActionType.NAVIGATE, locator=None, target=start_url, text=None)
        history: list[dict] = []
        consecutive_skips = 0
        needs_vision_fallback = False

        for step_index in range(self.max_steps):
            observed = self.surface.observe()
            screenshot_b64 = None
            if needs_vision_fallback:
                png = safe_screenshot(self.surface)
                if png is not None:
                    screenshot_b64 = base64.b64encode(png).decode("ascii")
                else:
                    self._print("[discover] WARNING: screenshot capture failed; falling back to a text-only decision this turn.")
                needs_vision_fallback = False
            try:
                decision = self.llm_client.decide_next_action(
                    goal=goal, observed_tree=observed.accessibility_tree, screenshot_b64=screenshot_b64, history=history
                )
            except Exception as exc:
                error_detail = f"{type(exc).__name__}: {exc}"
                self._print(f"[discover] LLM call failed: {error_detail}")
                self.evidence_logger.log_event(
                    "skipped_decision", {"reason": "llm_call_failed", "error": error_detail}
                )
                consecutive_skips = self._note_skip(goal, step_index, consecutive_skips)
                continue
            self.evidence_logger.log_event("decision", decision)

            if decision.get("done") or decision.get("action") == "finish":
                self._print("[discover] finish")
                trace.succeeded = True
                break

            try:
                action = ActionType(decision["action"])
            except ValueError:
                history.append({**decision, "error": f"unknown action '{decision.get('action')}'"})
                consecutive_skips = self._note_skip(goal, step_index, consecutive_skips)
                continue

            locator = _locator_from_decision(decision.get("locator"))
            target = _optional_str(decision.get("target"))
            text = _optional_str(decision.get("text"))
            value_source = _value_source_from_decision(decision.get("value_source"))
            extract_as = _optional_str(decision.get("extract_as"))

            if action in _LOCATOR_ACTIONS and locator is None:
                self._print(
                    f"[discover] skipped {action.value}: locator must look like "
                    '{"strategy":"role","value":{"role":"textbox","name":"Member ID"}} '
                    f"(model sent {decision.get('locator')!r})"
                )
                history.append({
                    **decision,
                    "error": "locator is required for click/type_text/select_option. "
                    'Example: {"strategy": "role", "value": {"role": "textbox", "name": "Member ID"}}',
                })
                self.evidence_logger.log_event("skipped_decision", {"reason": "missing_locator", "decision": decision})
                # The tree alone wasn't enough for the model to pin down an element -
                # give it a screenshot on the very next attempt for this same state.
                needs_vision_fallback = True
                consecutive_skips = self._note_skip(goal, step_index, consecutive_skips)
                continue
            if action == ActionType.NAVIGATE and not target:
                history.append({**decision, "error": "target URL is required for navigate"})
                self.evidence_logger.log_event("skipped_decision", {"reason": "missing_target", "decision": decision})
                consecutive_skips = self._note_skip(goal, step_index, consecutive_skips)
                continue

            self._print(f"[discover] {action.value} locator={locator} target={target} text={text}")
            url = target or self.surface.current_url() or start_url
            try:
                self.guardrail.check_allowlist(url, action.value)
            except AllowlistViolation as exc:
                self._print(f"[discover] skipped {action.value}: {exc}")
                history.append({**decision, "error": str(exc)})
                self.evidence_logger.log_event("skipped_decision", {"reason": "allowlist", "decision": decision})
                consecutive_skips = self._note_skip(goal, step_index, consecutive_skips)
                continue
            risk_tier = _classify_risk(action, target, locator)
            consecutive_skips = 0

            if self.guardrail.requires_confirmation(risk_tier, self.confirm_risky) and self.escalation is not None:
                self._escalate(goal, step_index, f"step {step_index} is risk_tier=risky and confirm_risky is False")

            try:
                self.surface.act(action, locator=locator, target=target, text=text)
            except Exception as exc:
                error_detail = f"{type(exc).__name__}: {exc}"
                self._print(f"[discover] action failed: {action.value} raised {error_detail}")
                history.append({**decision, "error": f"action failed: {error_detail}"})
                self.evidence_logger.log_event(
                    "skipped_decision", {"reason": "action_failed", "decision": decision, "error": error_detail}
                )
                # A locator that looked valid but didn't actually resolve is the same
                # underlying problem the vision fallback exists for - give the model a
                # screenshot on the very next attempt, same as a missing_locator skip.
                needs_vision_fallback = True
                consecutive_skips = self._note_skip(goal, step_index, consecutive_skips)
                continue

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
                    extract_as=extract_as if action == ActionType.EXTRACT else None,
                    risk_tier=risk_tier,
                )
            )
            history.append(decision)

        if not trace.succeeded:
            self._escalate(
                goal,
                len(trace.steps) - 1 if trace.steps else None,
                f"stuck: reached max_steps ({self.max_steps}) without finish",
            )

        trace.final_url = self.surface.current_url()
        return trace
