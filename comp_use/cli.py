import argparse
import json
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

from comp_use.config import Settings, load_settings
from comp_use.discovery.agent import DiscoveryAgent
from comp_use.discovery.compiler import compile_artifact
from comp_use.drift import propose_drift_patch
from comp_use.escalation.controller import EscalationController
from comp_use.escalation.transport import LocalSharedBrowserTransport
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.llm_client import FallbackLLMClient, LLMClient, NvidiaNimClient, OpenRouterClient
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
from comp_use.surface import PlaywrightSurface, safe_screenshot


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
            (int(p.stem[1:]) for p in capability_dir.glob("v*.json")), reverse=True
        )
        if not versions:
            raise FileNotFoundError(
                f"no artifact found for capability '{capability_name}' in {capability_dir} "
                "(check --capability-name for a typo, or run 'discover' first)"
            )
        for candidate_version in versions:
            candidate = Artifact.model_validate_json(
                (capability_dir / f"v{candidate_version}.json").read_text()
            )
            if candidate.status == "approved":
                return candidate
        raise FileNotFoundError(
            f"capability '{capability_name}' has {len(versions)} version(s) in {capability_dir} "
            "but none are approved (approve one via `comp-use approve --capability-name "
            f"{capability_name} --version N`, or pass --version explicitly to load a draft)"
        )
    path = capability_dir / f"v{version}.json"
    return Artifact.model_validate_json(path.read_text())


def approve_artifact(capability_name: str, version: int, artifacts_dir: Path) -> Artifact:
    artifact = load_artifact(capability_name, artifacts_dir, version=version)
    if artifact.status != "draft":
        raise ValueError(f"version {version} is '{artifact.status}', not 'draft' - only a draft can be approved")
    artifact.status = "approved"
    save_artifact(artifact, artifacts_dir)
    return artifact


def reject_artifact(capability_name: str, version: int, artifacts_dir: Path) -> Artifact:
    artifact = load_artifact(capability_name, artifacts_dir, version=version)
    if artifact.status != "draft":
        raise ValueError(f"version {version} is '{artifact.status}', not 'draft' - only a draft can be rejected")
    artifact.status = "rejected"
    save_artifact(artifact, artifacts_dir)
    return artifact


def retire_artifact(capability_name: str, version: int, artifacts_dir: Path) -> Artifact:
    artifact = load_artifact(capability_name, artifacts_dir, version=version)
    if artifact.status != "approved":
        raise ValueError(f"version {version} is '{artifact.status}', not 'approved' - only an approved version can be retired")
    artifact.status = "rejected"
    save_artifact(artifact, artifacts_dir)
    return artifact


def next_artifact_version(capability_name: str, artifacts_dir: Path) -> int:
    capability_dir = Path(artifacts_dir) / capability_name
    versions = [int(p.stem[1:]) for p in capability_dir.glob("v*.json")]
    return max(versions, default=0) + 1


# Guards the read-next-version-then-save sequence used by run_discover() and
# _diagnose_and_propose_patch(). A single process-wide lock (rather than a
# per-capability-name registry) is deliberate: discover/drift-diagnosis runs
# are slow, infrequent, LLM-driven browser sessions, so serializing their
# version bumps process-wide costs nothing meaningful, and it avoids the
# added complexity of a per-name lock registry. Without this, two concurrent
# discover requests for the same capability name (now reachable via the
# capability server's HTTP API, one thread per request) could both read the
# same "next version" number and one save would silently clobber the
# other's artifact. NEVER hold this lock across a blocking call (e.g. the
# interactive approval `input()` prompt in run_discover) - that would let
# one stuck human input block all other discover/version-allocation
# activity process-wide.
_version_lock = threading.Lock()


