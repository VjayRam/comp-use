import argparse
import json
import threading
import time
from unittest.mock import patch

import pytest
from playwright.sync_api import sync_playwright

import comp_use.cli as cli
from comp_use.cli import _derive_success_checkpoint, load_artifact, next_artifact_version, save_artifact
from comp_use.config import Settings
from comp_use.llm_client import FakeLLMClient
from comp_use.schemas import ActionType, Artifact, Checkpoint, CheckpointType, Locator, LocatorStrategy, RiskTier, Step
from comp_use.surface import PlaywrightSurface
from mock_app.app import create_app


def _artifact(version=1):
    return Artifact(
        capability_name="lookup_member",
        version=version,
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


def test_next_artifact_version_is_1_for_a_new_capability(tmp_path):
    assert next_artifact_version("lookup_member", tmp_path) == 1


def test_next_artifact_version_increments_past_existing_versions(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)
    save_artifact(_artifact(version=2), tmp_path)
    assert next_artifact_version("lookup_member", tmp_path) == 3


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
        cli._run_discover(discover_args)

        artifact = load_artifact("lookup_member_cli_integration_test", settings.artifacts_dir)
        assert artifact.success_checkpoint.type == CheckpointType.ELEMENT_VISIBLE
        assert artifact.success_checkpoint.locator.value["name"] == "Member Detail"

        # a second discover run for the same capability must not overwrite v1
        with patch.object(cli, "OpenRouterClient", return_value=_scripted_lookup_member()):
            cli._run_discover(discover_args)
        assert (settings.artifacts_dir / "lookup_member_cli_integration_test" / "v1.json").exists()
        assert (settings.artifacts_dir / "lookup_member_cli_integration_test" / "v2.json").exists()

        # the honest test: replay with a DIFFERENT member than discovery used
        replay_args = argparse.Namespace(
            capability_name="lookup_member_cli_integration_test",
            params=json.dumps({"member_id": "67890"}),
            confirm_risky=False,
        )
        with patch("builtins.print") as mock_print:
            cli._run_replay(replay_args)
        printed = "".join(str(c.args[0]) for c in mock_print.call_args_list)
        assert '"outcome": "success"' in printed
