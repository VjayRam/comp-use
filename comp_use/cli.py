import argparse
import contextlib
import json
import os
import shutil
import threading
from pathlib import Path
from urllib.parse import urlparse

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
from comp_use.pg import store as pg_store
from comp_use.replay.engine import ReplayEngine, validate_required_params
from comp_use.schemas import (
    ActionType,
    Artifact,
    Checkpoint,
    CheckpointType,
    DerivedOutputSpec,
    InterventionRequest,
    Locator,
    LocatorStrategy,
    OutcomeType,
    OutputParam,
    ReplayResult,
    RiskTier,
    Step,
)
from comp_use.surface import PlaywrightSurface, Surface, safe_screenshot


def _headless() -> bool:
    return os.environ.get("COMP_USE_HEADLESS", "").lower() in ("1", "true", "yes")


def _use_sandbox() -> bool:
    return os.environ.get("COMP_USE_SANDBOX", "").lower() in ("1", "true", "yes")


@contextlib.contextmanager
def _browser_session(start_url: str, sandbox: bool):
    """Yields (page, novnc_url). novnc_url is None unless sandbox=True.

    Two modes, chosen by the caller (CLI --sandbox flag / COMP_USE_SANDBOX env var):
    - Local (default, unchanged from before): a Chromium process launched directly on
      this machine, headed or headless per COMP_USE_HEADLESS. Fast, no Docker
      dependency - what every existing test and the original take-home path use.
    - Sandbox: one ephemeral Docker container (comp_use/sandbox.py, adapted from
      Project-Hawkeye's hawkeye_sandbox) running a headed Chromium behind noVNC, driven
      here over CDP. Lets a human watch (and, during escalation, actually control - see
      sandbox_image/supervisord.conf) the exact live session from a browser tab via the
      returned novnc_url, instead of needing a native window on this machine - the
      container-per-run lifetime is what makes "take over the SAME live session, not a
      fresh one" (§3.6) still true when the browser isn't running somewhere you can
      just look at.
    """
    from playwright.sync_api import sync_playwright

    if not sandbox:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=_headless())
            try:
                yield browser.new_page(), None
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
        return

    from comp_use.sandbox import SandboxConfig, spawn, stop

    handle = spawn(SandboxConfig(url=start_url))
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(handle.cdp_url)
        try:
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            yield page, handle.novnc_url
        finally:
            try:
                browser.close()
            except Exception:
                pass
            stop(handle)


def save_artifact(artifact: Artifact, artifacts_dir: Path) -> Path | str:
    # Postgres is the primary store when COMP_USE_DB_URL is configured (see
    # comp_use/pg/schema.sql's artifacts table) - artifacts_dir is only used as the
    # fallback on-disk layout for local dev/tests without a DB. This was a deliberate
    # switch: every discover run used to leave a new untracked JSON file under
    # artifacts/<name>/vN.json that nothing ever cleaned up.
    if pg_store.db_enabled():
        pg_store.save_artifact(
            artifact.capability_name, artifact.version, artifact.status,
            artifact.model_dump(mode="json"), artifact.created_from_run_id,
        )
        return f"postgres:artifacts/{artifact.capability_name}/v{artifact.version}"
    capability_dir = Path(artifacts_dir) / artifact.capability_name
    capability_dir.mkdir(parents=True, exist_ok=True)
    path = capability_dir / f"v{artifact.version}.json"
    path.write_text(artifact.model_dump_json(indent=2))
    return path


