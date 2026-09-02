import argparse
import json
import threading
import time
from unittest.mock import patch

import pytest
from playwright.sync_api import sync_playwright

import comp_use.cli as cli
from comp_use.cli import (
    _build_llm_client, _derive_success_checkpoint, approve_artifact, load_artifact,
    next_artifact_version, reject_artifact, retire_artifact, run_replay, save_artifact,
)
from comp_use.config import Settings
from comp_use.escalation.controller import EscalationController
from comp_use.escalation.transport import ControlTransport
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.llm_client import FakeLLMClient, FallbackLLMClient, NvidiaNimClient, OpenRouterClient
from comp_use.schemas import (
    ActionType, Artifact, Checkpoint, CheckpointType, InputParam, Locator,
    LocatorStrategy, OutcomeType, ReplayResult, RiskTier, Step, ValueSource,
)
from comp_use.surface import PlaywrightSurface
from mock_app.app import create_app


def _artifact(version=1, status="approved", capability_name="lookup_member"):
    return Artifact(
        capability_name=capability_name,
        version=version,
        status=status,
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        steps=[Step(action=ActionType.NAVIGATE, target="/member/search", risk_tier=RiskTier.SAFE)],
        success_checkpoint=Checkpoint(
            type=CheckpointType.ELEMENT_VISIBLE,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Member Detail"}),
        ),
        created_from_run_id="run_1",
    )


def test_save_artifact_writes_versioned_json(tmp_path):
    path = save_artifact(_artifact(version=1), tmp_path)
    assert path == tmp_path / "lookup_member" / "v1.json"
    assert json.loads(path.read_text())["capability_name"] == "lookup_member"


def test_load_artifact_loads_latest_version_when_unspecified(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)
    save_artifact(_artifact(version=2), tmp_path)
    loaded = load_artifact("lookup_member", tmp_path)
    assert loaded.version == 2


def test_load_artifact_loads_specific_version(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)
    save_artifact(_artifact(version=2), tmp_path)
    loaded = load_artifact("lookup_member", tmp_path, version=1)
    assert loaded.version == 1


def _artifact_requiring_member_id(version=1, status="approved"):
    """Same shape as `_artifact()`, plus a required input param - forces
    `run_replay` to return VALIDATION_ERROR (before ever opening a browser) when
    called with empty params, which is enough to exercise the version-resolution
    and approval-gating logic in isolation, without needing a live mock app."""
    artifact = _artifact(version=version, status=status)
    artifact.input_schema = [InputParam(name="member_id", type="string", required=True)]
    return artifact


def test_run_replay_defaults_to_latest_approved_version_when_unpinned(tmp_path, monkeypatch):
    settings = Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    save_artifact(_artifact_requiring_member_id(version=1, status="approved"), tmp_path)
    save_artifact(_artifact_requiring_member_id(version=2, status="draft"), tmp_path)

    # v2 is a draft, so the unpinned call must fall back to v1 (the latest approved),
    # never silently pick up the newer draft - same guarantee load_artifact(version=None)
    # already gives every other unpinned caller. The empty params dict makes this return
    # VALIDATION_ERROR before any browser opens, so the version-resolution path can be
    # exercised on its own without a live mock app.
    with patch.object(cli, "load_artifact", wraps=load_artifact) as spy_load:
        result = run_replay(
            "lookup_member", {}, confirm_risky=False, diagnose_drift_on_failure=False, transport=None,
        )
    loaded_versions = [call.kwargs.get("version") for call in spy_load.call_args_list]
    assert loaded_versions == [None]
    assert result.outcome == OutcomeType.VALIDATION_ERROR


def test_run_replay_raises_when_pinned_version_is_not_approved(tmp_path, monkeypatch):
    settings = Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    save_artifact(_artifact_requiring_member_id(version=1, status="approved"), tmp_path)
    save_artifact(_artifact_requiring_member_id(version=2, status="draft"), tmp_path)

    with pytest.raises(ValueError, match="version 2 of 'lookup_member' is 'draft', not 'approved'"):
        run_replay(
            "lookup_member", {"member_id": "12345"}, confirm_risky=False, diagnose_drift_on_failure=False,
            transport=None, version=2,
        )


