"""Best-effort, human-gated drift diagnosis for a failed replay step.

Deliberately NOT part of comp_use/replay/engine.py: ReplayEngine must stay LLM-free
(the "deterministic replay, no further LLM calls" guarantee is the core promise of
this system - see REPORT.md's Architecture table). This module runs strictly after
a replay has already produced its final, LLM-free ReplayResult, orchestrated by the
CLI (which already talks to an LLM for discover). It only ever proposes a patched
artifact as a new, unsaved version - nothing here saves, applies, or replays it.
That's the caller's job, gated on human approval via EscalationController.
"""
import base64
from dataclasses import dataclass

from comp_use.llm_client import LLMClient
from comp_use.schemas import Artifact, Locator


@dataclass
class DriftDiagnosis:
    patched_artifact: Artifact | None
    reasoning: str = ""


def propose_drift_patch(
    llm_client: LLMClient, surface, artifact: Artifact, failed_step_index: int
) -> DriftDiagnosis:
    """Ask a vision model whether the step that just failed to resolve is
    pointing at a control that merely moved/got renamed (drift) rather than a
    genuine break. `patched_artifact` is a *new*, unsaved Artifact with that
    one step's locator patched, or None if no plausible replacement was found
    - the `reasoning` is returned either way, for evidence/debugging."""
    step = artifact.steps[failed_step_index]
    if step.locator is None:
        # Nothing to diagnose - this step didn't fail because a locator
        # stopped resolving (e.g. a NAVIGATE step failing is a different kind
        # of problem entirely).
        return DriftDiagnosis(patched_artifact=None, reasoning="step has no locator to diagnose")

    screenshot_b64 = base64.b64encode(surface.screenshot()).decode("ascii")
    diagnosis = llm_client.diagnose_drift(
        expected_locator=step.locator.model_dump(mode="json"), screenshot_b64=screenshot_b64
    )
    reasoning = diagnosis.get("reasoning", "")
    if not diagnosis.get("found"):
        return DriftDiagnosis(patched_artifact=None, reasoning=reasoning)

    try:
        new_locator = Locator.model_validate(diagnosis["locator"])
    except Exception:
        return DriftDiagnosis(patched_artifact=None, reasoning=reasoning or "model returned an invalid locator")

    patched = artifact.model_copy(deep=True)
    patched.steps[failed_step_index].locator = new_locator
    return DriftDiagnosis(patched_artifact=patched, reasoning=reasoning)
