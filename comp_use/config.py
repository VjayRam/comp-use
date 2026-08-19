import os
from pathlib import Path

from pydantic import BaseModel, Field


class Settings(BaseModel):
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
    risky_confirm_default: bool = False
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-3.1-8b-instruct:free"
    # Used only as a vision fallback, when the accessibility tree alone hasn't been
    # enough to produce a usable locator (see DiscoveryAgent's needs_vision_fallback).
    # Must be a vision-capable model - the default text model above isn't.
    openrouter_vision_model: str = "google/gemma-4-26b-a4b-it:free"
    max_discovery_steps: int = 25
    artifacts_dir: Path = Path("artifacts")
    evidence_dir: Path = Path("evidence")


def load_settings() -> Settings:
    return Settings(
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        openrouter_model=os.environ.get(
            "OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free"
        ),
        openrouter_vision_model=os.environ.get(
            "OPENROUTER_VISION_MODEL", "google/gemma-4-26b-a4b-it:free"
        ),
    )