def _build_llm_client(settings: Settings) -> LLMClient:
    """MODEL_PROVIDER picks which provider is tried first; the other is used
    as an automatic fallback only if its own API key is configured (see
    FallbackLLMClient). If the primary provider's own key isn't configured,
    there's nothing usable to put first, so fall back to whichever provider
    does have a key rather than build a client guaranteed to fail on first
    use."""
    openrouter = OpenRouterClient(settings)
    nvidia = NvidiaNimClient(settings)

    if settings.model_provider == "nvidia" and settings.nvidia_api_key:
        primary, fallback, fallback_key = nvidia, openrouter, settings.openrouter_api_key
    else:
        primary, fallback, fallback_key = openrouter, nvidia, settings.nvidia_api_key

    if fallback_key:
        return FallbackLLMClient(primary, fallback)
    return primary


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


def run_discover(
    goal: str, start_url: str, capability_name: str, confirm_risky: bool,
    transport, interactive: bool = True,
) -> Artifact | None:
    import time
    from playwright.sync_api import sync_playwright

    settings = load_settings()
    guardrail = Guardrail(settings)
    run_id = f"discover_{int(time.time())}"
    evidence = EvidenceLogger(settings, guardrail, run_id=run_id)
    llm = _build_llm_client(settings)
    if settings.model_provider == "nvidia" and settings.nvidia_api_key:
        primary_label, primary_model = "NVIDIA NIM", settings.nvidia_model
        fallback_label, fallback_model, fallback_key = "OpenRouter", settings.openrouter_model, settings.openrouter_api_key
    else:
        primary_label, primary_model = "OpenRouter", settings.openrouter_model
        fallback_label, fallback_model, fallback_key = "NVIDIA NIM", settings.nvidia_model, settings.nvidia_api_key
    fallback_note = f" (falls back to {fallback_label}:{fallback_model} on failure)" if fallback_key else ""
    print(
        f"Discovering with {primary_label}:{primary_model}{fallback_note} (max {settings.max_discovery_steps} steps)",
        flush=True,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=_headless())
        page = browser.new_page()
        surface = PlaywrightSurface(page)
        escalation = EscalationController(evidence, transport, surface=surface)
        agent = DiscoveryAgent(
            surface, llm, guardrail, evidence, max_steps=settings.max_discovery_steps,
            escalation=escalation, confirm_risky=confirm_risky,
        )
        trace = agent.run(goal=goal, start_url=start_url)
        if not trace.succeeded:
            evidence.save_screenshot(safe_screenshot(surface), "final")
            browser.close()
        else:
            success_checkpoint = _derive_success_checkpoint(surface, fallback_url=trace.final_url)
            browser.close()

    if not trace.succeeded:
        print(f"Discovery did not reach 'finish' within {settings.max_discovery_steps} steps.")
        return None

    artifact = compile_artifact(
        trace,
        capability_name=capability_name,
        target={"app": "mock_bank", "base_url": start_url.split("/member")[0]},
        success_checkpoint=success_checkpoint,
        output_schema=[],
    )
    if not any(step.action == ActionType.NAVIGATE for step in artifact.steps):
        artifact.steps.insert(
            0,
            Step(action=ActionType.NAVIGATE, target=start_url, risk_tier=RiskTier.SAFE),
        )
    artifact.status = "draft"

    # Ask for approval *before* touching version numbers so the version-lock
    # below never has to be held across this blocking call - see the comment
    # on _version_lock.
    if interactive:
        try:
            answer = input("Approve as new default? [y/N]: ").strip().lower()
        except (EOFError, OSError):
            # OSError covers pytest's captured-stdin guard (and any other
            # environment where stdin isn't readable) - treat it the same as
            # EOFError: no answer given, artifact stays a draft.
            answer = ""
        if answer == "y":
            artifact.status = "approved"

    # Never silently overwrite a previous discovery run's artifact - bump the
    # version instead, so an existing (possibly still-working) artifact isn't
    # destroyed by re-running discovery for the same capability name. The
    # version read and the save must happen atomically with respect to other
    # threads (e.g. concurrent HTTP discover requests for the same capability
    # via the capability server), otherwise two threads can compute the same
    # "next version" and the second save silently clobbers the first's
    # artifact - see _version_lock.
    with _version_lock:
        artifact.version = next_artifact_version(capability_name, settings.artifacts_dir)
        # A fresh discovery run has no way to observe outcome_patterns - they're
        # hand-authored from watching real business/recoverable outcomes, not
        # something the agent infers from a single successful trace. Without this,
        # every re-discovery silently drops any hand-authored patterns from the
        # previous version, since replay always loads the latest version.
        if artifact.version > 1:
            try:
                prev_artifact = load_artifact(capability_name, settings.artifacts_dir, version=artifact.version - 1)
                if prev_artifact.outcome_patterns:
                    artifact.outcome_patterns = prev_artifact.outcome_patterns
                    print(
                        f"[discover] carried forward {len(prev_artifact.outcome_patterns)} "
                        f"outcome_patterns(s) from v{prev_artifact.version}",
                        flush=True,
                    )
            except (FileNotFoundError, ValueError):
                pass

        path = save_artifact(artifact, settings.artifacts_dir)

    print(f"Saved artifact to {path} (status={artifact.status})")
    return artifact


