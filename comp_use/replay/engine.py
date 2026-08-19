from comp_use.escalation.controller import EscalationController
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.schemas import Artifact, InterventionRequest, OutcomeType, ReplayResult, RiskTier


class ReplayEngine:
    def __init__(self, surface, guardrail: Guardrail, evidence_logger: EvidenceLogger, escalation: EscalationController | None = None):
        self.surface = surface
        self.guardrail = guardrail
        self.evidence_logger = evidence_logger
        self.escalation = escalation

    def _validate_params(self, artifact: Artifact, params: dict) -> str | None:
        for input_param in artifact.input_schema:
            if input_param.required and input_param.name not in params:
                return f"missing required param '{input_param.name}'"
        return None

    def _match_outcome_pattern(self, artifact: Artifact) -> ReplayResult | None:
        for pattern in artifact.outcome_patterns:
            if self.surface.check_checkpoint(pattern.checkpoint):
                return ReplayResult(outcome=pattern.outcome, detail=pattern.detail)
        return None

    def run(self, artifact: Artifact, params: dict, confirm_risky: bool = False) -> ReplayResult:
        validation_error = self._validate_params(artifact, params)
        if validation_error:
            self.evidence_logger.log_event("validation_error", {"detail": validation_error})
            return ReplayResult(outcome=OutcomeType.VALIDATION_ERROR, detail=validation_error)

        outputs: dict = {}
        for index, step in enumerate(artifact.steps):
            # The app may have already diverged onto a business/recoverable outcome
            # page after a previous step (e.g. "insufficient funds" instead of the
            # review screen a later recorded step expects). Detect that *before*
            # blindly attempting a step whose target element may not exist.
            outcome_result = self._match_outcome_pattern(artifact)
            if outcome_result is not None:
                return outcome_result

            if (
                step.risk_tier == RiskTier.RISKY
                and not confirm_risky
                and self.escalation is not None
            ):
                self.escalation.escalate(
                    InterventionRequest(
                        run_id=self.evidence_logger.run_id,
                        capability_or_goal=artifact.capability_name,
                        current_step=index,
                        screenshot_path=self.evidence_logger.save_screenshot(
                            self.surface.screenshot(), f"escalation_step{index}"
                        ),
                        reason=f"step {index} is risk_tier=risky and confirm_risky is False",
                    )
                )

            text = step.value
            if step.value_source is not None and step.value_source.type == "goal_parameter":
                text = str(params[step.value_source.param_name])

            target_url = step.target

            try:
                self.guardrail.check_allowlist(target_url or self.surface.current_url(), step.action.value)
                extracted = self.surface.act(step.action, locator=step.locator, target=target_url, text=text)
                if step.extract_as:
                    outputs[step.extract_as] = extracted
            except Exception as exc:
                outcome_result = self._match_outcome_pattern(artifact)
                if outcome_result is not None:
                    return outcome_result
                return ReplayResult(
                    outcome=OutcomeType.HARD_FAILURE,
                    step_index=index,
                    detail=str(exc),
                    expected=f"{step.action.value} to succeed",
                    observed=self.surface.current_url(),
                )
            self.evidence_logger.log_event("replay_step", {"index": index, "action": step.action.value})

            if step.checkpoint is not None and not self.surface.check_checkpoint(step.checkpoint):
                outcome_result = self._match_outcome_pattern(artifact)
                if outcome_result is not None:
                    return outcome_result
                return ReplayResult(
                    outcome=OutcomeType.HARD_FAILURE,
                    step_index=index,
                    expected=str(step.checkpoint),
                    observed=self.surface.current_url(),
                )

        if not self.surface.check_checkpoint(artifact.success_checkpoint):
            outcome_result = self._match_outcome_pattern(artifact)
            if outcome_result is not None:
                return outcome_result
            return ReplayResult(
                outcome=OutcomeType.HARD_FAILURE,
                step_index=len(artifact.steps) - 1,
                expected=str(artifact.success_checkpoint),
                observed=self.surface.current_url(),
            )

        return ReplayResult(outcome=OutcomeType.SUCCESS, outputs=outputs)
