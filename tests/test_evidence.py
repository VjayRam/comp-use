import json

from comp_use.config import load_settings
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail


def test_log_event_writes_redacted_jsonl(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    logger = EvidenceLogger(settings, Guardrail(settings), run_id="run_001")

    logger.log_event("action", {"detail": "typed $1,500.00 into field"})

    log_path = tmp_path / "evidence" / "run_001" / "log.jsonl"
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event_type"] == "action"
    assert "$1,500.00" not in record["data"]["detail"]
    assert "[REDACTED]" in record["data"]["detail"]


def test_save_screenshot_writes_file_and_returns_path(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    logger = EvidenceLogger(settings, Guardrail(settings), run_id="run_002")

    path = logger.save_screenshot(b"\x89PNG\r\n", label="step_1")

    assert path.endswith("step_1.png")
    assert (tmp_path / "evidence" / "run_002" / "step_1.png").exists()


def test_save_screenshot_returns_none_and_writes_nothing_when_bytes_are_none(tmp_path):
    # Capture can fail upstream (see comp_use.surface.safe_screenshot) - this
    # must degrade to "no screenshot" cleanly, not crash trying to write None.
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    logger = EvidenceLogger(settings, Guardrail(settings), run_id="run_003")

    path = logger.save_screenshot(None, label="step_1")

    assert path is None
    assert not (tmp_path / "evidence" / "run_003" / "step_1.png").exists()
