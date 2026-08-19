import re

from comp_use.config import Settings
from comp_use.schemas import RiskTier


class AllowlistViolation(Exception):
    pass


class Guardrail:
    def __init__(self, settings: Settings):
        self.settings = settings

    def check_allowlist(self, url: str, action_type: str) -> None:
        if action_type not in self.settings.allowed_action_types:
            raise AllowlistViolation(f"action type '{action_type}' not in allowlist")
        if not url or not any(
            url.startswith(prefix) for prefix in self.settings.allowed_url_prefixes
        ):
            raise AllowlistViolation(f"url '{url}' not in allowlist")

    def requires_confirmation(self, risk_tier: RiskTier, confirm_risky: bool) -> bool:
        return risk_tier == RiskTier.RISKY and not confirm_risky

    def redact(self, text: str) -> str:
        redacted = text
        for pattern in self.settings.redaction_patterns:
            redacted = re.sub(pattern, "[REDACTED]", redacted)
        return redacted
