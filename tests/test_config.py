import os
from comp_use.config import load_settings


def test_defaults(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = load_settings()
    assert settings.allowed_url_prefixes == ["http://localhost:5000"]
    assert "navigate" in settings.allowed_action_types
    assert settings.max_discovery_steps == 25
    assert settings.artifacts_dir.name == "artifacts"
    assert settings.evidence_dir.name == "evidence"


def test_env_override(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "some/model:free")
    settings = load_settings()
    assert settings.openrouter_api_key == "test-key"
    assert settings.openrouter_model == "some/model:free"
