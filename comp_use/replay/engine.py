import re
import time

from comp_use.escalation.controller import EscalationController
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import AllowlistViolation, Guardrail
from comp_use.schemas import Artifact, InterventionRequest, OutcomePattern, OutcomeType, ReplayResult
from comp_use.surface import safe_screenshot

_CURRENCY_RE = re.compile(r"\$[\d,]+\.\d{2}")
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _value_matches_declared_type(value, declared_type: str) -> bool:
    """A capability's input_schema declares a type per param, but nothing enforced
    it end-to-end - an LLM tool call (chat's propose_invoke) could hand a live
    Funds Transfer form an amount like "one hundred" and it would type straight
    into the page. `bool` is checked before `int`/`float` since `isinstance(True, int)`
    is True in Python and would otherwise let a boolean silently pass as a number."""
    if declared_type == "string":
        return isinstance(value, str)
    if declared_type == "number":
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return True
        return isinstance(value, str) and bool(_NUMBER_RE.match(value.strip()))
    if declared_type == "boolean":
        if isinstance(value, bool):
            return True
        return isinstance(value, str) and value.strip().lower() in ("true", "false")
    return True


def _resolve_derived_outputs(artifact: Artifact, outputs: dict) -> None:
    """Fills in any OutputParam with a `derive` spec, in place. Best-effort: a
    missing/unparseable source output leaves the derived output absent rather than
    failing the whole replay - a total that can't be computed is a lesser problem than
    losing an otherwise-successful run over it."""
    for output_param in artifact.output_schema:
        if output_param.derive is None:
            continue
        source_text = outputs.get(output_param.derive.from_output)
        if not isinstance(source_text, str):
            continue
        amounts = [float(m.replace("$", "").replace(",", "")) for m in _CURRENCY_RE.findall(source_text)]
        if amounts:
            outputs[output_param.name] = f"${sum(amounts):,.2f}"


def validate_required_params(artifact: Artifact, params: dict) -> str | None:
    """Single source of truth for "does this params dict satisfy this artifact's
    input_schema" - used both by ReplayEngine.run() and by cli.py's _run_replay
    (which needs a pre-browser answer, before ReplayEngine even exists, to honor
    "skip launching Chromium on validation_error"), and by the chat endpoint
    before trusting an LLM tool call's params. Checks presence of every required
    param AND, for any param actually supplied, that its value matches the type
    declared in input_schema - never trust a caller's (especially an LLM's)
    claimed types."""
    for input_param in artifact.input_schema:
        if input_param.required and input_param.name not in params:
            return f"missing required param '{input_param.name}'"
        if input_param.name in params and not _value_matches_declared_type(
            params[input_param.name], input_param.type
        ):
            return (
                f"param '{input_param.name}' must be of type '{input_param.type}' "
                f"(got {params[input_param.name]!r})"
            )
    return None