def _run_discover(args) -> None:
    transport = LocalSharedBrowserTransport()
    run_discover(
        goal=args.goal, start_url=args.start_url, capability_name=args.capability_name,
        confirm_risky=args.confirm_risky, transport=transport, interactive=True,
    )


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
    llm = _build_llm_client(settings)
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
    # Same version-clobbering race as run_discover() - see _version_lock.
    # Currently unreachable concurrently via the HTTP API (invoke always
    # passes diagnose_drift_on_failure=False), but guarding it costs nothing
    # and keeps this path consistent if that ever changes.
    with _version_lock:
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
            screenshot_path=evidence.save_screenshot(safe_screenshot(surface), "drift_diagnosis"),
            reason=f"possible drift detected at step {result.step_index} - review proposed patch "
            f"v{patched.version} at {path} before relying on it for future replays",
        )
    )
    result.proposed_patch_version = patched.version


def run_replay(
    capability_name: str, params: dict, confirm_risky: bool,
    diagnose_drift_on_failure: bool, transport,
) -> ReplayResult:
    import time
    from playwright.sync_api import sync_playwright

    settings = load_settings()
    guardrail = Guardrail(settings)
    run_id = f"replay_{int(time.time())}"
    evidence = EvidenceLogger(settings, guardrail, run_id=run_id)

    artifact = load_artifact(capability_name, settings.artifacts_dir)

    validation_error = validate_required_params(artifact, params)
    if validation_error:
        evidence.log_event("validation_error", {"detail": validation_error})
        return ReplayResult(outcome=OutcomeType.VALIDATION_ERROR, detail=validation_error)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=_headless())
        page = browser.new_page()
        surface = PlaywrightSurface(page)
        escalation = EscalationController(evidence, transport, surface=surface)
        engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)
        result = engine.run(artifact, params, confirm_risky=confirm_risky)
        if result.outcome != OutcomeType.SUCCESS:
            evidence.save_screenshot(safe_screenshot(surface), "final")
        if diagnose_drift_on_failure and result.outcome == OutcomeType.HARD_FAILURE:
            _diagnose_and_propose_patch(settings, evidence, escalation, surface, artifact, result)
        browser.close()

    return result


def _run_replay(args) -> None:
    transport = LocalSharedBrowserTransport()
    params = json.loads(args.params) if args.params else {}
    result = run_replay(
        capability_name=args.capability_name, params=params, confirm_risky=args.confirm_risky,
        diagnose_drift_on_failure=args.diagnose_drift_on_failure, transport=transport,
    )
    print(result.model_dump_json(indent=2))


def _run_approve(args) -> None:
    settings = load_settings()
    artifact = approve_artifact(args.capability_name, args.version, settings.artifacts_dir)
    print(f"Approved {args.capability_name} v{artifact.version}")


def _run_serve(args) -> None:
    import uvicorn
    from comp_use.server.app import create_app

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port)


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

    approve_parser = subparsers.add_parser("approve")
    approve_parser.add_argument("--capability-name", required=True)
    approve_parser.add_argument("--version", type=int, required=True)
    approve_parser.set_defaults(func=_run_approve)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.set_defaults(func=_run_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