def test_run_replay_accepts_a_pinned_version_that_is_approved(tmp_path, monkeypatch):
    settings = Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    save_artifact(_artifact_requiring_member_id(version=1, status="approved"), tmp_path)
    save_artifact(_artifact_requiring_member_id(version=2, status="approved"), tmp_path)

    # empty params -> VALIDATION_ERROR before any browser opens, once the pin has
    # already passed the approved check without raising.
    with patch.object(cli, "load_artifact", wraps=load_artifact) as spy_load:
        result = run_replay(
            "lookup_member", {}, confirm_risky=False, diagnose_drift_on_failure=False,
            transport=None, version=1,
        )
    loaded_versions = [call.kwargs.get("version") for call in spy_load.call_args_list]
    assert loaded_versions == [1]  # the pin was honored
    assert result.outcome == OutcomeType.VALIDATION_ERROR  # reached past the approval check


def test_next_artifact_version_is_1_for_a_new_capability(tmp_path):
    assert next_artifact_version("lookup_member", tmp_path) == 1


def test_load_artifact_raises_a_clear_error_for_unknown_capability(tmp_path):
    with pytest.raises(FileNotFoundError, match="does_not_exist"):
        load_artifact("does_not_exist", tmp_path)


def test_next_artifact_version_increments_past_existing_versions(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)
    save_artifact(_artifact(version=2), tmp_path)
    assert next_artifact_version("lookup_member", tmp_path) == 3


def test_load_artifact_skips_draft_versions_when_unspecified(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)
    save_artifact(_artifact(version=2, status="draft"), tmp_path)
    loaded = load_artifact("lookup_member", tmp_path)
    assert loaded.version == 1


def test_load_artifact_raises_a_clear_error_when_all_versions_are_drafts(tmp_path):
    save_artifact(_artifact(version=1, status="draft"), tmp_path)
    with pytest.raises(FileNotFoundError, match="none are approved"):
        load_artifact("lookup_member", tmp_path)


def test_load_artifact_with_explicit_version_bypasses_the_draft_filter(tmp_path):
    save_artifact(_artifact(version=1, status="draft"), tmp_path)
    loaded = load_artifact("lookup_member", tmp_path, version=1)
    assert loaded.version == 1
    assert loaded.status == "draft"


def test_approve_artifact_flips_draft_to_approved(tmp_path):
    save_artifact(_artifact(version=1, status="draft"), tmp_path)
    approved = approve_artifact("lookup_member", 1, tmp_path)
    assert approved.status == "approved"
    reloaded = load_artifact("lookup_member", tmp_path, version=1)
    assert reloaded.status == "approved"


def test_approve_artifact_rejects_a_version_that_is_not_a_draft(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)  # default status="approved"
    with pytest.raises(ValueError, match="not 'draft'"):
        approve_artifact("lookup_member", 1, tmp_path)


def test_reject_artifact_flips_draft_to_rejected_without_deleting_the_file(tmp_path):
    path = save_artifact(_artifact(version=1, status="draft"), tmp_path)
    rejected = reject_artifact("lookup_member", 1, tmp_path)
    assert rejected.status == "rejected"
    assert path.exists()


def test_reject_artifact_rejects_a_version_that_is_not_a_draft(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)
    with pytest.raises(ValueError, match="not 'draft'"):
        reject_artifact("lookup_member", 1, tmp_path)


def test_retire_artifact_flips_approved_to_rejected(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)  # approved
    retired = retire_artifact("lookup_member", 1, tmp_path)
    assert retired.status == "rejected"


