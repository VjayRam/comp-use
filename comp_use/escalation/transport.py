import queue
import threading
from typing import Callable

from comp_use.schemas import InterventionRequest


class EscalationAbandoned(RuntimeError):
    """Raised by QueueTransport.wait_for_resume() when a run's escalation is
    force-cancelled (see RunManager.cancel() / DELETE /runs/{run_id}?force=true)
    rather than ever being resumed by a human. Without this, an abandoned
    escalation blocked its worker thread - and the live browser/Docker container
    underneath it - forever, with DELETE explicitly refusing to touch a
    'running'/'escalated' run. ReplayEngine.run() and DiscoveryAgent._escalate()
    already catch any exception out of escalate() and turn it into a clean,
    reported failure, so this needs no special handling there - it just needs a
    way to actually fire."""


class RunInterrupted(BaseException):
    """Raised inside a run's own thread when an operator stops it from the
    dashboard (see RunManager.interrupt() / POST /runs/{run_id}/interrupt).

    Deliberately a BaseException, not an Exception. Every layer between the step
    loop and the worker thread - ReplayEngine.run(), DiscoveryAgent._escalate(),
    the CLI wrappers - catches broad `except Exception` and turns what it caught
    into a REPORTED failure (a hard_failure result, a failed discovery). An
    interrupt is not a failure of the capability and must not be recorded as
    one, so it has to pass through those handlers untouched and reach the worker,
    which marks the run 'interrupted'. Subclassing BaseException is what makes
    that true by construction rather than by remembering to re-raise it in a
    dozen places.

    Nothing needs to catch this to stop the sandbox container: cli.py's
    _browser_session is a context manager whose finally already calls
    sandbox.stop(), so unwinding through it tears the container down.
    """


class ControlTransport:
    def notify(self, request: InterventionRequest) -> None:
        raise NotImplementedError

    def wait_for_resume(self) -> str:
        raise NotImplementedError

    def request_takeover(self) -> None:
        """Ask the running discovery/replay loop to pause for a VOLUNTARY human
        takeover at the next step boundary, distinct from the agent/engine
        escalating on its own (a risky step, loop detection, ...). Default is a
        no-op - only QueueTransport (the capability server's HTTP-driven runs)
        supports this; a CLI-local run has no separate "take control" button to
        wire it to."""
        pass

    def takeover_requested(self) -> bool:
        """Polled by DiscoveryAgent/ReplayEngine once per step. True means: stop
        before the next action and escalate with a "manual takeover" reason,
        exactly like any other escalation (same screenshot/resume/evidence
        handling) - see EscalationController.escalate()."""
        return False

    def clear_takeover(self) -> None:
        """Called by EscalationController.escalate() once a pause actually
        happens, whatever triggered it - a takeover request must never fire a
        SECOND time after being honored once."""
        pass

    def on_novnc_url(self, url: str) -> None:
        """Called once, right after a sandbox container's noVNC URL becomes
        available (see cli.py's _browser_session) - not part of the
        notify/wait_for_resume escalation handshake, just a side-channel so
        whoever's managing this run (a human at a terminal, or RunManager for
        the HTTP API) learns where to watch/control the live session, whether
        or not escalation ever actually happens. Default is a no-op - only
        sandbox-mode runs ever call this at all."""
        pass

    def interrupt(self) -> None:
        """Ask the running loop to stop for good at its next step boundary and
        raise RunInterrupted. Distinct from request_takeover() (pause, hand the
        browser to a human, resume the SAME run) and from cancel() (unblock an
        abandoned escalation so the run can finish reporting): this one ends the
        run. Default is a no-op - only QueueTransport, the capability server's
        HTTP-driven runs, has a "Stop run" button to wire it to."""
        pass

    def interrupt_requested(self) -> bool:
        """Polled by DiscoveryAgent/ReplayEngine once per step, beside
        takeover_requested(). True means: raise RunInterrupted before starting
        the next step."""
        return False

    def cancel(self) -> None:
        """Force-unblocks a pending wait_for_resume() with EscalationAbandoned
        instead of a real resume note - see DELETE /runs/{run_id}?force=true.
        Default is a no-op: a CLI-local run (LocalSharedBrowserTransport) has no
        server-side "force cancel" button to wire this to, only QueueTransport
        (the capability server's HTTP-driven runs) supports it."""
        pass


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

    def on_novnc_url(self, url: str) -> None:
        print(f"[sandbox] Watch/control the live session at: {url}")


