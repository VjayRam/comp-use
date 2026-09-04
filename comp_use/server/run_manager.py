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
    novnc_url: str | None = None


class RunManager:
    """Runs `target_fn` on a background thread per capability-server request, so
    an HTTP call can return immediately while the real Playwright browser keeps
    driving the app. Escalation is surfaced via QueueTransport's on_notify
    callback (see .start()) instead of blocking the caller's HTTP thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._runs: dict[str, RunRecord] = {}

    def start(
        self, kind: str, capability_name: str, target_fn: Callable[[QueueTransport], Any],
        run_id: str | None = None,
    ) -> str:
        with self._lock:
            if run_id is None:
                run_id = f"{kind}_{int(time.time() * 1000)}"
                while run_id in self._runs:
                    run_id = f"{kind}_{int(time.time() * 1000)}"
            record = RunRecord(run_id=run_id, kind=kind, capability_name=capability_name)
            self._runs[run_id] = record

        def on_notify(request: InterventionRequest) -> None:
            with self._lock:
                record.status = "escalated"
                record.escalation = request

        def on_novnc_url(url: str) -> None:
            with self._lock:
                record.novnc_url = url

        transport = QueueTransport(on_notify=on_notify, on_novnc_url=on_novnc_url)
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

    def list(self) -> list[RunRecord]:
        with self._lock:
            return list(self._runs.values())

    def delete(self, run_id: str) -> bool:
        """Removes this process's in-memory record of a finished run - the
        capability server's DELETE /runs/{run_id} endpoint also clears the
        Postgres/on-disk history, this is just the RunManager half of it. Refuses
        to delete a 'running'/'escalated' run (its background thread and
        transport are still live and referenced elsewhere) - the caller should
        check status and map that to a 409 before ever reaching here. Returns
        False (not an error) if the run_id isn't known to this process at all,
        e.g. it only exists in Postgres from a run recorded before the server was
        last restarted."""
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return False
            if record.status in ("running", "escalated"):
                return False
            del self._runs[run_id]
            return True

    def request_takeover(self, run_id: str) -> bool:
        """Signals the running discovery/replay loop to pause for a human takeover
        at its next step boundary (see ControlTransport.request_takeover()). Only
        valid while the run is actively 'running' - a run already 'escalated' has
        already stopped for a human (nothing new to request), and 'done'/'error'
        runs have no loop left to pause. Returns False in either case; the caller
        maps that to a 409."""
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or record.status != "running":
                return False
            record.transport.request_takeover()
        return True

    def resume(self, run_id: str, note: str) -> bool:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or record.status != "escalated":
                return False
            record.status = "running"
        record.transport.resume(note)
        return True