def test_retire_artifact_rejects_a_version_that_is_not_approved(tmp_path):
    save_artifact(_artifact(version=1, status="draft"), tmp_path)
    with pytest.raises(ValueError, match="not 'approved'"):
        retire_artifact("lookup_member", 1, tmp_path)


@pytest.fixture(scope="module")
def live_server():
    app = create_app()
    server = threading.Thread(target=lambda: app.run(port=5098, use_reloader=False), daemon=True)
    server.start()
    time.sleep(0.5)
    yield "http://localhost:5098"


@pytest.fixture
def surface(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        yield PlaywrightSurface(page)
        browser.close()


def test_derive_success_checkpoint_uses_page_heading_not_literal_url(surface, live_server):
    surface.act(ActionType.NAVIGATE, locator=None, target=f"{live_server}/member/12345", text=None)

    checkpoint = _derive_success_checkpoint(surface, fallback_url=surface.current_url())

    assert checkpoint.type == CheckpointType.ELEMENT_VISIBLE
    assert checkpoint.locator.value["role"] == "heading"
    assert checkpoint.locator.value["name"] == "Member Detail"
    # crucially, this checkpoint must hold for a *different* member's detail page too
    surface.act(ActionType.NAVIGATE, locator=None, target=f"{live_server}/member/67890", text=None)
    assert surface.check_checkpoint(checkpoint) is True


def test_derive_success_checkpoint_falls_back_to_url_when_no_heading(surface, live_server):
    surface.act(ActionType.NAVIGATE, locator=None, target=f"{live_server}/widgets/ticker", text=None)

    checkpoint = _derive_success_checkpoint(surface, fallback_url=surface.current_url())

    assert checkpoint.type == CheckpointType.URL_MATCHES
    assert checkpoint.url_pattern == "widgets/ticker"


def _settings_for(tmp_path, live_server):
    return Settings(
        allowed_url_prefixes=[live_server],
        artifacts_dir=tmp_path / "artifacts",
        evidence_dir=tmp_path / "evidence",
    )


def test_run_discover_produces_a_reusable_checkpoint_end_to_end(tmp_path, live_server, monkeypatch):
    """Regression test for issue 11: _run_discover's own checkpoint-construction
    code path (not a scratchpad script bypassing it) must produce an artifact
    whose success_checkpoint holds for a replay with different, valid params."""
    monkeypatch.setenv("COMP_USE_HEADLESS", "1")
    settings = _settings_for(tmp_path, live_server)

    def _scripted_lookup_member():
        return FakeLLMClient(scripted_actions=[
            {"action": "type_text", "locator": {"strategy": "role", "value": {"role": "textbox", "name": "Member ID"}},
             "text": "12345", "value_source": {"type": "goal_parameter", "param_name": "member_id", "param_type": "string"}, "done": False},
            {"action": "click", "locator": {"strategy": "role", "value": {"role": "button", "name": "Search"}}, "done": False},
            {"action": "finish", "done": True},
        ])

    with patch.object(cli, "load_settings", return_value=settings), \
         patch.object(cli, "OpenRouterClient", return_value=_scripted_lookup_member()):
        discover_args = argparse.Namespace(
            goal="Look up member 12345", start_url=f"{live_server}/member/search",
            capability_name="lookup_member_cli_integration_test", confirm_risky=False,
        )
        with patch("builtins.input", return_value="y"):
            cli._run_discover(discover_args)

        artifact = load_artifact("lookup_member_cli_integration_test", settings.artifacts_dir)
        assert artifact.success_checkpoint.type == CheckpointType.ELEMENT_VISIBLE
        assert artifact.success_checkpoint.locator.value["name"] == "Member Detail"

        # a second discover run for the same capability must not overwrite v1
        with patch.object(cli, "OpenRouterClient", return_value=_scripted_lookup_member()), \
             patch("builtins.input", return_value="y"):
            cli._run_discover(discover_args)
        assert (settings.artifacts_dir / "lookup_member_cli_integration_test" / "v1.json").exists()
        assert (settings.artifacts_dir / "lookup_member_cli_integration_test" / "v2.json").exists()

        # the honest test: replay with a DIFFERENT member than discovery used
        replay_args = argparse.Namespace(
            capability_name="lookup_member_cli_integration_test",
            params=json.dumps({"member_id": "67890"}),
            confirm_risky=False,
            diagnose_drift_on_failure=False,
            version=None,
        )
        with patch("builtins.print") as mock_print:
            cli._run_replay(replay_args)
        printed = "".join(str(c.args[0]) for c in mock_print.call_args_list)
        assert '"outcome": "success"' in printed


def test_main_dispatches_discover_subcommand_with_parsed_args(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "comp-use", "discover",
            "--goal", "Look up member 12345",
            "--start-url", "http://localhost:5000/member/search",
            "--capability-name", "lookup_member",
            "--confirm-risky",
        ],
    )
    with patch.object(cli, "_run_discover") as mock_run_discover:
        cli.main()
    assert mock_run_discover.called
    called_args = mock_run_discover.call_args.args[0]
    assert called_args.goal == "Look up member 12345"
    assert called_args.start_url == "http://localhost:5000/member/search"
    assert called_args.capability_name == "lookup_member"
    assert called_args.confirm_risky is True


