import argparse
import json
from pathlib import Path

from comp_use.config import load_settings
from comp_use.discovery.agent import DiscoveryAgent
from comp_use.discovery.compiler import compile_artifact
from comp_use.escalation.controller import EscalationController
from comp_use.escalation.transport import LocalSharedBrowserTransport
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.llm_client import OpenRouterClient
from comp_use.replay.engine import ReplayEngine
from comp_use.schemas import Artifact, Checkpoint, CheckpointType, Locator, LocatorStrategy
from comp_use.surface import PlaywrightSurface


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


def _run_discover(args) -> None:
    import time
    from playwright.sync_api import sync_playwright

    settings = load_settings()
    guardrail = Guardrail(settings)
    run_id = f"discover_{int(time.time())}"
    evidence = EvidenceLogger(settings, guardrail, run_id=run_id)
    llm = OpenRouterClient(settings)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        surface = PlaywrightSurface(page)
        agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=settings.max_discovery_steps)
        trace = agent.run(goal=args.goal, start_url=args.start_url)
        browser.close()

    if not trace.succeeded:
        print(f"Discovery did not reach 'finish' within {settings.max_discovery_steps} steps.")
        return

    success_checkpoint = Checkpoint(
        type=CheckpointType.URL_MATCHES,
        url_pattern=trace.final_url.split("://", 1)[-1].split("/", 1)[-1],
    )
    artifact = compile_artifact(
        trace,
        capability_name=args.capability_name,
        target={"app": "mock_bank", "base_url": args.start_url.split("/member")[0]},
        success_checkpoint=success_checkpoint,
        output_schema=[],
    )
    path = save_artifact(artifact, settings.artifacts_dir)
    print(f"Saved artifact to {path}")


def _run_replay(args) -> None:
    import time
    from playwright.sync_api import sync_playwright

    settings = load_settings()
    guardrail = Guardrail(settings)
    run_id = f"replay_{int(time.time())}"
    evidence = EvidenceLogger(settings, guardrail, run_id=run_id)
    transport = LocalSharedBrowserTransport()
    escalation = EscalationController(evidence, transport)

    artifact = load_artifact(args.capability_name, settings.artifacts_dir)
    params = json.loads(args.params) if args.params else {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        surface = PlaywrightSurface(page)
        engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)
        result = engine.run(artifact, params, confirm_risky=args.confirm_risky)
        browser.close()

    print(result.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="comp-use")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser("discover")
    discover_parser.add_argument("--goal", required=True)
    discover_parser.add_argument("--start-url", required=True)
    discover_parser.add_argument("--capability-name", required=True)
    discover_parser.set_defaults(func=_run_discover)

    replay_parser = subparsers.add_parser("replay")
    replay_parser.add_argument("--capability-name", required=True)
    replay_parser.add_argument("--params", default="{}")
    replay_parser.add_argument("--confirm-risky", action="store_true")
    replay_parser.set_defaults(func=_run_replay)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
