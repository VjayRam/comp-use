import re

from comp_use.config import Settings
from comp_use.schemas import RiskTier

# Field-name substrings (not regex patterns over VALUES, unlike
# Settings.redaction_patterns) that mark a goal_parameter as a credential/secret
# rather than ordinary business data. Used to decide whether it's safe to capture the
# discovery-time literal value as InputParam.example - a real password/token typed
# during discovery has no business being written into an artifact JSON on disk, even
# though the parameter itself (e.g. "sign on as any operator") is legitimately
# generalizable and should stay required.
_SENSITIVE_PARAM_NAME_HINTS = ("password", "passwd", "pwd", "secret", "token", "apikey", "api_key")


def is_sensitive_param_name(name: str | None) -> bool:
    if not name:
        return False
    lowered = name.lower()
    return any(hint in lowered for hint in _SENSITIVE_PARAM_NAME_HINTS)


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
