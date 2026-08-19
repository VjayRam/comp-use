import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from comp_use.config import load_settings
from comp_use.discovery.agent import DiscoveryAgent
from comp_use.discovery.compiler import compile_artifact
from comp_use.drift import propose_drift_patch
from comp_use.escalation.controller import EscalationController
from comp_use.escalation.transport import LocalSharedBrowserTransport
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.llm_client import OpenRouterClient
from comp_use.replay.engine import ReplayEngine, validate_required_params
from comp_use.schemas import (
    ActionType,
    Artifact,
    Checkpoint,
    CheckpointType,
    InterventionRequest,
    Locator,
    LocatorStrategy,
    OutcomeType,
    ReplayResult,
    RiskTier,
    Step,
)
from comp_use.surface import PlaywrightSurface


def _headless() -> bool:
    return os.environ.get("COMP_USE_HEADLESS", "").lower() in ("1", "true", "yes")


def save_artifact(artifact: Artifact, artifacts_dir: Path) -> Path:
    capability_dir = Path(artifacts_dir) / artifact.capability_name
    capability_dir.mkdir(parents=True, exist_ok=True)
    path = capability_dir / f"v{artifact.version}.json"
    path.write_text(artifact.model_dump_json(indent=2))
    return path


def load_artifact(capability_name: str, artifacts_dir: Path, version: int | None = None) -> Artifact:
    capability_dir = Path(artifacts_dir) / capability_name
    if version is None:
        versions = sorted(
            int(p.stem[1:]) for p in capability_dir.glob("v*.json")
        )
        version = versions[-1]
    path = capability_dir / f"v{version}.json"
    return Artifact.model_validate_json(path.read_text())


def next_artifact_version(capability_name: str, artifacts_dir: Path) -> int:
    capability_dir = Path(artifacts_dir) / capability_name
    versions = [int(p.stem[1:]) for p in capability_dir.glob("v*.json")]
    return max(versions, default=0) + 1


def _derive_success_checkpoint(surface: PlaywrightSurface, fallback_url: str) -> Checkpoint:
    """Build a checkpoint from the final page's own heading rather than baking in
    this run's literal URL, which would only ever match a future replay that
    happens to reproduce the exact same dynamic path segments (a member ID, a
    generated confirmation/transaction number, ...)."""
    heading_text = None
    try:
        heading_text = surface.page.get_by_role("heading").first.text_content(timeout=2000)
    except Exception:
        heading_text = None
    if heading_text and heading_text.strip():
        return Checkpoint(
            type=CheckpointType.ELEMENT_VISIBLE,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": heading_text.strip()}),
        )
    print(
        "[discover] WARNING: no heading found on the final page to build a generic "
        "success checkpoint; falling back to a literal URL match, which will only "
        f"match future replays that happen to produce this exact URL again ({fallback_url!r}).",
        flush=True,
    )
    return Checkpoint(
        type=CheckpointType.URL_MATCHES,
        url_pattern=fallback_url.split("://", 1)[-1].split("/", 1)[-1],
    )


def _run_discover(args) -> None:
    import time
    from playwright.sync_api import sync_playwright

    settings = load_settings()
    guardrail = Guardrail(settings)
    run_id = f"discover_{int(time.time())}"
    evidence = EvidenceLogger(settings, guardrail, run_id=run_id)
    llm = OpenRouterClient(settings)
    transport = LocalSharedBrowserTransport()
    print(f"Discovering with {settings.openrouter_model} (max {settings.max_discovery_steps} steps)", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=_headless())
        page = browser.new_page()
        surface = PlaywrightSurface(page)
        escalation = EscalationController(evidence, transport, surface=surface)
        agent = DiscoveryAgent(
            surface, llm, guardrail, evidence, max_steps=settings.max_discovery_steps,
            escalation=escalation, confirm_risky=args.confirm_risky,
        )
        trace = agent.run(goal=args.goal, start_url=args.start_url)
        if not trace.succeeded:
            evidence.save_screenshot(surface.screenshot(), "final")
            browser.close()
        else:
            success_checkpoint = _derive_success_checkpoint(surface, fallback_url=trace.final_url)
            browser.close()

    if not trace.succeeded:
        print(f"Discovery did not reach 'finish' within {settings.max_discovery_steps} steps.")
        return

    artifact = compile_artifact(
        trace,
        capability_name=args.capability_name,
        target={"app": "mock_bank", "base_url": args.start_url.split("/member")[0]},
        success_checkpoint=success_checkpoint,
        output_schema=[],
    )
    if not any(step.action == ActionType.NAVIGATE for step in artifact.steps):
        artifact.steps.insert(
            0,
            Step(action=ActionType.NAVIGATE, target=args.start_url, risk_tier=RiskTier.SAFE),
        )
    # Never silently overwrite a previous discovery run's artifact - bump the
    # version instead, so an existing (possibly still-working) artifact isn't
    # destroyed by re-running discovery for the same capability name.
    artifact.version = next_artifact_version(args.capability_name, settings.artifacts_dir)
    path = save_artifact(artifact, settings.artifacts_dir)
    print(f"Saved artifact to {path}")


