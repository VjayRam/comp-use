import time

from comp_use.escalation.controller import EscalationController
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import AllowlistViolation, Guardrail
from comp_use.schemas import Artifact, InterventionRequest, OutcomePattern, OutcomeType, ReplayResult
from comp_use.surface import safe_screenshot


def validate_required_params(artifact: Artifact, params: dict) -> str | None:
    """Single source of truth for "does this params dict satisfy this artifact's
    required input_schema" - used both by ReplayEngine.run() and by cli.py's
    _run_replay (which needs a pre-browser answer, before ReplayEngine even
    exists, to honor "skip launching Chromium on validation_error")."""
    for input_param in artifact.input_schema:
        if input_param.required and input_param.name not in params:
            return f"missing required param '{input_param.name}'"
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

    def _attempt_recovery(self, index: int, pattern: OutcomePattern) -> None:
        """Best-effort: perform the pattern's declared recovery_action (e.g. dismiss an
        interstitial, click 'Try Again') before the caller retries the current step.
        A failure here is not itself fatal - if recovery genuinely didn't work, the
        next _match_outcome_pattern check simply sees the same recoverable state again,
        and the bounded retry budget in run() eventually gives up and reports it."""
        if pattern.retry_delay_seconds > 0:
            time.sleep(pattern.retry_delay_seconds)
        if pattern.recovery_action is None:
            return
        try:
            self.surface.act(
                pattern.recovery_action.action,
                locator=pattern.recovery_action.locator,
                target=pattern.recovery_action.target,
                text=pattern.recovery_action.value,
            )
        except Exception as exc:
            self.evidence_logger.log_event(
                "recovery_action_failed", {"step_index": index, "error": f"{type(exc).__name__}: {exc}"}
            )

    def _should_retry(self, pattern: OutcomePattern, index: int, retries_used: dict[int, int]) -> bool:
        """True if `pattern` is a RECOVERABLE pattern with retry budget left (3 by
        default - see OutcomePattern.max_retries), in which case the recovery action
        (if any) has already been performed and the caller should retry rather than
        give up. False means: report `pattern.outcome` as final - either because it's
        a business_outcome (never retried - "no such member" isn't fixed by retrying),
        or a recoverable pattern whose author explicitly set max_retries=0 (a condition
        known to never clear on its own), or one whose budget is now exhausted."""
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
        self._attempt_recovery(index, pattern)
        return True

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

            # The app may have already diverged onto a business/recoverable outcome
            # page after a previous step (e.g. "insufficient funds" instead of the
            # review screen a later recorded step expects). Detect that *before*
            # blindly attempting a step whose target element may not exist. A matched
            # RECOVERABLE pattern with retry budget left retries THIS SAME step (index
            # is not advanced) rather than giving up immediately.
            pattern = self._match_outcome_pattern(artifact)
            if pattern is not None:
                if self._should_retry(pattern, index, retries_used):
                    continue
                return ReplayResult(outcome=pattern.outcome, detail=pattern.detail)

            if self.guardrail.requires_confirmation(step.risk_tier, confirm_risky) and self.escalation is not None:
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
                    if self._should_retry(pattern, index, retries_used):
                        continue
                    return ReplayResult(outcome=pattern.outcome, detail=pattern.detail)
                return ReplayResult(
                    outcome=OutcomeType.HARD_FAILURE,
                    step_index=index,
                    detail=f"{type(exc).__name__}: {exc}",
                    expected=f"{step.action.value} to succeed",
                    observed=self.surface.current_url(),
                )
            self.evidence_logger.log_event("replay_step", {"index": index, "action": step.action.value})

            if step.checkpoint is not None and not self.surface.check_checkpoint(step.checkpoint):
                pattern = self._match_outcome_pattern(artifact)
                if pattern is not None:
                    if self._should_retry(pattern, index, retries_used):
                        continue
                    return ReplayResult(outcome=pattern.outcome, detail=pattern.detail)
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
                if self._should_retry(pattern, len(artifact.steps) - 1, retries_used):
                    continue
                return ReplayResult(outcome=pattern.outcome, detail=pattern.detail)
            return ReplayResult(
                outcome=OutcomeType.HARD_FAILURE,
                step_index=len(artifact.steps) - 1,
                expected=str(artifact.success_checkpoint),
                observed=self.surface.current_url(),
            )

        return ReplayResult(outcome=OutcomeType.SUCCESS, outputs=outputs)
