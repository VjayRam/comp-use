import json
from pathlib import Path

from comp_use.config import Settings
from comp_use.guardrail import Guardrail


def _redact_value(guardrail: Guardrail, value):
    if isinstance(value, str):
        return guardrail.redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(guardrail, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(guardrail, v) for v in value]
    return value


class EvidenceLogger:
    def __init__(self, settings: Settings, guardrail: Guardrail, run_id: str):
        self.settings = settings
        self.guardrail = guardrail
        self.run_id = run_id
        self.run_dir = Path(settings.evidence_dir) / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def log_event(self, event_type: str, data: dict) -> None:
        record = {"event_type": event_type, "data": _redact_value(self.guardrail, data)}
        log_path = self.run_dir / "log.jsonl"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def save_screenshot(self, png_bytes: bytes, label: str) -> str:
        path = self.run_dir / f"{label}.png"
        path.write_bytes(png_bytes)
        return str(path)
