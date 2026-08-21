import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from comp_use.escalation.transport import QueueTransport
from comp_use.schemas import InterventionRequest, ReplayResult


@dataclass
class RunRecord:
    run_id: str
    kind: Literal["discover", "invoke"]
    capability_name: str
    status: Literal["running", "escalated", "done", "error"] = "running"
    escalation: InterventionRequest | None = None
    transport: QueueTransport | None = None
    result: ReplayResult | None = None
    discover_result: dict | None = None
    error: str | None = None


class RunManager:
    """Runs `target_fn` on a background thread per capability-server request, so
    an HTTP call can return immediately while the real Playwright browser keeps
    driving the app. Escalation is surfaced via QueueTransport's on_notify
    callback (see .start()) instead of blocking the caller's HTTP thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._runs: dict[str, RunRecord] = {}

    def start(self, kind: str, capability_name: str, target_fn: Callable[[QueueTransport], Any]) -> str:
        with self._lock:
            run_id = f"{kind}_{int(time.time() * 1000)}"
            while run_id in self._runs:
                run_id = f"{kind}_{int(time.time() * 1000)}"
            record = RunRecord(run_id=run_id, kind=kind, capability_name=capability_name)
            self._runs[run_id] = record

        def on_notify(request: InterventionRequest) -> None:
            with self._lock:
                record.status = "escalated"
                record.escalation = request

        transport = QueueTransport(on_notify=on_notify)
        record.transport = transport

        def worker() -> None:
            try:
                outcome = target_fn(transport)
            except Exception as exc:
                with self._lock:
                    record.status = "error"
                    record.error = f"{type(exc).__name__}: {exc}"
                return
            with self._lock:
                if kind == "invoke":
                    record.result = outcome
                else:
                    record.discover_result = outcome
                record.status = "done"

        threading.Thread(target=worker, daemon=True).start()
        return run_id

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def resume(self, run_id: str, note: str) -> bool:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or record.status != "escalated":
                return False
            record.status = "running"
        record.transport.resume(note)
        return True