def load_artifact(capability_name: str, artifacts_dir: Path, version: int | None = None) -> Artifact:
    if pg_store.db_enabled():
        if version is None:
            data = pg_store.latest_approved_artifact(capability_name)
            if data is None:
                raise FileNotFoundError(
                    f"no approved artifact found for capability '{capability_name}' in Postgres "
                    "(check --capability-name for a typo, run 'discover' first, or approve a draft "
                    f"via `comp-use approve --capability-name {capability_name} --version N`)"
                )
            return Artifact.model_validate(data)
        data = pg_store.load_artifact(capability_name, version)
        if data is None:
            raise FileNotFoundError(f"no artifact found for capability '{capability_name}' version {version} in Postgres")
        return Artifact.model_validate(data)

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
        approved = []
        for candidate_version in versions:
            candidate = Artifact.model_validate_json(
                (capability_dir / f"v{candidate_version}.json").read_text()
            )
            if candidate.status == "approved":
                approved.append(candidate)
        if approved:
            # Prefer the version explicitly marked is_default (see
            # set_default_version()); otherwise fall back to the highest version
            # number - `versions` is sorted descending, so approved[0] already is
            # that, same as the behavior before is_default existed.
            return next((a for a in approved if a.is_default), approved[0])
        raise FileNotFoundError(
            f"capability '{capability_name}' has {len(versions)} version(s) in {capability_dir} "
            "but none are approved (approve one via `comp-use approve --capability-name "
            f"{capability_name} --version N`, or pass --version explicitly to load a draft)"
        )
    path = capability_dir / f"v{version}.json"
    return Artifact.model_validate_json(path.read_text())


def approve_artifact(capability_name: str, version: int, artifacts_dir: Path) -> Artifact:
    artifact = load_artifact(capability_name, artifacts_dir, version=version)
    # A "retired" version was live in production once and was deliberately
    # withdrawn (not rejected outright) - re-approving it is the rollback path
    # is_default exists for, so it's allowed here alongside the normal
    # draft-to-approved flow. A "rejected" draft was never approved and stays
    # a one-way door.
    if artifact.status not in ("draft", "retired"):
        raise ValueError(
            f"version {version} is '{artifact.status}', not 'draft' or 'retired' - "
            "only a draft or a retired version can be approved"
        )
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
    artifact.status = "retired"
    save_artifact(artifact, artifacts_dir)
    return artifact


def list_capability_names(artifacts_dir: Path) -> list[str]:
    if pg_store.db_enabled():
        return pg_store.list_capability_names()
    artifacts_dir = Path(artifacts_dir)
    if not artifacts_dir.exists():
        return []
    return sorted(p.name for p in artifacts_dir.iterdir() if p.is_dir())


def list_versions(capability_name: str, artifacts_dir: Path) -> list[int]:
    if pg_store.db_enabled():
        return sorted(v["version"] for v in pg_store.list_artifact_versions(capability_name))
    capability_dir = Path(artifacts_dir) / capability_name
    if not capability_dir.exists():
        return []
    return sorted(int(p.stem[1:]) for p in capability_dir.glob("v*.json"))


def set_default_version(capability_name: str, version: int, artifacts_dir: Path) -> Artifact:
    """Marks `version` as the one an unpinned invoke/replay picks (see
    load_artifact's version=None branch). Only one version per capability may be
    default at a time - any other version currently marked is cleared first. Only
    an approved version can become the default; a draft or a retired/rejected
    version being invocable-by-default at all would defeat the point of those
    statuses gating what's live."""
    artifact = load_artifact(capability_name, artifacts_dir, version=version)
    if artifact.status != "approved":
        raise ValueError(
            f"version {version} is '{artifact.status}', not 'approved' - only an "
            "approved version can be set as the default"
        )
    for other_version in list_versions(capability_name, artifacts_dir):
        if other_version == version:
            continue
        other = load_artifact(capability_name, artifacts_dir, version=other_version)
        if other.is_default:
            other.is_default = False
            save_artifact(other, artifacts_dir)
    artifact.is_default = True
    save_artifact(artifact, artifacts_dir)
    return artifact


def clear_default_version(capability_name: str, version: int, artifacts_dir: Path) -> Artifact:
    """The opposite of set_default_version: reverts `version` to NOT being the
    explicit default, so unpinned resolution falls back to its original behavior -
    "highest approved version wins" (see load_artifact's version=None branch) -
    rather than moving the default to some other specific version. A no-op (not
    an error) if `version` isn't currently the default, so a caller never needs
    to check first."""
    artifact = load_artifact(capability_name, artifacts_dir, version=version)
    if artifact.is_default:
        artifact.is_default = False
        save_artifact(artifact, artifacts_dir)
    return artifact


