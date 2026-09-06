from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from urllib.parse import urlparse

from comp_use.discovery.outcome_library import outcome_patterns_for_target
from comp_use.escalation.controller import EscalationController
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import AllowlistViolation, Guardrail, is_sensitive_param_name
from comp_use.llm_client import LLMClient
from comp_use.schemas import (
    ActionType,
    DerivedOutputSpec,
    InterventionRequest,
    Locator,
    LocatorStrategy,
    OutcomeType,
    OutputParam,
    RiskTier,
    Step,
    ValueSource,
)
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
_WHOLE_PAGE_SELECTORS = {"body", "html", "*", ":root"}

# How much of the extracted value a text/name anchor may reproduce before it counts as
# being keyed on the value rather than labelling it. 0.8 keeps "Signed on as" (12 chars)
# as a valid anchor for "Signed on as J. TELLER (TELLER)" (30) while still catching an
# anchor that restates the whole thing.
_VALUE_KEYED_RATIO = 0.8

# How many times one run may be refused an extract locator before its judgement is
# accepted as final. Two attempts is enough to correct an obvious mistake; more just
# burns turns arguing with a model that has run out of ideas.
_MAX_EXTRACT_REJECTIONS = 2


def _unusable_extract_reason(locator: Locator | None, extracted: str | None) -> str | None:
    """Why this extract locator won't survive to the next run, or None if it will.

    Both failures below record perfectly at discovery time and only bite on first
    real use, which is the worst possible shape for a bug - hence checking here,
    while the model is still around to be asked for a better locator.
    """
    if locator is None:
        return None
    value = locator.value or {}
    if locator.strategy == LocatorStrategy.CSS:
        if str(value.get("css", "")).strip().lower() in _WHOLE_PAGE_SELECTORS:
            return (
                "it captures the whole page rather than the specific value or table the "
                "goal asked to report"
            )
        return None
    # A text/name locator that RESTATES what was read is keyed on the value itself -
    # "CN480332" locates this run's confirmation number and nothing else.
    #
    # But a short leading label is the opposite: it is exactly the stable anchor this
    # check wants people to use, and it is legitimately a substring of what was read.
    # "Signed on as" locating "Signed on as J. TELLER (TELLER)" is correct and must
    # pass - rejecting it (as a plain `anchor in captured` test did) left discovery
    # oscillating between the value-keyed locator it was refused and table selectors
    # that matched nothing, because the right answer had been ruled out.
    #
    # So the test is how much of the value the anchor reproduces, not whether it
    # appears at all: an anchor that is most of the captured text is the value; an
    # anchor that is a small part of it is a label.
    anchor = str(value.get("text") or value.get("name") or "").strip()
    captured = " ".join((extracted or "").split())
    if anchor and captured and len(anchor) >= _VALUE_KEYED_RATIO * len(captured):
        return (
            f"it is keyed on the value it just read ({anchor[:60]!r}), which is different "
            "on every run, so it will match nothing next time"
        )
    return None


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
    # Populated when the model declares derive_as/derive_op on an EXTRACT decision -
    # e.g. the goal asks for a "total" a page never shows directly, only individual
    # line items. compile_artifact() merges these into the artifact's output_schema
    # as real OutputParam(derive=...) entries, so replay computes the value
    # deterministically (ReplayEngine._resolve_derived_outputs, no LLM call) using
    # the exact same mechanism discovery is declaring here - never a number the model
    # itself computed or guessed at discovery time.
    derived_outputs: list[OutputParam] = field(default_factory=list)
    # Every decide_next_action call attempted this run, success or failure - a real
    # LLM API call/cost regardless of outcome. token_usage accumulates whatever the
    # provider reported per successful call (LLMClient.last_usage) - zero for any
    # call a provider didn't report usage for (e.g. FakeLLMClient in tests), never
    # estimated locally.
    tool_call_count: int = 0
    token_usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})


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
        # The same host-wide error taxonomy replay uses. Discovery previously had none
        # at all: it drove on through injected faults and natural rejections as if the
        # error page were just an unfamiliar screen, and whatever the model did next got
        # recorded as canonical steps - an early meridian_check_balance recording
        # contained a failed sign-on AND its retry that way. Recognising the state is
        # what lets discovery stop recording, recover, or hand to a human instead.
        outcome_patterns = outcome_patterns_for_target({"app": urlparse(start_url).netloc})
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
        asked_to_extract = False
        asked_for_missing_params = False
        extract_rejections = 0
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
            trace.tool_call_count += 1
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

            # Surfaced per-call on the dashboard's run log, not just accumulated at
            # the end - lets a reviewer see cost accrue turn by turn, not just a
            # final total once the run is already over. None of these numbers are
            # estimated locally; they're exactly what the provider's own response
            # reported (LLMClient.last_usage), so a provider that omits usage (or
            # FakeLLMClient in tests) simply logs nothing here.
            usage = getattr(self.llm_client, "last_usage", None)
            if usage:
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    trace.token_usage[key] = trace.token_usage.get(key, 0) + (usage.get(key) or 0)
                self.evidence_logger.log_event(
                    "llm_usage",
                    {
                        "call_number": trace.tool_call_count,
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                        "total_tokens": usage.get("total_tokens"),
                        "running_total_tokens": trace.token_usage["total_tokens"],
                    },
                )

            if decision.get("done") or decision.get("action") == "finish":
                self._print("[discover] finish")
                trace.succeeded = True
                # Once an irreversible step has run, a finish is FINAL. Neither gate
                # below may send the agent round again, because "round again" means
                # back through a flow that has already committed - live-observed on a
                # funds transfer that had posted (confirmation CN480346 in hand) and
                # was pushed back for a missing memo: it returned to the member record,
                # reopened Funds Transfer and began selecting shares for a SECOND
                # transfer, stopping only because it ran out of steps. An incomplete
                # recording is a bad artifact; a duplicated financial transaction is a
                # different category of problem entirely. Warn, record, and stop.
                committed = any(s.risk_tier == RiskTier.RISKY for s in trace.steps)
                if committed:
                    self.evidence_logger.log_event(
                        "finish_accepted_after_commit",
                        {"reason": "an irreversible step already ran; gates cannot re-run the flow"},
                    )
                # param_hints names the inputs this capability is supposed to accept.
                # A hint that never became a real parameter means the flow never
                # entered that value - live-observed on an Update Member Information
                # recording that opened the pre-filled form and went straight to "Save
                # Changes", capturing no e-mail, phone or address: it submitted the
                # record unchanged and still reported MEMBER INFORMATION UPDATED. A
                # capability that silently does nothing is worse than one that fails,
                # so ask once before accepting the finish.
                captured = {
                    s.value_source.param_name for s in trace.steps if s.value_source is not None
                }
                missing = [h for h in (param_hints or []) if h not in captured]
                if missing and not asked_for_missing_params and not committed:
                    asked_for_missing_params = True
                    self._print(f"[discover] finish rejected once: never entered {', '.join(missing)}")
                    self.evidence_logger.log_event(
                        "finish_rejected_missing_params", {"missing": missing}
                    )
                    history.append({
                        **decision,
                        "error": f"you never entered a value for: {', '.join(missing)}. The goal says "
                        "these vary per call, so this capability would submit the form with whatever "
                        "was already in those fields and change nothing. Go back, type or select each "
                        "of them (a pre-filled field still has to be set), then submit and finish.",
                    })
                    trace.succeeded = False
                    continue
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
                if (
                    not any(s.action == ActionType.EXTRACT for s in trace.steps)
                    and not asked_to_extract
                    and not committed
                ):
                    # A capability that never extracts anything compiles to an empty
                    # output_schema, and replay then reports "success" with no result
                    # at all - live-observed on three read-only capabilities whose
                    # entire purpose was the answer they returned nothing of ("check a
                    # member's balance" reporting no balance).
                    #
                    # Asked once, not enforced: plenty of goals legitimately have
                    # nothing to report ("sign on"), and only the model can tell those
                    # apart from stopping one step short. If it finishes again the
                    # answer is taken at face value, so this can push back but never
                    # trap the run.
                    asked_to_extract = True
                    self._print("[discover] finish rejected once: nothing was extracted yet")
                    self.evidence_logger.log_event(
                        "finish_rejected_no_extract", {"step_index": step_index}
                    )
                    history.append(
                        {
                            **decision,
                            "error": "You have not extracted anything, so this capability would "
                            "return no result to its caller. If the goal asks to find, read, check, "
                            "view, search for or report ANY information, extract it now with an "
                            "`extract` action and a descriptive `extract_as` name. Only finish "
                            "again if the goal genuinely has nothing to report back.",
                        }
                    )
                    trace.succeeded = False
                    continue
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
            derive_as = _optional_str(decision.get("derive_as"))
            derive_op = _optional_str(decision.get("derive_op"))

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
                # Naming the ACTUAL defect matters: a decision carrying
                # {"strategy":"text","value":{}} was being told "locator is required",
                # which is not what was wrong with it - so the model resent the same
                # empty-valued locator until the run dead-ended on consecutive skips.
                history.append({
                    **decision,
                    "error": f"the locator you sent ({decision.get('locator')!r}) is not usable for "
                    f"{action.value}: it must have a strategy AND a non-empty value carrying that "
                    'strategy\'s key - {"strategy":"role","value":{"role":"textbox","name":"Member ID"}}, '
                    '{"strategy":"text","value":{"text":"Signed on as"}}, or '
                    '{"strategy":"css","value":{"css":"td.balance"}}. Send a different locator; '
                    "resending this one will fail the same way.",
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

            escalated_for_this_step = False
            if self.guardrail.requires_confirmation(risk_tier, self.confirm_risky) and self.escalation is not None:
                self._escalate(goal, step_index, f"step {step_index} is risk_tier=risky and confirm_risky is False")
                escalated_for_this_step = True

            extracted = None
            try:
                extracted = self.surface.act(action, locator=locator, target=target, text=text)
                if action == ActionType.SELECT_OPTION:
                    used_select_option = True
            except Exception as exc:
                if escalated_for_this_step:
                    # The human almost certainly performed this exact action themselves
                    # while control was handed over just above - by the time automation
                    # gets a turn again the page has already moved on (the control that
                    # would be clicked/typed into is simply gone), so re-attempting it
                    # here always fails. The step still gets recorded below with
                    # risk_tier=risky (matching why it escalated) rather than silently
                    # dropped - the simplest version of "wherever discovery escalates,
                    # replay escalates too": every step discovery paused a human for
                    # ends up in the artifact as risky, whether or not automation's own
                    # copy of that action succeeded afterward.
                    self._print(
                        f"[discover] {action.value} already performed manually during "
                        f"escalation - recording step {step_index} without re-attempting it"
                    )
                    self.evidence_logger.log_event(
                        "step_recorded_after_escalation", {"step_index": step_index, "action": action.value}
                    )
                else:
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

            # Did that action land on a page the host taxonomy recognises as an error?
            # Checked BEFORE the step is recorded, because the damage of not checking is
            # not a missing label - it is a corrupted artifact. Without this, the model
            # sees an unfamiliar screen, improvises, and the improvisation is recorded
            # as part of the capability.
            fault = next(
                (p for p in outcome_patterns if self.surface.check_checkpoint(p.checkpoint)), None
            )
            if fault is not None:
                self.evidence_logger.log_event(
                    "fault_detected",
                    {
                        "step_index": step_index,
                        "outcome": fault.outcome.value,
                        "detail": fault.detail,
                        "url": self.surface.current_url(),
                    },
                )
                if fault.outcome == OutcomeType.RECOVERABLE and fault.recovery_action is not None:
                    # Dismiss it and retry the same decision. The recovery is NOT recorded
                    # as a step: it is a property of the host being briefly unwell today,
                    # not of the capability being learned.
                    self._print(f"[discover] recovering from: {fault.detail}")
                    try:
                        self.surface.act(
                            fault.recovery_action.action,
                            locator=fault.recovery_action.locator,
                            target=fault.recovery_action.target,
                            text=fault.recovery_action.value,
                        )
                    except Exception as exc:
                        self._print(f"[discover] recovery failed: {type(exc).__name__}: {exc}")
                    history.append({**decision, "error": f"the host showed: {fault.detail} It was "
                                    "dismissed - re-check where you are and continue."})
                    continue

                # BUSINESS_OUTCOME / HARD_FAILURE / un-recoverable: the flow cannot be
                # learned from here. Hand to a human with the reason in their own words -
                # they can fix the input, sign on with the right role, or stop - which is
                # exactly the "ask the operator when you hit something unknown" path.
                self._print(f"[discover] fault: {fault.detail}")
                self._escalate(
                    goal, step_index,
                    f"the host reported: {fault.detail} Discovery stopped before recording this "
                    "step, so the capability does not learn the error path. Resolve it live "
                    "(correct the input, sign on with the right role) and resume, or cancel.",
                )
                history.append({**decision, "error": f"the host reported: {fault.detail} Do not "
                                "record this path. Re-check where you are before continuing."})
                continue

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
            # Checked before the step is recorded, not after: an extract anchored on the
            # value it just read (this run's confirmation number) or on the whole page
            # records perfectly here and then fails, or returns a screenful of noise, on
            # the very first real invocation. Rejected once per extract_as so the model
            # gets a chance to anchor on the stable label beside the value instead - and
            # only once, so a model that can't do better still produces a capability.
            unusable = (
                _unusable_extract_reason(locator, extracted)
                if action == ActionType.EXTRACT
                else None
            )
            # Bounded by how many times this run may be refused an extract AT ALL, not
            # per extract_as: the model renames the output when it retries
            # ("signed_on_line" -> "operator_signed_on_line"), which slipped straight
            # past a per-name bound and let the same argument repeat indefinitely.
            if unusable is not None and extract_rejections < _MAX_EXTRACT_REJECTIONS:
                extract_rejections += 1
                self._print(f"[discover] extract locator rejected once: {unusable}")
                self.evidence_logger.log_event(
                    "extract_locator_rejected", {"extract_as": extract_as, "reason": unusable}
                )
                # Both idioms are spelled out because naming only the table one sent
                # models hunting through CSS attribute selectors like
                # [role='text'][name^='Signed on as'] - which match nothing in HTML and
                # cost a 30-second timeout each - when the value was a line of prose and
                # a two-word text anchor was all it needed.
                history.append({
                    **decision,
                    "error": f"that extract locator is unusable because {unusable}. Anchor on the "
                    "stable label instead, and keep the anchor SHORT - just the label, never the "
                    "value. Two shapes cover almost everything:\n"
                    "- the value sits in its own cell beside a label -> "
                    "{\"strategy\":\"css\",\"value\":{\"css\":\"td:text-is('Confirmation:') + td\"}}\n"
                    "- the value sits in a line of text after a fixed label -> "
                    "{\"strategy\":\"text\",\"value\":{\"text\":\"Signed on as\"}}, which matches the "
                    "whole line and works whoever is signed on.\n"
                    "Do not invent attribute selectors like [role=...][name=...]: role and name are "
                    "accessibility-tree concepts, not HTML attributes, so they match nothing. Retry "
                    "the extract with one of the two shapes above.",
                })
                continue

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
            # The model can declare, on the SAME extract decision, that the goal wants a
            # computed value the page doesn't show directly (e.g. "total balance" summed
            # from individual line items) - derive_op is deliberately restricted to the
            # same closed set ReplayEngine._resolve_derived_outputs() already implements
            # (currently just sum_currency), so this only ever wires up a computation
            # replay can perform deterministically, never something requiring an LLM at
            # replay time. Recorded once per derive_as name; a repeat declaration for the
            # same output name is ignored rather than overwriting it.
            if (
                action == ActionType.EXTRACT
                and extract_as is not None
                and derive_as is not None
                and derive_op in ("sum_currency",)
                and derive_as not in {p.name for p in trace.derived_outputs}
            ):
                trace.derived_outputs.append(
                    OutputParam(
                        name=derive_as, type="string",
                        derive=DerivedOutputSpec(from_output=extract_as, op=derive_op),
                    )
                )
            if action == ActionType.EXTRACT:
                # Hand the extracted text back to the model. Without it the history says
                # only "I decided to extract" - so on a page that doesn't change as a
                # result (extract never does), the model sees the same tree, re-decides
                # the identical extract, and loops until the loop guard trips. It also
                # cannot otherwise do what the prompt asks and check that what it
                # captured is real data rather than an empty form.
                preview = " ".join((extracted or "").split())[:500]
                self.evidence_logger.log_event(
                    "extracted_value", {"extract_as": extract_as, "preview": preview}
                )
                tokenized = self.tokenizer.tokenize(preview)
                entry = {**decision, "extracted": tokenized or "(empty)"}
                # Regulated values (balances, account numbers) are tokenized before the
                # model ever sees them, which is the point - but without saying so, a
                # table of [[TOK...]] reads as a FAILED extract. Live-observed on the
                # balance capability: every dollar amount matched a redaction pattern,
                # the model could not tell it had captured the balances, and it tried
                # ten progressively more elaborate selectors over six minutes, each of
                # which had already worked. It needs to know the shape is right, never
                # the values.
                if tokenized != preview:
                    entry["extracted_note"] = (
                        "[[TOK...]] marks a real value that WAS captured and redacted before "
                        "being shown to you. Their presence means this extract worked - the "
                        "rows and columns around them are the proof. Do not retry the extract "
                        "to try to see them; you will never be shown them."
                    )
                history.append(entry)
            else:
                history.append(decision)

        if not trace.succeeded:
            self._escalate(
                goal,
                len(trace.steps) - 1 if trace.steps else None,
                f"stuck: reached max_steps ({self.max_steps}) without finish",
            )

        self.evidence_logger.log_event(
            "llm_usage_summary",
            {"tool_call_count": trace.tool_call_count, **trace.token_usage},
        )
        trace.final_url = self.surface.current_url()
        return trace
