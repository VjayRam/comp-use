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
        ]
    )
    risky_confirm_default: bool = False
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-3.1-8b-instruct:free"
    max_discovery_steps: int = 25
    artifacts_dir: Path = Path("artifacts")
    evidence_dir: Path = Path("evidence")


def load_settings() -> Settings:
    return Settings(
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        openrouter_model=os.environ.get(
            "OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free"
        ),
    )