def delete_capability(capability_name: str, artifacts_dir: Path) -> int:
    """Deletes every version of a capability. Run history (runs/run_events/
    run_screenshots, or evidence/<run_id>/... on disk) is untouched - runs are
    keyed on run_id, never on capability_name+version, so a capability's run
    history survives its artifact being deleted, in both storage modes."""
    if pg_store.db_enabled():
        return pg_store.delete_capability(capability_name)
    capability_dir = Path(artifacts_dir) / capability_name
    if not capability_dir.exists():
        return 0
    versions = list(capability_dir.glob("v*.json"))
    shutil.rmtree(capability_dir)
    return len(versions)


def next_artifact_version(capability_name: str, artifacts_dir: Path) -> int:
    if pg_store.db_enabled():
        versions = [v["version"] for v in pg_store.list_artifact_versions(capability_name)]
        return max(versions, default=0) + 1
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


def build_llm_client(settings: Settings) -> LLMClient:
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


def _derive_success_checkpoint(surface: Surface, fallback_url: str) -> Checkpoint:
    """Build a checkpoint from the final page's own heading rather than baking in
    this run's literal URL, which would only ever match a future replay that
    happens to reproduce the exact same dynamic path segments (a member ID, a
    generated confirmation/transaction number, ...).

    Goes through Surface.get_heading_text() rather than reaching into a
    PlaywrightSurface's underlying `.page` directly - this function used to be
    typed on PlaywrightSurface and call `surface.page.get_by_role(...)` itself,
    which meant discovery's checkpoint derivation couldn't work against any future
    non-Playwright Surface (e.g. a desktop implementation) without being rewritten.
    Depending only on the abstract Surface interface keeps this generic."""
    heading_text = surface.get_heading_text()
    if heading_text:
        return Checkpoint(
            type=CheckpointType.ELEMENT_VISIBLE,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": heading_text}),
        )
    path = fallback_url.split("://", 1)[-1].split("/", 1)[-1]
    # A purely-numeric path segment is almost always a dynamic ID (a member number,
    # here) rather than part of the route itself - wildcard it before falling back to
    # a literal match, so e.g. "members/102777/hold/review" becomes
    # "members/*/hold/review" and generalizes across every member, not just the one
    # seen during this discovery run. See EXT_TASK_FIXES.md #4: without this, a page
    # with no heading (so no generic ELEMENT_VISIBLE checkpoint is possible) produced
    # an artifact whose success_checkpoint could only ever verify against the exact
    # member discovered against.
    generalized_path = "/".join("*" if segment.isdigit() else segment for segment in path.split("/"))
    if generalized_path != path:
        print(
            f"[discover] no heading found on the final page; falling back to a "
            f"wildcarded URL match ({generalized_path!r}) with numeric segments "
            f"generalized so future replays for a different record still verify.",
            flush=True,
        )
    else:
        print(
            "[discover] WARNING: no heading found on the final page to build a generic "
            "success checkpoint; falling back to a literal URL match, which will only "
            f"match future replays that happen to produce this exact URL again ({fallback_url!r}).",
            flush=True,
        )
    return Checkpoint(type=CheckpointType.URL_MATCHES, url_pattern=generalized_path)


def _settings_with_target_allowlisted(settings: Settings, url: str) -> Settings:
    """A capability's discovery/replay must always be allowed to reach the exact
    site it targets, whether or not that site happens to be in the server's own
    configured ALLOWED_URL_PREFIXES - multiple capabilities recorded against
    different target sites need to coexist on one running server without a
    restart. Returns a per-run Settings copy; the passed-in settings (and any
    other in-flight run's settings) are never mutated."""
    if not url:
        return settings
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return settings
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin in settings.allowed_url_prefixes:
        return settings
    return settings.model_copy(update={"allowed_url_prefixes": [*settings.allowed_url_prefixes, origin]})