def test_main_dispatches_replay_subcommand_with_parsed_args(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["comp-use", "replay", "--capability-name", "lookup_member", "--params", '{"member_id": "12345"}'],
    )
    with patch.object(cli, "_run_replay") as mock_run_replay:
        cli.main()
    assert mock_run_replay.called
    called_args = mock_run_replay.call_args.args[0]
    assert called_args.capability_name == "lookup_member"
    assert called_args.params == '{"member_id": "12345"}'
    assert called_args.confirm_risky is False  # not passed - defaults to False
    assert called_args.diagnose_drift_on_failure is False  # not passed - defaults to False
    assert called_args.version is None  # not passed - defaults to latest approved


def test_main_dispatches_replay_subcommand_with_explicit_version(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["comp-use", "replay", "--capability-name", "lookup_member", "--version", "3"],
    )
    with patch.object(cli, "_run_replay") as mock_run_replay:
        cli.main()
    called_args = mock_run_replay.call_args.args[0]
    assert called_args.version == 3


def _instrument_real_browser_close(monkeypatch, closed: list):
    """Wraps the REAL Playwright BrowserType.launch (not a fake) so the returned
    Browser's own .close() is observed - proves cleanup happens at the real API
    level, not just against a mock. Used by the two tests below."""
    from playwright.sync_api import BrowserType

    original_launch = BrowserType.launch

    def wrapped_launch(self, *args, **kwargs):
        browser = original_launch(self, *args, **kwargs)
        original_close = browser.close

        def wrapped_close(*a, **k):
            closed.append(True)
            return original_close(*a, **k)

        browser.close = wrapped_close
        return browser

    monkeypatch.setattr(BrowserType, "launch", wrapped_launch)


def test_run_discover_closes_the_browser_even_when_the_agent_raises(tmp_path, monkeypatch):
    settings = Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setenv("COMP_USE_HEADLESS", "1")
    closed: list = []
    _instrument_real_browser_close(monkeypatch, closed)

    class _RaisingAgent:
        def __init__(self, *a, **k):
            pass

        def run(self, goal, start_url):
            raise RuntimeError("simulated crash - e.g. a non-interactive-stdin escalation")

    monkeypatch.setattr(cli, "DiscoveryAgent", _RaisingAgent)

    with pytest.raises(RuntimeError, match="simulated crash"):
        cli.run_discover(
            goal="x", start_url="http://localhost:5000/member/search", capability_name="leak_check",
            confirm_risky=False, transport=None,
        )

    assert closed == [True]  # browser.close() ran despite the exception, not skipped


