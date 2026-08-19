import os
from comp_use.config import load_settings


def test_defaults(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    settings = load_settings()
    assert settings.allowed_url_prefixes == ["http://localhost:5000"]
    assert "navigate" in settings.allowed_action_types
    assert settings.max_discovery_steps == 25
    assert settings.artifacts_dir.name == "artifacts"
    assert settings.evidence_dir.name == "evidence"
    # no NVIDIA_API_KEY means no fallback provider configured, by default
    assert settings.nvidia_api_key == ""


def test_env_override(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "some/model:free")
    monkeypatch.setenv("OPENROUTER_VISION_MODEL", "some/vision-model:free")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvidia-test-key")
    monkeypatch.setenv("NVIDIA_MODEL", "meta/some-model")
    monkeypatch.setenv("NVIDIA_VISION_MODEL", "meta/some-vision-model")
    settings = load_settings()
    assert settings.openrouter_api_key == "test-key"
    assert settings.openrouter_model == "some/model:free"
    assert settings.openrouter_vision_model == "some/vision-model:free"
    assert settings.nvidia_api_key == "nvidia-test-key"
    assert settings.nvidia_model == "meta/some-model"
    assert settings.nvidia_vision_model == "meta/some-vision-model"
