import queue
from typing import Callable

from comp_use.schemas import InterventionRequest


class ControlTransport:
    def notify(self, request: InterventionRequest) -> None:
        raise NotImplementedError

    def wait_for_resume(self) -> str:
        raise NotImplementedError


class LocalSharedBrowserTransport(ControlTransport):
    def notify(self, request: InterventionRequest) -> None:
        print(f"[ESCALATION] {request.reason} (step {request.current_step}) — "
              f"take over the browser window, then type 'resume' here.")

    def wait_for_resume(self) -> str:
        while True:
            try:
                typed = input("> ").strip().lower()
            except EOFError:
                raise RuntimeError(
                    "Escalation requires a human to type 'resume' in an interactive "
                    "terminal, but stdin is closed/non-interactive. Re-run in an "
                    "interactive shell, or avoid triggering escalation (e.g. pass "
                    "--confirm-risky, or use params/artifacts that don't hit a risky "
                    "step or a drift-diagnosis review)."
                ) from None
            if typed == "resume":
                break
        try:
            note = input("Briefly, what did you do? (one line, optional): ").strip()
        except EOFError:
            note = ""
        return note


class QueueTransport(ControlTransport):
    """A ControlTransport for the capability server: `notify()` reports the
    escalation to a caller-supplied callback (how GET /runs/{run_id} learns
    about it) instead of printing, and `wait_for_resume()` blocks on a
    thread-safe queue instead of stdin - unblocked by POST /runs/{run_id}/resume
    calling .resume(note) from a different thread (the HTTP request thread)."""

    def __init__(self, on_notify: Callable[[InterventionRequest], None]):
        self._on_notify = on_notify
        self._resume_queue: "queue.Queue[str]" = queue.Queue()

    def notify(self, request: InterventionRequest) -> None:
        self._on_notify(request)

    def wait_for_resume(self) -> str:
        return self._resume_queue.get()

    def resume(self, note: str) -> None:
        self._resume_queue.put(note)