class ReplayEngine:
    def __init__(self, surface, guardrail: Guardrail, evidence_logger: EvidenceLogger, escalation: EscalationController | None = None):
        self.surface = surface
        self.guardrail = guardrail
        self.evidence_logger = evidence_logger
        self.escalation = escalation

    def _match_outcome_pattern(self, artifact: Artifact) -> OutcomePattern | None:
        for pattern in artifact.outcome_patterns:
            if self.surface.check_checkpoint(pattern.checkpoint):
                return pattern
        return None

    def _attempt_recovery(self, artifact: Artifact, index: int, pattern: OutcomePattern, confirm_risky: bool) -> None:
        """Best-effort: perform the pattern's declared recovery_action (e.g. dismiss an
        interstitial, click 'Try Again') before the caller retries the current step.

        A recovery_action is a real action against the live surface, so it gets the
        SAME two guardrails every other step in run() gets - never silently skipped:
        - `guardrail.check_allowlist()` - raises AllowlistViolation on a violation,
          which is deliberately NOT caught here (propagates out of this method, out of
          _should_retry, and out of run()'s call sites as a HARD_FAILURE). A recovery
          action trying to leave the allowlisted domain is a policy breach, not a
          transient condition - it must hard-stop the run, never be silently retried
          or softened into a mere `recoverable` report.
        - `guardrail.requires_confirmation()` - escalates via EscalationController
          exactly like a normal risky step does, *before* the action runs.

        Once past both, performing the action failing (element not found, etc.) IS
        best-effort and non-fatal on its own: if recovery genuinely didn't work, the
        next _match_outcome_pattern check simply sees the same recoverable state again,
        and the bounded retry budget in run() eventually gives up and reports it."""
        if pattern.retry_delay_seconds > 0:
            time.sleep(pattern.retry_delay_seconds)
        if pattern.recovery_action is None:
            return
        action = pattern.recovery_action

        self.guardrail.check_allowlist(action.target or self.surface.current_url(), action.action.value)

        if self.guardrail.requires_confirmation(action.risk_tier, confirm_risky) and self.escalation is not None:
            self.escalation.escalate(
                InterventionRequest(
                    run_id=self.evidence_logger.run_id,
                    capability_or_goal=artifact.capability_name,
                    current_step=index,
                    screenshot_path=self.evidence_logger.save_screenshot(
                        safe_screenshot(self.surface), f"escalation_recovery_step{index}"
                    ),
                    reason=f"recovery_action for step {index} is risk_tier={action.risk_tier.value} "
                    "and confirm_risky is False",
                )
            )

        try:
            self.surface.act(action.action, locator=action.locator, target=action.target, text=action.value)
            self.evidence_logger.log_event(
                "recovery_action_performed", {"step_index": index, "action": action.action.value}
            )
        except Exception as exc:
            self.evidence_logger.log_event(
                "recovery_action_failed", {"step_index": index, "error": f"{type(exc).__name__}: {exc}"}
            )
            return

        # Same post-action re-check run() does for every normal step: the recovery
        # action just performed (most concretely a CLICK) may have navigated somewhere
        # new - deliberately NOT caught here, same reasoning as the pre-check above.
        self.guardrail.check_allowlist(self.surface.current_url(), action.action.value)

    def _should_retry(
        self, artifact: Artifact, pattern: OutcomePattern, index: int, retries_used: dict[int, int], confirm_risky: bool
    ) -> bool:
        """True if `pattern` is a RECOVERABLE pattern with retry budget left (3 by
        default - see OutcomePattern.max_retries), in which case the recovery action
        (if any) has already been performed and the caller should retry rather than
        give up. False means: report `pattern.outcome` as final - either because it's
        a business_outcome (never retried - "no such member" isn't fixed by retrying),
        or a recoverable pattern whose author explicitly set max_retries=0 (a condition
        known to never clear on its own), or one whose budget is now exhausted.

        Can also raise AllowlistViolation (from _attempt_recovery) - deliberately not
        caught here; see _handle_matched_pattern for how run() reacts to that."""
        if pattern.outcome != OutcomeType.RECOVERABLE or pattern.max_retries <= 0:
            return False
        used = retries_used.get(id(pattern), 0)
        if used >= pattern.max_retries:
            return False
        retries_used[id(pattern)] = used + 1
        self.evidence_logger.log_event(
            "recoverable_retry",
            {"step_index": index, "attempt": used + 1, "max_retries": pattern.max_retries, "detail": pattern.detail},
        )
        self._attempt_recovery(artifact, index, pattern, confirm_risky)
        return True

    def _handle_matched_pattern(
        self, artifact: Artifact, pattern: OutcomePattern, index: int, retries_used: dict[int, int], confirm_risky: bool
    ) -> ReplayResult | None:
        """Returns None if a retry was performed and run()'s loop should `continue`
        (retry the same step); otherwise a final ReplayResult run() should return -
        either the pattern's own declared outcome (retries exhausted / business_outcome
        / opted out via max_retries=0), or HARD_FAILURE if the recovery_action itself
        violated the allowlist or its escalation transport failed (e.g. a real,
        live-observed RuntimeError: LocalSharedBrowserTransport requires an
        interactive terminal). Either way, nothing from _should_retry/_attempt_recovery
        is ever allowed to propagate as a raw traceback out of this engine."""
        try:
            if self._should_retry(artifact, pattern, index, retries_used, confirm_risky):
                return None
        except AllowlistViolation as exc:
            return ReplayResult(
                outcome=OutcomeType.HARD_FAILURE,
                step_index=index,
                detail=f"{type(exc).__name__}: {exc}",
                expected="recovery_action to pass the URL/action allowlist",
                observed=self.surface.current_url(),
            )
        except Exception as exc:
            return ReplayResult(
                outcome=OutcomeType.HARD_FAILURE,
                step_index=index,
                detail=f"{type(exc).__name__}: {exc}",
                expected="recovery_action's escalation to complete",
                observed=self.surface.current_url(),
            )
        return ReplayResult(outcome=pattern.outcome, detail=pattern.detail)

    def run(self, artifact: Artifact, params: dict, confirm_risky: bool = False) -> ReplayResult:
        validation_error = validate_required_params(artifact, params)
        if validation_error:
            self.evidence_logger.log_event("validation_error", {"detail": validation_error})
            return ReplayResult(outcome=OutcomeType.VALIDATION_ERROR, detail=validation_error)

        outputs: dict = {}
        retries_used: dict[int, int] = {}
        index = 0
        while index < len(artifact.steps):
            step = artifact.steps[index]

            if self.escalation is not None and self.escalation.takeover_requested():
                # A human clicked "Take control" on the dashboard - pause here, before
                # this step runs, exactly like a risky-step escalation just below.
                try:
                    self.escalation.escalate(
                        InterventionRequest(
                            run_id=self.evidence_logger.run_id,
                            capability_or_goal=artifact.capability_name,
                            current_step=index,
                            screenshot_path=self.evidence_logger.save_screenshot(
                                safe_screenshot(self.surface), f"escalation_step{index}"
                            ),
                            reason="manual takeover requested by operator",
                        )
                    )
                except Exception as exc:
                    return ReplayResult(
                        outcome=OutcomeType.HARD_FAILURE,
                        step_index=index,
                        detail=f"{type(exc).__name__}: {exc}",
                        expected="escalation to complete (manual takeover)",
                        observed=self.surface.current_url(),
                    )

            # The app may have already diverged onto a business/recoverable outcome
            # page after a previous step (e.g. "insufficient funds" instead of the
            # review screen a later recorded step expects). Detect that *before*
            # blindly attempting a step whose target element may not exist. A matched
            # RECOVERABLE pattern with retry budget left retries THIS SAME step (index
            # is not advanced) rather than giving up immediately.
            pattern = self._match_outcome_pattern(artifact)
            if pattern is not None:
                result = self._handle_matched_pattern(artifact, pattern, index, retries_used, confirm_risky)
                if result is not None:
                    return result
                continue

            if self.guardrail.requires_confirmation(step.risk_tier, confirm_risky) and self.escalation is not None:
                try:
                    self.escalation.escalate(
                        InterventionRequest(
                            run_id=self.evidence_logger.run_id,
                            capability_or_goal=artifact.capability_name,
                            current_step=index,
                            screenshot_path=self.evidence_logger.save_screenshot(
                                safe_screenshot(self.surface), f"escalation_step{index}"
                            ),
                            reason=f"step {index} is risk_tier=risky and confirm_risky is False",
                        )
                    )
                except Exception as exc:
                    # A transport failure (e.g. non-interactive stdin) must never
                    # surface as a raw traceback - without human confirmation there's
                    # no safe way to proceed with a risky step, so this is HARD_FAILURE,
                    # not a skip.
                    return ReplayResult(
                        outcome=OutcomeType.HARD_FAILURE,
                        step_index=index,
                        detail=f"{type(exc).__name__}: {exc}",
                        expected="escalation to complete (human confirmation for a risky step)",
                        observed=self.surface.current_url(),
                    )

            text = step.value
            if step.value_source is not None and step.value_source.type == "goal_parameter":
                param_name = step.value_source.param_name
                if param_name not in params:
                    # validate_required_params only checks input_schema entries marked
                    # required=True - a step can still reference an optional-and-omitted
                    # or mismatched param name and reach here. Report it the same way as
                    # any other "caller didn't supply what this artifact needs" case,
                    # rather than letting a raw KeyError crash the whole replay.
                    detail = f"step {index} references param '{param_name}' which was not provided"
                    self.evidence_logger.log_event("validation_error", {"detail": detail})
                    return ReplayResult(outcome=OutcomeType.VALIDATION_ERROR, detail=detail, step_index=index)
                text = str(params[param_name])

            target_url = step.target

            try:
                self.guardrail.check_allowlist(target_url or self.surface.current_url(), step.action.value)
            except AllowlistViolation as exc:
                # Kept distinct from the locator/action-failure except block below - this is
                # a policy rejection, not a UI drift, and must not look like one to
                # cli.py's _is_action_locator_failure (which drives --diagnose-drift-on-failure).
                return ReplayResult(
                    outcome=OutcomeType.HARD_FAILURE,
                    step_index=index,
                    detail=f"{type(exc).__name__}: {exc}",
                    expected=f"{step.action.value} to pass the URL/action allowlist",
                    observed=self.surface.current_url(),
                )

            try:
                extracted = self.surface.act(step.action, locator=step.locator, target=target_url, text=text)
                if step.extract_as:
                    outputs[step.extract_as] = extracted
            except Exception as exc:
                pattern = self._match_outcome_pattern(artifact)
                if pattern is not None:
                    result = self._handle_matched_pattern(artifact, pattern, index, retries_used, confirm_risky)
                    if result is not None:
                        return result
                    continue
                return ReplayResult(
                    outcome=OutcomeType.HARD_FAILURE,
                    step_index=index,
                    detail=f"{type(exc).__name__}: {exc}",
                    expected=f"{step.action.value} to succeed",
                    observed=self.surface.current_url(),
                )
            self.evidence_logger.log_event("replay_step", {"index": index, "action": step.action.value})

            # The action that just ran (most concretely a CLICK, but any action could
            # trigger a redirect) may have navigated the browser somewhere new. The
            # check above only validated where we were BEFORE the action - a click that
            # navigates off-allowlist was never checked at all. Re-validate the
            # CURRENT url now, every time, so leaving the allowlist can't go unnoticed
            # just because it happened as a side effect of an otherwise-allowed action.
            try:
                self.guardrail.check_allowlist(self.surface.current_url(), step.action.value)
            except AllowlistViolation as exc:
                return ReplayResult(
                    outcome=OutcomeType.HARD_FAILURE,
                    step_index=index,
                    detail=f"{type(exc).__name__}: {exc}",
                    expected=f"{step.action.value} result to stay within the URL/action allowlist",
                    observed=self.surface.current_url(),
                )

            if step.checkpoint is not None and not self.surface.check_checkpoint(step.checkpoint):
                pattern = self._match_outcome_pattern(artifact)
                if pattern is not None:
                    result = self._handle_matched_pattern(artifact, pattern, index, retries_used, confirm_risky)
                    if result is not None:
                        return result
                    continue
                return ReplayResult(
                    outcome=OutcomeType.HARD_FAILURE,
                    step_index=index,
                    expected=str(step.checkpoint),
                    observed=self.surface.current_url(),
                )

            index += 1

        while not self.surface.check_checkpoint(artifact.success_checkpoint):
            pattern = self._match_outcome_pattern(artifact)
            if pattern is not None:
                result = self._handle_matched_pattern(
                    artifact, pattern, len(artifact.steps) - 1, retries_used, confirm_risky
                )
                if result is not None:
                    return result
                continue
            return ReplayResult(
                outcome=OutcomeType.HARD_FAILURE,
                step_index=len(artifact.steps) - 1,
                expected=str(artifact.success_checkpoint),
                observed=self.surface.current_url(),
            )

        _resolve_derived_outputs(artifact, outputs)
        return ReplayResult(outcome=OutcomeType.SUCCESS, outputs=outputs)