class QueueTransport(ControlTransport):
    """A ControlTransport for the capability server: `notify()` reports the
    escalation to a caller-supplied callback (how GET /runs/{run_id} learns
    about it) instead of printing, and `wait_for_resume()` blocks on a
    thread-safe queue instead of stdin - unblocked by POST /runs/{run_id}/resume
    calling .resume(note) from a different thread (the HTTP request thread)."""

    def __init__(
        self,
        on_notify: Callable[[InterventionRequest], None],
        on_novnc_url: Callable[[str], None] | None = None,
    ):
        self._on_notify = on_notify
        self._on_novnc_url = on_novnc_url
        self._resume_queue: "queue.Queue[str]" = queue.Queue()
        self._takeover_event = threading.Event()
        self._cancel_event = threading.Event()
        self._interrupt_event = threading.Event()

    def notify(self, request: InterventionRequest) -> None:
        self._on_notify(request)

    def wait_for_resume(self) -> str:
        # A duplicate resume() call (e.g. a double-clicked "Resume" button firing
        # two HTTP requests) can leave a leftover item in the queue after the
        # escalation it was meant for already consumed one and moved on - RunManager
        # only gates resume() on status=="escalated", which a LATER escalation in
        # the same run also satisfies. Without draining first, that stale item
        # would satisfy the next escalation's wait_for_resume() immediately, with
        # no human having looked at it - silently no-opping the confirmation gate.
        while True:
            try:
                self._resume_queue.get_nowait()
            except queue.Empty:
                break
        # Polls in bounded slices rather than an untimed queue.get() - an
        # abandoned escalation (nobody ever resumes it) previously blocked this
        # thread, and the browser/Docker container underneath it, forever. The
        # slice length only affects how quickly cancel() is noticed, not
        # normal-path latency (a real resume() still wakes this up within one
        # slice at most).
        while True:
            # Checked before the cancel event: interrupt() sets BOTH (an escalated
            # run is parked here, not at a step boundary, so the boundary poll can
            # never fire for it). Interrupt is the more specific intent of the two,
            # and it must not be reported as an abandoned escalation.
            if self._interrupt_event.is_set():
                raise RunInterrupted("run was interrupted by an operator while escalated")
            if self._cancel_event.is_set():
                raise EscalationAbandoned("escalation was force-cancelled before a human resumed it")
            try:
                return self._resume_queue.get(timeout=1.0)
            except queue.Empty:
                continue

    def resume(self, note: str) -> None:
        self._resume_queue.put(note)

    def request_takeover(self) -> None:
        self._takeover_event.set()

    def takeover_requested(self) -> bool:
        return self._takeover_event.is_set()

    def clear_takeover(self) -> None:
        self._takeover_event.clear()

    def on_novnc_url(self, url: str) -> None:
        if self._on_novnc_url is not None:
            self._on_novnc_url(url)

    def interrupt(self) -> None:
        # Also sets the cancel event, so a run blocked in wait_for_resume() wakes
        # immediately instead of waiting for a step boundary that will never come.
        # wait_for_resume() checks the interrupt event first, so it still raises
        # RunInterrupted rather than EscalationAbandoned.
        self._interrupt_event.set()
        self._cancel_event.set()

    def interrupt_requested(self) -> bool:
        return self._interrupt_event.is_set()

    def cancel(self) -> None:
        self._cancel_event.set()
