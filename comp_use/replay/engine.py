import re
import time

from comp_use.discovery.outcome_library import outcome_patterns_for_target
from comp_use.escalation.controller import EscalationController
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import AllowlistViolation, Guardrail, is_sensitive_param_name
from comp_use.schemas import (
    ActionType,
    Artifact,
    InterventionRequest,
    OutcomePattern,
    OutcomeType,
    ReplayResult,
    RiskTier,
)
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
    def _finish_if_already_succeeded(self, artifact: Artifact, outputs: dict) -> ReplayResult | None:
        """Checked right after every escalation resumes, before ever attempting
        another recorded step. A human taking over during an escalation has full
        control of the browser and no obligation to stop at exactly the point
        automation was stuck - they may complete the ENTIRE remaining flow
        themselves (confirmed live: a "supervisor override" denial resolved by the
        operator finishing the whole Place Account Hold there and then, not just
        clicking past the denial screen). Without this check, the next recorded
        step (e.g. selecting a share from a dropdown that no longer exists once
        the hold is already applied) is attempted against a page that has already
        moved past it, and fails - `HARD_FAILURE` for a run that, in fact,
        succeeded. Returns a SUCCESS result if the artifact's own success
        checkpoint is already satisfied, else None (continue automating
        normally)."""
        if self.surface.check_checkpoint(artifact.success_checkpoint):
            _resolve_derived_outputs(artifact, outputs)
            return ReplayResult(outcome=OutcomeType.SUCCESS, outputs=outputs)
        return None

    def __init__(self, surface, guardrail: Guardrail, evidence_logger: EvidenceLogger, escalation: EscalationController | None = None):
        self.surface = surface
        self.guardrail = guardrail
        self.evidence_logger = evidence_logger
        self.escalation = escalation

    def _unclassified_failure(
        self,
        outputs: dict,
        recovered_from: OutcomePattern | None,
        *,
        step_index: int,
        expected: str,
        detail: str = "",
    ) -> ReplayResult:
        """The result for a failure no outcome pattern explains.

        Normally HARD_FAILURE - but not if a RECOVERABLE condition was already met and
        its recovery action performed earlier in this run. Live-observed: an injected
        maintenance interstitial appeared right after sign-on, was correctly matched,
        and its "Continue" link was clicked - which returned the host to a SIGNED-OUT
        sign-on page. Replay resumed at the step it was on, but the interruption had
        cost it the session, so every later step failed against a page showing no error
        at all. Reporting that as a hard failure blames the automation for a transient
        host condition it recognised and handled correctly.

        Deliberately NOT solved by restarting the capability from step 0: a flow whose
        earlier steps are irreversible (post a transfer, apply a hold) must never be
        silently re-run. Recoverable is the honest report - the caller re-invokes.

        Outputs collected before the failure travel with every result: a run that read
        three values and then died still knows those three values, and dropping them
        made a partial answer indistinguishable from no answer."""
        if recovered_from is not None:
            return ReplayResult(
                outcome=OutcomeType.RECOVERABLE,
                step_index=step_index,
                detail=(
                    f"{recovered_from.detail} It was dismissed, but the interruption left the "
                    "host in a state this capability cannot resume from mid-flow (typically "
                    "signed out). Re-invoke the capability to run it from the start."
                ),
                expected=expected,
                observed=self.surface.current_url(),
                outputs=outputs,
            )
        return ReplayResult(
            outcome=OutcomeType.HARD_FAILURE,
            step_index=step_index,
            detail=detail,
            expected=expected,
            observed=self.surface.current_url(),
            outputs=outputs,
        )

    def _match_outcome_pattern(self, artifact: Artifact) -> OutcomePattern | None:
        # The artifact's own patterns first: anything hand-authored for THIS capability
        # is more specific than the shared, host-wide taxonomy behind it.
        for pattern in artifact.outcome_patterns + outcome_patterns_for_target(artifact.target):
            if self.surface.check_checkpoint(pattern.checkpoint):
                return pattern
        return None

    def _pattern_detail(self, pattern: OutcomePattern) -> str:
        """The pattern's category text, plus the live reason off the matched page when
        the pattern declares where that sits. "The transaction could not be validated"
        tells a caller only which category their request fell into; "Insufficient
        available balance in the source share" tells them what to do about it. Reading
        it is best-effort - a page that no longer renders the reason must not turn a
        clean business-outcome report into a crash."""
        if pattern.detail_locator is None:
            return pattern.detail
        try:
            reason = self.surface.act(
                ActionType.EXTRACT, locator=pattern.detail_locator, target=None, text=None
            )
        except Exception:
            return pattern.detail
        reason = " ".join((reason or "").split())
        return f"{pattern.detail} {reason}".strip() if reason else pattern.detail

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
        return ReplayResult(
            outcome=pattern.outcome,
            detail=self._pattern_detail(pattern),
            step_index=index,
            observed=self.surface.current_url(),
        )

    def run(self, artifact: Artifact, params: dict, confirm_risky: bool = False) -> ReplayResult:
        validation_error = validate_required_params(artifact, params)
        if validation_error:
            self.evidence_logger.log_event("validation_error", {"detail": validation_error})
            return ReplayResult(outcome=OutcomeType.VALIDATION_ERROR, detail=validation_error)

        outputs: dict = {}
        retries_used: dict[int, int] = {}
        # The last RECOVERABLE condition this run met and acted on, if any. Remembered
        # because its after-effects can outlive the page that showed it - see
        # _unclassified_failure.
        recovered_from: OutcomePattern | None = None
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
                already_succeeded = self._finish_if_already_succeeded(artifact, outputs)
                if already_succeeded is not None:
                    return already_succeeded

            # The app may have already diverged onto a business/recoverable outcome
            # page after a previous step (e.g. "insufficient funds" instead of the
            # review screen a later recorded step expects). Detect that *before*
            # blindly attempting a step whose target element may not exist. A matched
            # RECOVERABLE pattern with retry budget left retries THIS SAME step (index
            # is not advanced) rather than giving up immediately.
            pattern = self._match_outcome_pattern(artifact)
            if pattern is not None:
                # A BUSINESS_OUTCOME match here would otherwise short-circuit the run
                # before ever reaching a step discovery itself flagged risky - meaning
                # a human confirmed, during discovery, that this exact artifact
                # legitimately needs judgment further down the line. An unexpected
                # outcome page on the way there deserves the same scrutiny, not an
                # automatic, unappealable give-up: escalate first and let a human
                # look at the live page. If they resolve it (the human's own action
                # during the handoff clears the condition - e.g. this was a stale
                # page, a transient state, or something only a human could push
                # past), the SAME step is re-attempted normally; if the condition is
                # still there after resume, it's reported exactly as before - the
                # human's own judgment that this is a genuine dead end, not the
                # engine's.
                has_pending_risky_step = any(
                    s.risk_tier == RiskTier.RISKY for s in artifact.steps[index:]
                )
                if (
                    pattern.outcome == OutcomeType.BUSINESS_OUTCOME
                    and has_pending_risky_step
                    and self.escalation is not None
                ):
                    try:
                        self.escalation.escalate(
                            InterventionRequest(
                                run_id=self.evidence_logger.run_id,
                                capability_or_goal=artifact.capability_name,
                                current_step=index,
                                screenshot_path=self.evidence_logger.save_screenshot(
                                    safe_screenshot(self.surface), f"escalation_step{index}"
                                ),
                                reason=f"unexpected outcome '{pattern.detail}' matched before reaching a "
                                "step this artifact records as risky (which a human already confirmed "
                                "during discovery) - confirm this is a genuine business outcome, or "
                                "resolve it live and resume to continue",
                            )
                        )
                    except Exception as exc:
                        return ReplayResult(
                            outcome=OutcomeType.HARD_FAILURE,
                            step_index=index,
                            detail=f"{type(exc).__name__}: {exc}",
                            expected="escalation to complete (unexpected outcome before a risky step)",
                            observed=self.surface.current_url(),
                        )
                    already_succeeded = self._finish_if_already_succeeded(artifact, outputs)
                    if already_succeeded is not None:
                        return already_succeeded
                    pattern = self._match_outcome_pattern(artifact)
                    if pattern is None:
                        continue
                if pattern.outcome == OutcomeType.RECOVERABLE:
                    recovered_from = pattern
                result = self._handle_matched_pattern(artifact, pattern, index, retries_used, confirm_risky)
                if result is not None:
                    return result
                continue

            escalated_for_this_step = False
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
                    escalated_for_this_step = True
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
                # The human may have completed this risky step AND everything after
                # it themselves during the handoff, not just confirmed it's safe to
                # proceed - checking for overall success now, before ever attempting
                # this (or a later) recorded step, is what makes "hand back control
                # once you're done, whether or not you did more than strictly
                # necessary" actually work.
                already_succeeded = self._finish_if_already_succeeded(artifact, outputs)
                if already_succeeded is not None:
                    return already_succeeded

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
                # A recognised outcome page explains the failure outright, so it is
                # checked FIRST - before the escalated_for_this_step assumption below.
                # Live-observed why: a transfer whose source share was on HOLD escalated
                # at its risky step (correctly), the human resumed without fixing
                # anything, and the "Post Transfer" click then failed simply because the
                # rejection page has no such button. Assuming "the human must have done
                # it" recorded a success-shaped event for a transfer that never posted,
                # and the run limped on to die at the next step with a locator timeout
                # that named nothing real. The rejection itself is the answer.
                pattern = self._match_outcome_pattern(artifact)
                if pattern is not None:
                    if pattern.outcome == OutcomeType.RECOVERABLE:
                        recovered_from = pattern
                    result = self._handle_matched_pattern(artifact, pattern, index, retries_used, confirm_risky)
                    if result is not None:
                        return result
                    continue
                if escalated_for_this_step:
                    # No recognised outcome page, and control was handed to a human at
                    # this exact step: they almost certainly performed the action
                    # themselves, so by the time replay gets a turn again the control it
                    # targeted is gone from the page and re-attempting it always fails.
                    # Same gap already fixed in DiscoveryAgent (ENHANCEMENTS.md #16):
                    # treat it as "already done manually" and fall through to the normal
                    # post-action checks below (allowlist/checkpoint) against whatever
                    # state the human actually left the page in.
                    self.evidence_logger.log_event(
                        "step_completed_during_escalation", {"step_index": index, "action": step.action.value}
                    )
                else:
                    return self._unclassified_failure(
                        outputs,
                        recovered_from,
                        step_index=index,
                        expected=f"{step.action.value} to succeed",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
            # Surfaced on the dashboard/chat's run log (what got typed/clicked, not just
            # "type_text happened") - masked by param name, not by value shape, since a
            # substituted param here is the caller's real data (unlike discovery's decision
            # log, which only ever sees a value the LLM itself chose/typed).
            is_sensitive = (
                step.value_source is not None and is_sensitive_param_name(step.value_source.param_name)
            )
            self.evidence_logger.log_event(
                "replay_step",
                {
                    "index": index,
                    "action": step.action.value,
                    "locator": step.locator.model_dump(mode="json") if step.locator else None,
                    "target": target_url,
                    "value": ("••••••" if is_sensitive else text) if text is not None else None,
                },
            )

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
                    if pattern.outcome == OutcomeType.RECOVERABLE:
                        recovered_from = pattern
                    result = self._handle_matched_pattern(artifact, pattern, index, retries_used, confirm_risky)
                    if result is not None:
                        return result
                    continue
                return self._unclassified_failure(
                    outputs, recovered_from, step_index=index, expected=str(step.checkpoint)
                )

            index += 1

        while not self.surface.check_checkpoint(artifact.success_checkpoint):
            pattern = self._match_outcome_pattern(artifact)
            if pattern is not None:
                if pattern.outcome == OutcomeType.RECOVERABLE:
                    recovered_from = pattern
                result = self._handle_matched_pattern(
                    artifact, pattern, len(artifact.steps) - 1, retries_used, confirm_risky
                )
                if result is not None:
                    return result
                continue
            return self._unclassified_failure(
                outputs,
                recovered_from,
                step_index=len(artifact.steps) - 1,
                expected=str(artifact.success_checkpoint),
            )

        _resolve_derived_outputs(artifact, outputs)
        return ReplayResult(outcome=OutcomeType.SUCCESS, outputs=outputs)
