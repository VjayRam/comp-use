from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

from comp_use.escalation.controller import EscalationController
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import AllowlistViolation, Guardrail, is_sensitive_param_name
from comp_use.llm_client import LLMClient
from comp_use.schemas import ActionType, InterventionRequest, Locator, RiskTier, Step, ValueSource
from comp_use.surface import safe_screenshot
from comp_use.tokenizer import SensitiveValueTokenizer


class _EscalationTransportFailed(Exception):
    """Raised by DiscoveryAgent._escalate() when the transport itself fails (e.g. a
    real, live-observed RuntimeError: LocalSharedBrowserTransport requires an
    interactive terminal and raises when stdin is non-interactive). Never left to
    surface as a raw traceback - caught in run(), which treats it as a hard stop:
    there is no way to safely get human input at that point, so the run cannot
    safely continue."""


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
# Match the control that actually commits an irreversible change (a "Confirm ...",
# "Post ..." button), not navigation toward it - a link that just opens a form isn't
# risky. "post" was added after live testing against MERIDIAN CORE showed its own
# commit buttons are named "Post Transfer"/"Post Hold", which the original
# confirm/delete-only list never matched - see EXT_TASK_FIXES.md #1.
_RISKY_TARGET_HINTS = ("confirm", "delete", "post")
# Second, broader signal: any click/navigate while sitting on a URL whose path
# contains "review" is treated as risky regardless of the control's own label. Every
# review->post flow observed on MERIDIAN CORE (transfer, hold, open-share) reaches a
# `.../review`-style URL immediately before the irreversible commit action - and one
# of those commit buttons ("Open Share") matches no keyword at all. This is
# deliberately over-conservative: it will also flag a "Cancel" link on the same review
# page as risky (an unnecessary escalation), which is the safe failure direction,
# unlike the alternative of silently missing a real commit button's exact wording.
_RISKY_URL_HINTS = ("review",)


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
    # Side-channel, never persisted onto a Step: the literal discovery-time value used
    # for each goal_parameter, keyed by param_name - compile_artifact() surfaces this
    # as InputParam.example. Deliberately excludes credential-shaped param names (see
    # the is_sensitive_param_name() check where this is populated).
    parameter_examples: dict[str, str] = field(default_factory=dict)


def _classify_risk(action: ActionType, target: str | None, locator, current_url: str | None = None) -> RiskTier:
    haystack = " ".join(filter(None, [target, str(locator.value) if locator else ""])).lower()
    if action in (ActionType.CLICK, ActionType.NAVIGATE):
        if any(h in haystack for h in _RISKY_TARGET_HINTS):
            return RiskTier.RISKY
        if current_url and any(h in current_url.lower() for h in _RISKY_URL_HINTS):
            return RiskTier.RISKY
    return RiskTier.SAFE