def test_run_replay_closes_the_browser_even_when_the_engine_raises(tmp_path, monkeypatch):
    settings = Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setenv("COMP_USE_HEADLESS", "1")
    save_artifact(_artifact(version=1, status="approved"), settings.artifacts_dir)
    closed: list = []
    _instrument_real_browser_close(monkeypatch, closed)

    class _RaisingEngine:
        def __init__(self, *a, **k):
            pass

        def run(self, artifact, params, confirm_risky=False):
            raise RuntimeError("simulated crash - e.g. a non-interactive-stdin escalation")

    monkeypatch.setattr(cli, "ReplayEngine", _RaisingEngine)

    with pytest.raises(RuntimeError, match="simulated crash"):
        cli.run_replay(
            capability_name="lookup_member", params={}, confirm_risky=False,
            diagnose_drift_on_failure=False, transport=None,
        )

    assert closed == [True]  # browser.close() ran despite the exception, not skipped


class FakeDriftTransport(ControlTransport):
    def __init__(self):
        self.notified = []

    def notify(self, request):
        self.notified.append(request)

    def wait_for_resume(self):
        return "reviewed the proposed patch"


class FakeVisionDriftClient:
    """Stands in for OpenRouterClient - the real vision-diagnosis behavior is
    already covered live in comp_use/llm_client.py's own tests; here we're
    proving cli.py wires the diagnosis result through to a saved artifact
    version and an escalation, not re-testing the LLM call itself."""

    def diagnose_drift(self, expected_locator, screenshot_b64):
        return {"found": True, "locator": {"strategy": "role", "value": {"role": "button", "name": "Confirm Transfer"}}}


def _broken_transfer_artifact(live_server: str) -> Artifact:
    """A transfer_funds-shaped artifact whose final step's locator has a typo
    ("Cofirm Transfer" vs. the real page's "Confirm Transfer") - simulating
    drift (a control renamed) without needing to actually modify the mock app."""
    return Artifact(
        capability_name="transfer_funds_drift_test",
        target={"app": "mock_bank", "base_url": live_server},
        steps=[
            Step(action=ActionType.NAVIGATE, target=f"{live_server}/member/12345/transfer", risk_tier=RiskTier.SAFE),
            Step(
                action=ActionType.TYPE_TEXT,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "From Account"}),
                value_source=ValueSource(type="fixed", reason="test"), value="ACC-001", risk_tier=RiskTier.SAFE,
            ),
            Step(
                action=ActionType.TYPE_TEXT,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "To Account"}),
                value_source=ValueSource(type="fixed", reason="test"), value="ACC-002", risk_tier=RiskTier.SAFE,
            ),
            Step(
                action=ActionType.TYPE_TEXT,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "Amount"}),
                value_source=ValueSource(type="fixed", reason="test"), value="25", risk_tier=RiskTier.SAFE,
            ),
            Step(
                action=ActionType.CLICK,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Transfer"}),
                risk_tier=RiskTier.SAFE,
            ),
            Step(
                action=ActionType.CLICK,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Cofirm Transfer"}),
                risk_tier=RiskTier.RISKY,
            ),
        ],
        success_checkpoint=Checkpoint(
            type=CheckpointType.ELEMENT_VISIBLE,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Confirmation"}),
        ),
        created_from_run_id="run_drift_test",
    )


class _RaisingDriftTransport(ControlTransport):
    """Reproduces a real, live-observed failure: LocalSharedBrowserTransport raises
    RuntimeError when stdin isn't interactive."""

    def notify(self, request):
        raise RuntimeError("stdin is closed/non-interactive")

    def wait_for_resume(self):
        raise AssertionError("should never be reached - notify() already raised")


class _MinimalSurface:
    """Just enough of Surface for propose_drift_patch()/_diagnose_and_propose_patch():
    a screenshot to send to the vision model, nothing more - no real browser needed."""

    def screenshot(self):
        return b"fakepng"

    def current_url(self):
        return "http://localhost:5000/member/12345/transfer"