def run_discover(
    goal: str, start_url: str, capability_name: str, confirm_risky: bool,
    transport, interactive: bool = True, param_hints: list[str] | None = None,
    derived_outputs: list[OutputParam] | None = None, sandbox: bool | None = None,
    run_id: str | None = None,
) -> Artifact | None:
    import time

    sandbox = _use_sandbox() if sandbox is None else sandbox
    settings = load_settings()
    # Discovery must be allowed to navigate to whatever start_url the caller gave it,
    # even if the server's own ALLOWED_URL_PREFIXES is scoped to a different target
    # site (e.g. running discovery against a second site without restarting the
    # server). Union the requested origin into the allowlist for this run only -
    # global settings/other in-flight runs are untouched.
    settings = _settings_with_target_allowlisted(settings, start_url)
    guardrail = Guardrail(settings)
    # A caller driving this through the capability server (RunManager) already
    # minted a run_id the frontend is polling against - reuse it so this run's
    # own evidence/Postgres records land under the same id, instead of each
    # generating an independent timestamp id that never match up (see
    # EXT_TASK_FIXES.md - this was a live, reproducible dashboard bug: the events
    # panel showed nothing because it polled the wrong run_id's evidence).
    run_id = run_id or f"discover_{int(time.time())}"
    evidence = EvidenceLogger(settings, guardrail, run_id=run_id)
    pg_store.start_run(run_id, kind="discover", capability_name=capability_name, goal=goal)
    llm = build_llm_client(settings)
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

    # Everything below can raise (browser/CDP failure, an LLM call inside agent.run(),
    # _derive_success_checkpoint, artifact compilation) - without this try/except, any
    # such exception propagated straight out of run_discover, skipping finish_run()
    # entirely and leaving the run's Postgres row (and hence the dashboard) stuck at
    # status='running' forever, indistinguishable from a run still genuinely in
    # progress. Re-raised after recording so the caller (RunManager.worker) still sees
    # the real exception and sets its own in-memory record to status='error' too.
    try:
        with _browser_session(start_url, sandbox) as (page, novnc_url):
            if novnc_url:
                evidence.log_event("sandbox_started", {"novnc_url": novnc_url})
                pg_store.set_novnc_url(run_id, novnc_url, container_name="")
                transport.on_novnc_url(novnc_url)
            # _browser_session's own finally (browser.close(), and stop() in sandbox mode)
            # already guarantees cleanup even if agent.run() raises - e.g. a non-interactive-
            # stdin RuntimeError from an escalation - so nothing here needs its own
            # try/finally the way the old inline sync_playwright block did.
            surface = PlaywrightSurface(page)
            escalation = EscalationController(evidence, transport, surface=surface)
            agent = DiscoveryAgent(
                surface, llm, guardrail, evidence, max_steps=settings.max_discovery_steps,
                escalation=escalation, confirm_risky=confirm_risky,
            )
            trace = agent.run(goal=goal, start_url=start_url, param_hints=param_hints)
            if not trace.succeeded:
                evidence.save_screenshot(safe_screenshot(surface), "final")
            else:
                success_checkpoint = _derive_success_checkpoint(surface, fallback_url=trace.final_url)

        if not trace.succeeded:
            print(f"Discovery did not reach 'finish' within {settings.max_discovery_steps} steps.")
            pg_store.finish_run(run_id, status="failed", result={"succeeded": False})
            return None

        # Derive the target descriptor from start_url's own host/scheme rather than a
        # hardcoded "mock_bank" label + a "/member"-split base_url - both were written for
        # the original take-home's mock app specifically and silently produced wrong
        # values (a leftover mock_bank label, and a base_url that still included the path,
        # e.g. ".../signon") against MERIDIAN CORE, whose start_url has no "/member"
        # segment to split on. See EXT_TASK_FIXES.md #8.
        parsed_start_url = urlparse(start_url)
        artifact = compile_artifact(
            trace,
            capability_name=capability_name,
            target={
                "app": parsed_start_url.hostname or start_url,
                "base_url": f"{parsed_start_url.scheme}://{parsed_start_url.netloc}",
            },
            success_checkpoint=success_checkpoint,
            output_schema=derived_outputs or [],
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
        pg_store.finish_run(
            run_id, status="done",
            result={"succeeded": True, "artifact_version": artifact.version, "artifact_status": artifact.status},
        )
        return artifact
    except Exception as exc:
        pg_store.finish_run(run_id, status="error", result={"error": f"{type(exc).__name__}: {exc}"})
        raise


def _run_discover(args) -> None:
    transport = LocalSharedBrowserTransport()
    param_hint = getattr(args, "param_hint", None)
    param_hints = [p.strip() for p in param_hint.split(",") if p.strip()] if param_hint else None
    derived_outputs = None
    derive_sum_output = getattr(args, "derive_sum_output", None)
    if derive_sum_output:
        derived_outputs = []
        for spec in derive_sum_output:
            name, _, source = spec.partition(":")
            derived_outputs.append(
                OutputParam(name=name, type="string", derive=DerivedOutputSpec(from_output=source))
            )
    run_discover(
        goal=args.goal, start_url=args.start_url, capability_name=args.capability_name,
        confirm_risky=args.confirm_risky, transport=transport, interactive=True,
        param_hints=param_hints, derived_outputs=derived_outputs,
        sandbox=getattr(args, "sandbox", None),
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
    llm = build_llm_client(settings)
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
    try:
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
    except Exception as exc:
        # This whole diagnosis pass is best-effort (§9: --diagnose-drift-on-failure is
        # optional and beyond the assignment's requirements) - a transport failure here
        # (e.g. non-interactive stdin) must not crash run_replay() and lose the real,
        # already-computed `result`. The patch is already saved to disk regardless
        # (above); only the "notify a human to review it now" step failed.
        error_detail = f"{type(exc).__name__}: {exc}"
        print(
            f"[replay] drift diagnosis: patch v{patched.version} saved, but escalating for review failed "
            f"({error_detail}) - review it manually, e.g. `comp-use approve --capability-name "
            f"{artifact.capability_name} --version {patched.version}`.",
            flush=True,
        )
        evidence.log_event(
            "escalation_transport_failed",
            {"reason": "drift_diagnosis_review", "proposed_version": patched.version, "error": error_detail},
        )
        return
    result.proposed_patch_version = patched.version


def run_replay(
    capability_name: str, params: dict, confirm_risky: bool,
    diagnose_drift_on_failure: bool, transport, version: int | None = None,
    sandbox: bool | None = None, run_id: str | None = None,
) -> ReplayResult:
    import time

    sandbox = _use_sandbox() if sandbox is None else sandbox
    settings = load_settings()
    artifact = load_artifact(capability_name, settings.artifacts_dir, version=version)
    # This artifact was recorded against artifact.target["base_url"] - replay must be
    # allowed to reach that exact site even if the server's own ALLOWED_URL_PREFIXES
    # is scoped to a different capability's target (see _settings_with_target_allowlisted).
    settings = _settings_with_target_allowlisted(settings, artifact.target.get("base_url", ""))
    guardrail = Guardrail(settings)
    # See the matching comment in run_discover() - reuse the caller's run_id
    # (from RunManager, when invoked through the capability server) so evidence/
    # Postgres records land under the same id the frontend is polling.
    run_id = run_id or f"replay_{int(time.time())}"
    evidence = EvidenceLogger(settings, guardrail, run_id=run_id)
    pg_store.start_run(run_id, kind="replay", capability_name=capability_name, goal=None)
    # load_artifact(version=None) already guarantees "approved" (it walks versions
    # newest-first and returns the first approved one). An explicitly-pinned version
    # gets no such guarantee - it loads that exact file regardless of status - so a
    # pinned draft/rejected version must be rejected here, before any browser opens.
    if version is not None and artifact.status != "approved":
        detail = (
            f"version {version} of '{capability_name}' is '{artifact.status}', not "
            "'approved' - only an approved version can be replayed/invoked"
        )
        evidence.log_event("version_not_approved", {"detail": detail})
        pg_store.finish_run(run_id, status="error", result={"detail": detail})
        raise ValueError(detail)

    validation_error = validate_required_params(artifact, params)
    if validation_error:
        evidence.log_event("validation_error", {"detail": validation_error})
        pg_store.finish_run(run_id, status="validation_error", result={"detail": validation_error})
        return ReplayResult(outcome=OutcomeType.VALIDATION_ERROR, detail=validation_error)

    # Replay has no explicit start_url param (it's baked into the artifact's own first
    # NAVIGATE step) - use the artifact's own recorded base_url as the sandbox's initial
    # page so a human watching via noVNC sees something meaningful immediately, rather
    # than a blank tab until the first recorded navigate runs.
    sandbox_start_url = artifact.target.get("base_url", "about:blank") if sandbox else "about:blank"
    # Same reasoning as run_discover()'s matching try/except: without this, an
    # exception from engine.run(), a screenshot, or drift diagnosis propagated
    # straight out of run_replay, skipping finish_run() and leaving the run's
    # Postgres row stuck at status='running' forever even though the browser
    # session (and its real ReplayResult, if one was computed) is long gone.
    try:
        with _browser_session(sandbox_start_url, sandbox) as (page, novnc_url):
            if novnc_url:
                evidence.log_event("sandbox_started", {"novnc_url": novnc_url})
                pg_store.set_novnc_url(run_id, novnc_url, container_name="")
                transport.on_novnc_url(novnc_url)
            surface = PlaywrightSurface(page)
            escalation = EscalationController(evidence, transport, surface=surface)
            engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)
            result = engine.run(artifact, params, confirm_risky=confirm_risky)
            if result.outcome != OutcomeType.SUCCESS:
                evidence.save_screenshot(safe_screenshot(surface), "final")
            if diagnose_drift_on_failure and result.outcome == OutcomeType.HARD_FAILURE:
                _diagnose_and_propose_patch(settings, evidence, escalation, surface, artifact, result)

        pg_store.finish_run(run_id, status=result.outcome.value, result=result.model_dump(mode="json"))
        return result
    except Exception as exc:
        pg_store.finish_run(run_id, status="error", result={"error": f"{type(exc).__name__}: {exc}"})
        raise


def _run_replay(args) -> None:
    transport = LocalSharedBrowserTransport()
    params = json.loads(args.params) if args.params else {}
    result = run_replay(
        capability_name=args.capability_name, params=params, confirm_risky=args.confirm_risky,
        diagnose_drift_on_failure=args.diagnose_drift_on_failure, transport=transport,
        version=args.version, sandbox=getattr(args, "sandbox", None),
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
    discover_parser.add_argument(
        "--param-hint", default=None,
        help="Comma-separated list of value concepts that MUST become goal_parameters "
        "if touched (e.g. 'member_number,amount,memo') - removes ambiguity for values "
        "the operator already knows should vary per call.",
    )
    discover_parser.add_argument(
        "--derive-sum-output", action="append", default=None, metavar="NAME:SOURCE_OUTPUT",
        help="Add a computed output that sums every $X,XXX.XX found in another "
        "output's extracted text (e.g. 'total_balance:shares_balances_table'). "
        "Repeatable. For pages that list line items with no total of their own.",
    )
    discover_parser.add_argument(
        "--sandbox", action="store_true", default=None,
        help="Run the browser in an isolated Docker sandbox (comp_use/sandbox.py) with a "
        "noVNC URL to watch/control it, instead of launching Chromium directly on this "
        "machine. Defaults to the COMP_USE_SANDBOX env var if this flag is omitted.",
    )
    discover_parser.set_defaults(func=_run_discover)

    replay_parser = subparsers.add_parser("replay")
    replay_parser.add_argument("--capability-name", required=True)
    replay_parser.add_argument("--params", default="{}")
    replay_parser.add_argument("--confirm-risky", action="store_true")
    replay_parser.add_argument(
        "--version", type=int, default=None,
        help="Replay this specific artifact version instead of the latest approved one. "
        "Must itself be 'approved' - a draft or rejected version raises an error and no "
        "replay is attempted.",
    )
    replay_parser.add_argument(
        "--diagnose-drift-on-failure", action="store_true",
        help="On hard_failure, ask a vision model whether the target control just moved/renamed "
        "(drift) and, if so, propose a patched artifact as a new version for human review. "
        "Never applied automatically; this run's own outcome is unaffected.",
    )
    replay_parser.add_argument(
        "--sandbox", action="store_true", default=None,
        help="Run the browser in an isolated Docker sandbox with a noVNC URL to watch/"
        "control it, instead of launching Chromium directly. Defaults to the "
        "COMP_USE_SANDBOX env var if this flag is omitted.",
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
