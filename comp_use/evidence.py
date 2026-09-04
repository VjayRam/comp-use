import json
from pathlib import Path

from comp_use.config import Settings
from comp_use.guardrail import Guardrail
from comp_use.pg import store as pg_store


def _redact_value(guardrail: Guardrail, value):
    if isinstance(value, str):
        return guardrail.redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(guardrail, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(guardrail, v) for v in value]
    return value


class EvidenceLogger:
    """Postgres (comp_use/pg/schema.sql's run_events/run_screenshots tables) is the
    primary evidence store when COMP_USE_DB_URL is configured - events and
    screenshots go there and nowhere else, so a run leaves nothing on disk to manage
    or accidentally commit. Without a DB configured (local dev/tests), this falls
    back to the original evidence/<run_id>/log.jsonl + <label>.png layout."""

    def __init__(self, settings: Settings, guardrail: Guardrail, run_id: str):
        self.settings = settings
        self.guardrail = guardrail
        self.run_id = run_id
        self.db_backed = pg_store.db_enabled()
        self.run_dir = Path(settings.evidence_dir) / run_id
        if not self.db_backed:
            self.run_dir.mkdir(parents=True, exist_ok=True)

    def log_event(self, event_type: str, data: dict) -> None:
        redacted = _redact_value(self.guardrail, data)
        if self.db_backed:
            pg_store.insert_event(self.run_id, event_type, redacted)
            return
        record = {"event_type": event_type, "data": redacted}
        log_path = self.run_dir / "log.jsonl"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def save_screenshot(self, png_bytes: bytes | None, label: str) -> str | None:
        if png_bytes is None:
            return None
        if self.db_backed:
            pg_store.save_screenshot(self.run_id, label, png_bytes)
            return f"postgres:{self.run_id}/{label}"
        path = self.run_dir / f"{label}.png"
        path.write_bytes(png_bytes)
        return str(path)