def test_diagnose_and_propose_patch_survives_escalation_transport_failure(tmp_path):
    # The drift-diagnosis escalation is the 4th of the four call sites where
    # EscalationController.escalate() was previously unwrapped - a transport failure
    # here must not crash run_replay() and lose the already-computed ReplayResult;
    # the proposed patch must still be saved to disk even though notifying a human
    # about it failed.
    settings = Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence")
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="drift_escalation_transport_fail")
    escalation = EscalationController(evidence, _RaisingDriftTransport())
    surface = _MinimalSurface()

    artifact = Artifact(
        capability_name="drift_escalation_test",
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        steps=[
            Step(
                action=ActionType.CLICK,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Cofirm Transfer"}),
                risk_tier=RiskTier.RISKY,
            ),
        ],
        success_checkpoint=Checkpoint(
            type=CheckpointType.ELEMENT_VISIBLE,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Confirmation"}),
        ),
        created_from_run_id="run_x",
    )
    result = ReplayResult(
        outcome=OutcomeType.HARD_FAILURE, step_index=0, expected="click to succeed", observed="..."
    )

    with patch.object(cli, "OpenRouterClient", return_value=FakeVisionDriftClient()):
        cli._diagnose_and_propose_patch(settings, evidence, escalation, surface, artifact, result)

    # must not raise, and the real replay result must be left untouched
    assert result.proposed_patch_version is None
    # the patch was still saved to disk despite the escalation failure - only the
    # "notify a human to review it now" step failed, not the whole diagnosis
    # (this artifact had no prior versions saved, so the proposed patch is v1)
    assert (tmp_path / "drift_escalation_test" / "v1.json").exists()
    events = [
        json.loads(line)
        for line in (tmp_path / "evidence" / "drift_escalation_transport_fail" / "log.jsonl").read_text().splitlines()
    ]
    failed = [e for e in events if e["event_type"] == "escalation_transport_failed"]
    assert len(failed) == 1
    assert failed[0]["data"]["proposed_version"] == 1


def test_run_replay_with_diagnose_drift_proposes_a_patched_version_not_active(tmp_path, live_server, monkeypatch):
    monkeypatch.setenv("COMP_USE_HEADLESS", "1")
    settings = _settings_for(tmp_path, live_server)
    save_artifact(_broken_transfer_artifact(live_server), settings.artifacts_dir)
    transport = FakeDriftTransport()

    with patch.object(cli, "load_settings", return_value=settings), \
         patch.object(cli, "OpenRouterClient", return_value=FakeVisionDriftClient()), \
         patch.object(cli, "LocalSharedBrowserTransport", return_value=transport):
        replay_args = argparse.Namespace(
            capability_name="transfer_funds_drift_test",
            params="{}",
            confirm_risky=True,  # skip the unrelated risky-step pause; only care about drift escalation here
            diagnose_drift_on_failure=True,
            version=None,
        )
        result_json = None

        def _capture(*args, **kwargs):
            nonlocal result_json
            if args and isinstance(args[0], str) and '"outcome"' in args[0]:
                result_json = args[0]

        with patch("builtins.print", side_effect=_capture):
            cli._run_replay(replay_args)

    import json as _json
    result = _json.loads(result_json)
    # the run itself genuinely failed - drift diagnosis must never change that
    assert result["outcome"] == "hard_failure"
    assert result["proposed_patch_version"] == 2

    # v1 (the original, broken artifact) must be untouched
    original = load_artifact("transfer_funds_drift_test", settings.artifacts_dir, version=1)
    assert original.steps[5].locator.value["name"] == "Cofirm Transfer"
    # v2 is the proposed patch, saved but never applied to this run
    patched = load_artifact("transfer_funds_drift_test", settings.artifacts_dir, version=2)
    assert patched.steps[5].locator.value["name"] == "Confirm Transfer"

    assert len(transport.notified) == 1
    assert "drift" in transport.notified[0].reason.lower()
    assert "v2" in transport.notified[0].reason


