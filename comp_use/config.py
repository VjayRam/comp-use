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
    # Single source of truth for "what counts as sensitive" - both Guardrail.redact()
    # (one-way, for logs/console) and SensitiveValueTokenizer (two-way, for the LLM
    # call in discovery) read this same list. Neither has any field-specific logic
    # hardcoded; covering a new sensitive field (SSN, email, phone, ...) is adding
    # one regex here, not a code change to either consumer.
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
    # is configured (see build_llm_client in comp_use/cli.py) - this only
    # picks which one goes first, not whether fallback is available at all.
    # NVIDIA first: OpenRouter's free tier caps the whole ACCOUNT at 50 model
    # requests a day across every free model, and one discovery run spends 15-20
    # of them, so a couple of recordings exhaust it and every later run fails on
    # HTTP 429 regardless of which free model is named.
    model_provider: Literal["openrouter", "nvidia", "openai"] = "nvidia"
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-3.1-8b-instruct:free"
    # Used only as a vision fallback, when the accessibility tree alone hasn't been
    # enough to produce a usable locator (see DiscoveryAgent's needs_vision_fallback).
    # Must be a vision-capable model - the default text model above isn't.
    openrouter_vision_model: str = "google/gemma-4-26b-a4b-it:free"
    # The default provider (see model_provider above); OpenRouter is the automatic
    # fallback when this one fails, and only when its own key is configured.
    nvidia_api_key: str = ""
    nvidia_model: str = "meta/muse-glimmer-30b"
    nvidia_vision_model: str = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    # OpenAI proper. Its /v1/chat/completions is the shape the other two providers
    # imitate, so it needs no special handling - only a key, a model, and a
    # vision-capable model. Both model ids are env-overridable and deliberately not
    # pinned to anything exotic: check them against your own account's model list,
    # since OpenAI retires ids on a published schedule and a stale default here
    # fails at the first call with a 404 rather than at import.
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_vision_model: str = "gpt-4o"
    max_discovery_steps: int = 25
    artifacts_dir: Path = Path("artifacts")
    evidence_dir: Path = Path("evidence")


def load_settings() -> Settings:
    allowed_url_prefixes_env = os.environ.get("ALLOWED_URL_PREFIXES", "")
    kwargs: dict = {}
    if allowed_url_prefixes_env.strip():
        kwargs["allowed_url_prefixes"] = [
            p.strip() for p in allowed_url_prefixes_env.split(",") if p.strip()
        ]
    return Settings(
        **kwargs,
        model_provider=os.environ.get("MODEL_PROVIDER", "nvidia").strip().lower(),
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        openrouter_model=os.environ.get(
            "OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free"
        ),
        openrouter_vision_model=os.environ.get(
            "OPENROUTER_VISION_MODEL", "google/gemma-4-26b-a4b-it:free"
        ),
        nvidia_api_key=os.environ.get("NVIDIA_API_KEY", ""),
        nvidia_model=os.environ.get("NVIDIA_MODEL", "meta/muse-glimmer-30b"),
        nvidia_vision_model=os.environ.get(
            "NVIDIA_VISION_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
        ),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
        openai_vision_model=os.environ.get("OPENAI_VISION_MODEL", "gpt-4o"),
    )
