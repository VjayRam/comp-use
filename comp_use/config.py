import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    # model_provider is a deliberate field name (not a Pydantic internal) - silence
    # the "protected namespace" warning rather than rename around it.
    model_config = ConfigDict(protected_namespaces=())

    allowed_url_prefixes: list[str] = Field(
        default_factory=lambda: ["http://localhost:5000"]
    )
    allowed_action_types: list[str] = Field(
        default_factory=lambda: [
            "navigate", "click", "type_text", "select_option", "extract",
        ]
    )
    redaction_patterns: list[str] = Field(
        default_factory=lambda: [
            r"\b\d{9,12}\b",
            r"\$[\d,]+\.\d{2}",
            # Mock bank's own structured identifiers (account/sub-account/
            # transaction/confirmation numbers) - real-looking business
            # identifiers, unlike the bare member_id used openly in the
            # assignment's own example goal ("look up member 12345"), which is
            # deliberately left unredacted. See REPORT.md Safety section.
            r"\bACC-\d+\b",
            r"\bSUB-\d+\b",
            r"\bTXN-\d+\b",
            r"\bCONF-\d+\b",
        ]
    )
    # Which provider decide_next_action()/diagnose_drift() go to first. The
    # OTHER provider is still used as an automatic fallback if its own API key
    # is configured (see _build_llm_client in comp_use/cli.py) - this only
    # picks which one goes first, not whether fallback is available at all.
    model_provider: Literal["openrouter", "nvidia"] = "openrouter"
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-3.1-8b-instruct:free"
    # Used only as a vision fallback, when the accessibility tree alone hasn't been
    # enough to produce a usable locator (see DiscoveryAgent's needs_vision_fallback).
    # Must be a vision-capable model - the default text model above isn't.
    openrouter_vision_model: str = "google/gemma-4-26b-a4b-it:free"
    # Second provider, tried only when OpenRouter fails (rate limit, timeout, ...) -
    # see FallbackLLMClient. Empty by default: no NVIDIA_API_KEY means no fallback,
    # identical to the previous OpenRouter-only behavior.
    nvidia_api_key: str = ""
    nvidia_model: str = "meta/llama-3.1-8b-instruct"
    nvidia_vision_model: str = "meta/llama-3.2-11b-vision-instruct"
    max_discovery_steps: int = 25
    artifacts_dir: Path = Path("artifacts")
    evidence_dir: Path = Path("evidence")


def load_settings() -> Settings:
    return Settings(
        model_provider=os.environ.get("MODEL_PROVIDER", "openrouter").strip().lower(),
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        openrouter_model=os.environ.get(
            "OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free"
        ),
        openrouter_vision_model=os.environ.get(
            "OPENROUTER_VISION_MODEL", "google/gemma-4-26b-a4b-it:free"
        ),
        nvidia_api_key=os.environ.get("NVIDIA_API_KEY", ""),
        nvidia_model=os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct"),
        nvidia_vision_model=os.environ.get(
            "NVIDIA_VISION_MODEL", "meta/llama-3.2-11b-vision-instruct"
        ),
    )