class FakeTransport(ControlTransport):
    def notify(self, request):
        pass

    def wait_for_resume(self):
        return "handled it"


def _scripted_lookup_member():
    return FakeLLMClient(scripted_actions=[
        {"action": "type_text", "locator": {"strategy": "role", "value": {"role": "textbox", "name": "Member ID"}},
         "text": "12345", "value_source": {"type": "goal_parameter", "param_name": "member_id", "param_type": "string"}, "done": False},
        {"action": "click", "locator": {"strategy": "role", "value": {"role": "button", "name": "Search"}}, "done": False},
        {"action": "finish", "done": True},
    ])


def test_run_discover_produces_a_draft_artifact_by_default(tmp_path, live_server, monkeypatch):
    monkeypatch.setenv("COMP_USE_HEADLESS", "1")
    settings = _settings_for(tmp_path, live_server)
    transport = FakeTransport()  # existing fake from test_escalation.py-style tests; reuse comp_use.escalation.transport.ControlTransport
    with patch.object(cli, "load_settings", return_value=settings), \
         patch.object(cli, "OpenRouterClient", return_value=_scripted_lookup_member()), \
         patch("builtins.input", side_effect=EOFError):  # non-interactive stdin: must NOT auto-approve
        artifact = cli.run_discover(
            goal="look up member 12345",
            start_url=f"{live_server}/member/search",
            capability_name="lookup_member_draft_test",
            confirm_risky=False,
            transport=transport,
            interactive=True,
        )
    assert artifact.status == "draft"


def test_run_discover_approves_when_the_operator_confirms_interactively(tmp_path, live_server, monkeypatch):
    monkeypatch.setenv("COMP_USE_HEADLESS", "1")
    settings = _settings_for(tmp_path, live_server)
    transport = FakeTransport()
    with patch.object(cli, "load_settings", return_value=settings), \
         patch.object(cli, "OpenRouterClient", return_value=_scripted_lookup_member()), \
         patch("builtins.input", return_value="y"):
        artifact = cli.run_discover(
            goal="look up member 12345",
            start_url=f"{live_server}/member/search",
            capability_name="lookup_member_approve_test",
            confirm_risky=False,
            transport=transport,
            interactive=True,
        )
    assert artifact.status == "approved"


def test_run_discover_non_interactive_never_prompts_and_stays_draft(tmp_path, live_server, monkeypatch):
    monkeypatch.setenv("COMP_USE_HEADLESS", "1")
    settings = _settings_for(tmp_path, live_server)
    transport = FakeTransport()
    with patch.object(cli, "load_settings", return_value=settings), \
         patch.object(cli, "OpenRouterClient", return_value=_scripted_lookup_member()), \
         patch("builtins.input", side_effect=AssertionError("must not prompt when interactive=False")):
        artifact = cli.run_discover(
            goal="look up member 12345",
            start_url=f"{live_server}/member/search",
            capability_name="lookup_member_api_test",
            confirm_risky=False,
            transport=transport,
            interactive=False,
        )
    assert artifact.status == "draft"


def test_build_llm_client_is_openrouter_only_without_an_nvidia_key():
    settings = Settings(nvidia_api_key="")
    client = _build_llm_client(settings)
    assert isinstance(client, OpenRouterClient)


def test_build_llm_client_wraps_openrouter_with_nvidia_fallback_when_key_present():
    settings = Settings(nvidia_api_key="nvidia-test-key")
    client = _build_llm_client(settings)
    assert isinstance(client, FallbackLLMClient)
    assert isinstance(client.primary, OpenRouterClient)
    assert isinstance(client.fallback, NvidiaNimClient)


def test_build_llm_client_is_nvidia_only_when_provider_is_nvidia_and_no_openrouter_key():
    settings = Settings(model_provider="nvidia", nvidia_api_key="nvidia-test-key", openrouter_api_key="")
    client = _build_llm_client(settings)
    assert isinstance(client, NvidiaNimClient)


