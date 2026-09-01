from __future__ import annotations

import re


class SensitiveValueTokenizer:
    """Reversibly replaces sensitive values (account/transaction/confirmation
    IDs, dollar amounts - the same shapes Guardrail.redact() matches) with
    stable per-run placeholder tokens before text is sent to the LLM, and maps
    them back before any value reaches the real browser, the artifact, or the
    evidence log.

    Unlike Guardrail.redact() (one-way, "[REDACTED]", for logs/console), this
    is two-way: the LLM reasons over tokens, never raw values, but the tool
    layer (surface.act()) always acts on real data. Scoped to text content
    only - it cannot cover a raw screenshot sent to a vision model; that
    remains a documented separate exposure (see REPORT.md Safety section).
    """

    def __init__(self, patterns: list[str]):
        self._patterns = [re.compile(p) for p in patterns]
        self._value_to_token: dict[str, str] = {}
        self._token_to_value: dict[str, str] = {}
        self._counter = 0

    def tokenize(self, text: str | None) -> str | None:
        if not text:
            return text
        for pattern in self._patterns:
            text = pattern.sub(self._replace, text)
        return text

    def _replace(self, match: re.Match) -> str:
        value = match.group(0)
        token = self._value_to_token.get(value)
        if token is None:
            self._counter += 1
            token = f"[[TOK{self._counter}]]"
            self._value_to_token[value] = token
            self._token_to_value[token] = value
        return token

    def detokenize(self, text: str | None) -> str | None:
        if not text or "[[TOK" not in text:
            return text
        for token, value in self._token_to_value.items():
            text = text.replace(token, value)
        return text

    def detokenize_value(self, value):
        """Recursively detokenize strings inside a dict/list/str decision fragment
        (e.g. a Locator's `value` dict), leaving other types untouched."""
        if isinstance(value, str):
            return self.detokenize(value)
        if isinstance(value, dict):
            return {k: self.detokenize_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.detokenize_value(v) for v in value]
        return value