def _is_action_locator_failure(artifact: Artifact, result: ReplayResult) -> bool:
    """True only for the "the recorded action itself raised" branch of
    HARD_FAILURE (see ReplayEngine.run()'s except Exception block) - not a
    checkpoint mismatch. Drift diagnosis patches a STEP's action locator; that
    only makes sense when the action locator is what actually failed to
    resolve. A checkpoint mismatch means the action succeeded but the page
    afterward didn't look as expected - patching the action's own locator
    wouldn't address that, so we deliberately don't try."""
    if result.step_index is None or result.step_index >= len(artifact.steps):
        return False
    step = artifact.steps[result.step_index]
    return result.expected == f"{step.action.value} to succeed"


def _diagnose_and_propose_patch(settings, evidence, escalation, surface, artifact: Artifact, result: ReplayResult) -> None:
    """Best-effort, opt-in post-mortem after a HARD_FAILURE: is the control
    that failed to resolve merely drifted (renamed/moved), not genuinely gone?
    Never touches `result.outcome` - this run genuinely failed and stays
    HARD_FAILURE. If a plausible fix is found, it's saved as a NEW artifact
    version (never overwriting) and a human is escalated to review it before
    it's ever used - nothing here is auto-applied."""
    if not _is_action_locator_failure(artifact, result):
        return
    print(f"[replay] hard_failure at step {result.step_index}; diagnosing possible drift...", flush=True)
    llm = OpenRouterClient(settings)
    diagnosis = propose_drift_patch(llm, surface, artifact, result.step_index)
    if diagnosis.patched_artifact is None:
        print(
            f"[replay] drift diagnosis: no plausible replacement found ({diagnosis.reasoning or 'no reasoning given'}) "
            "- looks like a genuine failure, not drift.",
            flush=True,
        )
        evidence.log_event(
            "drift_diagnosis", {"found": False, "step_index": result.step_index, "reasoning": diagnosis.reasoning}
        )
        return

    patched = diagnosis.patched_artifact
    patched.version = next_artifact_version(artifact.capability_name, settings.artifacts_dir)
    path = save_artifact(patched, settings.artifacts_dir)
    proposed_locator = patched.steps[result.step_index].locator.model_dump(mode="json")
    evidence.log_event(
        "drift_diagnosis",
        {
            "found": True, "step_index": result.step_index, "proposed_version": patched.version,
            "proposed_locator": proposed_locator, "reasoning": diagnosis.reasoning,
        },
    )
    print(f"[replay] drift diagnosis: proposed a patch, saved as {path} (NOT active until a human approves it).", flush=True)
    escalation.escalate(
        InterventionRequest(
            run_id=evidence.run_id,
            capability_or_goal=artifact.capability_name,
            current_step=result.step_index,
            screenshot_path=evidence.save_screenshot(surface.screenshot(), "drift_diagnosis"),
            reason=f"possible drift detected at step {result.step_index} - review proposed patch "
            f"v{patched.version} at {path} before relying on it for future replays",
        )
    )
    result.proposed_patch_version = patched.version


def _run_replay(args) -> None:
    import time
    from playwright.sync_api import sync_playwright

    settings = load_settings()
    guardrail = Guardrail(settings)
    run_id = f"replay_{int(time.time())}"
    evidence = EvidenceLogger(settings, guardrail, run_id=run_id)
    transport = LocalSharedBrowserTransport()

    artifact = load_artifact(args.capability_name, settings.artifacts_dir)
    params = json.loads(args.params) if args.params else {}

    validation_error = validate_required_params(artifact, params)
    if validation_error:
        evidence.log_event("validation_error", {"detail": validation_error})
        result = ReplayResult(outcome=OutcomeType.VALIDATION_ERROR, detail=validation_error)
        print(result.model_dump_json(indent=2))
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=_headless())
        page = browser.new_page()
        surface = PlaywrightSurface(page)
        escalation = EscalationController(evidence, transport, surface=surface)
        engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)
        result = engine.run(artifact, params, confirm_risky=args.confirm_risky)
        if result.outcome != OutcomeType.SUCCESS:
            evidence.save_screenshot(surface.screenshot(), "final")
        if args.diagnose_drift_on_failure and result.outcome == OutcomeType.HARD_FAILURE:
            _diagnose_and_propose_patch(settings, evidence, escalation, surface, artifact, result)
        browser.close()

    print(result.model_dump_json(indent=2))


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="comp-use")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser("discover")
    discover_parser.add_argument("--goal", required=True)
    discover_parser.add_argument("--start-url", required=True)
    discover_parser.add_argument("--capability-name", required=True)
    discover_parser.add_argument("--confirm-risky", action="store_true")
    discover_parser.set_defaults(func=_run_discover)

    replay_parser = subparsers.add_parser("replay")
    replay_parser.add_argument("--capability-name", required=True)
    replay_parser.add_argument("--params", default="{}")
    replay_parser.add_argument("--confirm-risky", action="store_true")
    replay_parser.add_argument(
        "--diagnose-drift-on-failure", action="store_true",
        help="On hard_failure, ask a vision model whether the target control just moved/renamed "
        "(drift) and, if so, propose a patched artifact as a new version for human review. "
        "Never applied automatically; this run's own outcome is unaffected.",
    )
    replay_parser.set_defaults(func=_run_replay)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