def test_build_llm_client_wraps_nvidia_with_openrouter_fallback_when_provider_is_nvidia():
    settings = Settings(model_provider="nvidia", nvidia_api_key="nvidia-test-key", openrouter_api_key="or-test-key")
    client = _build_llm_client(settings)
    assert isinstance(client, FallbackLLMClient)
    assert isinstance(client.primary, NvidiaNimClient)
    assert isinstance(client.fallback, OpenRouterClient)


def test_build_llm_client_nvidia_provider_with_no_nvidia_key_still_falls_back_to_openrouter():
    # MODEL_PROVIDER=nvidia with no NVIDIA_API_KEY configured can't actually
    # use NVIDIA as primary - fall back to OpenRouter-only rather than
    # building a client that's guaranteed to fail on first use.
    settings = Settings(model_provider="nvidia", nvidia_api_key="", openrouter_api_key="or-test-key")
    client = _build_llm_client(settings)
    assert isinstance(client, OpenRouterClient)


def test_main_dispatches_approve_subcommand_with_parsed_args(tmp_path, monkeypatch):
    save_artifact(_artifact(version=1, status="draft"), tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence"))
    monkeypatch.setattr(
        "sys.argv",
        ["comp-use", "approve", "--capability-name", "lookup_member", "--version", "1"],
    )

    cli.main()

    reloaded = load_artifact("lookup_member", tmp_path, version=1)
    assert reloaded.status == "approved"


def test_version_lock_prevents_concurrent_discover_from_clobbering_versions(tmp_path):
    """Regression test for the TOCTOU race: run_discover reads the next
    artifact version, then later saves under it. Two concurrent discover
    requests for the same capability (now reachable via the capability
    server's HTTP API, one background thread per request) could previously
    both read the same "next version" number, and whichever save ran second
    would silently clobber the first's artifact. This exercises the exact
    read-then-save sequence guarded by cli._version_lock - with an
    artificial delay between the read and the write to force the race
    window wide open - and asserts every thread ends up with a distinct,
    gapless version number."""
    capability_name = "concurrent_version_test"

    def allocate_and_save():
        with cli._version_lock:
            version = next_artifact_version(capability_name, tmp_path)
            time.sleep(0.05)  # widen the window between read and save
            save_artifact(_artifact(version=version, status="draft", capability_name=capability_name), tmp_path)

    threads = [threading.Thread(target=allocate_and_save) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    versions = sorted(int(p.stem[1:]) for p in (tmp_path / capability_name).glob("v*.json"))
    assert versions == [1, 2, 3, 4, 5]


def test_version_lock_methodology_actually_detects_the_race_when_unguarded(tmp_path):
    """Negative control proving the test above is a real regression test and
    not a tautology: the identical read-then-save sequence, run WITHOUT the
    lock, reliably produces colliding (and therefore silently overwritten)
    version numbers under the same artificial delay."""
    capability_name = "concurrent_version_test_unguarded"

    def allocate_and_save_without_lock():
        version = next_artifact_version(capability_name, tmp_path)
        time.sleep(0.05)
        save_artifact(_artifact(version=version, status="draft", capability_name=capability_name), tmp_path)

    threads = [threading.Thread(target=allocate_and_save_without_lock) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    versions = sorted(int(p.stem[1:]) for p in (tmp_path / capability_name).glob("v*.json"))
    # all 5 threads raced to read version 0 -> computed "1" -> only one file exists
    assert len(versions) < 5


def test_run_serve_launches_uvicorn_with_the_app(monkeypatch):
    calls = {}

    def fake_run(app, host, port):
        calls["host"] = host
        calls["port"] = port

    monkeypatch.setattr("uvicorn.run", fake_run)

    args = argparse.Namespace(host="0.0.0.0", port=9000)
    cli._run_serve(args)

    assert calls == {"host": "0.0.0.0", "port": 9000}