_DEAD_END_THRESHOLD = 3
# How many times the exact same (url, action, locator) can be attempted before it's
# treated as a loop rather than legitimate retries. Deliberately separate from
# _DEAD_END_THRESHOLD: that one only counts INVALID/skipped decisions, so it never
# fires when the model keeps making VALID-looking decisions that just don't make
# progress - exactly what was observed live against MERIDIAN CORE's Place Hold flow
# (fill form -> Continue -> fail -> "Return to previous screen" -> refill -> Continue
# -> fail again), which cycled for the full step budget without ever tripping the
# existing dead-end check. See EXT_TASK_FIXES.md's loop-detection discussion.
_LOOP_THRESHOLD = 3


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
        # Tokenized independently of Guardrail.redact() (one-way, log/console only):
        # this is two-way so the LLM only ever reasons over placeholder tokens for
        # account/transaction/confirmation IDs and dollar amounts, while everything
        # that actually touches the browser or gets persisted uses real values.
        self.tokenizer = SensitiveValueTokenizer(guardrail.settings.redaction_patterns)

    def _escalate(self, goal: str, current_step: int, reason: str) -> None:
        if self.escalation is None:
            return
        try:
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
        except Exception as exc:
            # A transport failure (e.g. non-interactive stdin) must never surface as a
            # raw traceback - convert it into a controlled abort instead. See
            # _EscalationTransportFailed's docstring for why run() treats this as a
            # hard stop rather than a skip-and-continue.
            error_detail = f"{type(exc).__name__}: {exc}"
            self._print(f"[discover] ESCALATION TRANSPORT FAILED: {error_detail}")
            self.evidence_logger.log_event(
                "escalation_transport_failed", {"reason": reason, "error": error_detail}
            )
            raise _EscalationTransportFailed(error_detail) from exc

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

    def run(self, goal: str, start_url: str, param_hints: list[str] | None = None) -> RunTrace:
        try:
            return self._run_loop(goal, start_url, param_hints=param_hints)
        except _EscalationTransportFailed:
            # The escalation transport itself failed (e.g. non-interactive stdin) -
            # already logged inside _escalate(). There's no way to safely get human
            # input at that point, so the run cannot safely continue; report it the
            # same way any other "stuck, never reached finish" run is reported
            # (succeeded=False, steps=[]) rather than letting the exception surface
            # as a raw traceback all the way out of run().
            return RunTrace(run_id=self.evidence_logger.run_id, goal=goal, final_url=self.surface.current_url())

    def _run_loop(self, goal: str, start_url: str, param_hints: list[str] | None = None) -> RunTrace:
        trace = RunTrace(run_id=self.evidence_logger.run_id, goal=goal)
        # goal_for_llm augments the prompt with an explicit, operator-declared list of
        # values that MUST become goal_parameters if the agent interacts with them -
        # trace.goal (and hence the artifact's description) stays the clean, original
        # goal text; this augmented version is only ever used in the LLM prompt below.
        # This exists because under-parameterization is a judgment call the model can
        # get wrong even with a clearly-phrased goal (observed live - see
        # EXT_TASK_FIXES.md) - a hint removes the ambiguity for values the operator
        # already knows should vary, without constraining the model from *also*
        # parameterizing something it independently judges should vary.
        goal_for_llm = goal
        if param_hints:
            goal_for_llm = (
                f"{goal}\n\n(Operator-declared parameters - if you type or select a "
                f"value for any of these concepts, you MUST use value_source "
                f"type=goal_parameter, not a literal: {', '.join(param_hints)}.)"
            )
        self.surface.act(ActionType.NAVIGATE, locator=None, target=start_url, text=None)
        history: list[dict] = []
        consecutive_skips = 0
        needs_vision_fallback = False
        # Best-effort completeness signal, not per-page-precise tracking: if a
        # dropdown was visible on ANY page seen this run but the trace never actually
        # performed a select_option, a capability may have silently shipped with a
        # field left at its default (e.g. Place Hold's share/reason-code dropdowns -
        # see EXT_TASK_FIXES.md #5). This can't tell you WHICH dropdown was skipped or
        # whether skipping it was actually fine for this goal - it's a visibility net,
        # not a hard gate, so it never blocks or fails a run.
        saw_combobox = False
        used_select_option = False
        # Loop detection: counts every exact (url, action, locator) signature seen
        # across the WHOLE run, not a sliding window - simple, and sufficient to catch
        # a repeating cycle of several steps too, since each component of the cycle
        # accumulates its own count every time the cycle repeats (no need to detect
        # the cycle itself as a unit).
        loop_signature_counts: dict[tuple, int] = {}

        for step_index in range(self.max_steps):
            if self.escalation is not None and self.escalation.takeover_requested():
                # A human clicked "Take control" on the dashboard - pause here rather
                # than mid-decision, before spending an LLM call on a step that may
                # never get performed. Same escalate()/resume() handshake as any other
                # escalation, so the SAME "Resume" flow hands control back.
                self._escalate(goal, step_index, "manual takeover requested by operator")
            observed = self.surface.observe()
            if not saw_combobox and "combobox" in observed.accessibility_tree.lower():
                saw_combobox = True
            screenshot_b64 = None
            if needs_vision_fallback:
                png = safe_screenshot(self.surface)
                if png is not None:
                    screenshot_b64 = base64.b64encode(png).decode("ascii")
                else:
                    self._print("[discover] WARNING: screenshot capture failed; falling back to a text-only decision this turn.")
                needs_vision_fallback = False
            # The LLM only ever sees a tokenized tree - real account/transaction/
            # confirmation IDs and dollar amounts never leave the machine on this path.
            # Screenshots (vision fallback) are NOT covered by this - a raw screenshot
            # sent to a vision model remains a documented, separate exposure.
            tokenized_tree = self.tokenizer.tokenize(observed.accessibility_tree)
            try:
                decision = self.llm_client.decide_next_action(
                    goal=goal_for_llm, observed_tree=tokenized_tree, screenshot_b64=screenshot_b64, history=history
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
                if saw_combobox and not used_select_option:
                    self._print(
                        "[discover] WARNING: a dropdown (combobox) was visible at some point "
                        "during this run but no select_option action was ever performed - a "
                        "field may have been silently left at its default. Review the "
                        "compiled artifact before approving it."
                    )
                    self.evidence_logger.log_event(
                        "possible_incomplete_capability",
                        {"reason": "combobox_seen_but_never_selected"},
                    )
                break

            try:
                action = ActionType(decision["action"])
            except ValueError:
                history.append({**decision, "error": f"unknown action '{decision.get('action')}'"})
                consecutive_skips = self._note_skip(goal, step_index, consecutive_skips)
                continue

            # Detokenize here, at the single boundary between "what the LLM decided"
            # and "what actually drives the browser" - everything downstream of this
            # point (surface.act, risk classification, the persisted artifact) sees
            # real values again, same as if tokenization never happened.
            locator = _locator_from_decision(self.tokenizer.detokenize_value(decision.get("locator")))
            target = _optional_str(self.tokenizer.detokenize(_optional_str(decision.get("target"))))
            text = _optional_str(self.tokenizer.detokenize(_optional_str(decision.get("text"))))
            raw_value_source = decision.get("value_source")
            value_source = _value_source_from_decision(raw_value_source)
            extract_as = _optional_str(decision.get("extract_as"))

            if raw_value_source and value_source is None:
                # The model sent SOMETHING for value_source, but it didn't parse into a
                # valid ValueSource (typo'd type, missing param_name, ...). Treating this
                # as "no value_source" would silently bake this turn's literal discovery-time
                # text (e.g. a real member ID or dollar amount) into the artifact instead of
                # binding it to a replay-time parameter - skip and let the model retry instead.
                self._print(
                    f"[discover] skipped {action.value}: value_source was present but invalid "
                    f"(model sent {raw_value_source!r})"
                )
                history.append({
                    **decision,
                    "error": "value_source, if present, must look like "
                    '{"type": "goal_parameter", "param_name": "member_id", "param_type": "string"} '
                    '- omit value_source entirely (leave it null) for a value that never varies.',
                })
                self.evidence_logger.log_event("skipped_decision", {"reason": "invalid_value_source", "decision": decision})
                consecutive_skips = self._note_skip(goal, step_index, consecutive_skips)
                continue

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
            consecutive_skips = 0

            loop_signature = (url, action.value, locator.model_dump_json() if locator else None)
            loop_signature_counts[loop_signature] = loop_signature_counts.get(loop_signature, 0) + 1
            if loop_signature_counts[loop_signature] >= _LOOP_THRESHOLD:
                self._print(
                    f"[discover] LOOP DETECTED: {action.value} on {locator} at {url} "
                    f"attempted {loop_signature_counts[loop_signature]} times without progress"
                )
                self.evidence_logger.log_event(
                    "loop_detected",
                    {"url": url, "action": action.value, "count": loop_signature_counts[loop_signature]},
                )
                self._escalate(
                    goal, step_index,
                    f"loop detected: {action.value} on this control at {url} was attempted "
                    f"{loop_signature_counts[loop_signature]} times without making progress",
                )
                # Give the human's intervention a chance to actually change something -
                # reset this signature's count rather than re-escalating on every single
                # subsequent turn if the model tries the exact same thing again anyway.
                loop_signature_counts[loop_signature] = 0
                continue

            risk_tier = _classify_risk(action, target, locator, current_url=self.surface.current_url())

            if self.guardrail.requires_confirmation(risk_tier, self.confirm_risky) and self.escalation is not None:
                self._escalate(goal, step_index, f"step {step_index} is risk_tier=risky and confirm_risky is False")

            try:
                self.surface.act(action, locator=locator, target=target, text=text)
                if action == ActionType.SELECT_OPTION:
                    used_select_option = True
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

            # The action that just ran (most concretely a CLICK) may have navigated the
            # browser somewhere new. The allowlist check above only validated where we
            # were BEFORE the action - a click that navigates off-allowlist was never
            # checked at all. Re-validate the CURRENT url now, every time. Unlike the
            # pre-check (which can just skip the decision and let the model retry),
            # this one can't be undone - the browser has already left the allowlisted
            # domain - so it's treated as a hard stop for the whole run, not a skip.
            try:
                self.guardrail.check_allowlist(self.surface.current_url(), action.value)
            except AllowlistViolation as exc:
                self._print(f"[discover] ABORTING: {action.value} navigated outside the allowlist: {exc}")
                self.evidence_logger.log_event(
                    "allowlist_violation_post_action",
                    {"action": action.value, "url": self.surface.current_url(), "error": str(exc)},
                )
                self._escalate(goal, step_index, f"post-action allowlist violation: {exc}")
                trace.final_url = self.surface.current_url()
                return trace

            # Only persist the literal text for steps that AREN'T a goal_parameter -
            # a goal_parameter's discovery-time example (a real member ID, account
            # number, amount, ...) has no business being baked into Step.value; replay
            # always substitutes the caller's own params for those anyway. ValueSource
            # now only ever means "goal_parameter" (see schemas.py) - any non-None
            # value_source is one.
            is_goal_parameter = value_source is not None
            recorded_value = (
                text
                if action in (ActionType.TYPE_TEXT, ActionType.SELECT_OPTION) and not is_goal_parameter
                else None
            )
            # The discovery-time literal DOES get kept, but off to the side in
            # trace.parameter_examples rather than in the artifact's Step - purely so
            # compile_artifact() can surface it as InputParam.example (a real,
            # concrete example value for a reviewer/calling agent - see §3.2's
            # "reviewable" requirement), never as something replay itself reads.
            # First occurrence wins (matches compile_artifact()'s own dedup-by-name).
            # Credential-shaped params (password, token, ...) are deliberately excluded
            # - a real password typed during discovery has no business persisted to
            # an artifact JSON on disk, even as an "example".
            if (
                is_goal_parameter
                and text is not None
                and action in (ActionType.TYPE_TEXT, ActionType.SELECT_OPTION)
                and value_source.param_name not in trace.parameter_examples
                and not is_sensitive_param_name(value_source.param_name)
            ):
                trace.parameter_examples[value_source.param_name] = text
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
