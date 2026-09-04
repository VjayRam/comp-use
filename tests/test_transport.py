import builtins
import threading
import time

import pytest

from comp_use.escalation.transport import LocalSharedBrowserTransport, QueueTransport
from comp_use.schemas import InterventionRequest


def test_wait_for_resume_raises_clear_error_on_closed_stdin(monkeypatch):
    # A scripted/non-interactive invocation whose stdin is already closed before
    # 'resume' is ever typed must fail with a clear message, not a raw EOFError.
    monkeypatch.setattr(builtins, "input", lambda *_: (_ for _ in ()).throw(EOFError()))
    transport = LocalSharedBrowserTransport()

    with pytest.raises(RuntimeError, match="interactive terminal"):
        transport.wait_for_resume()


def test_wait_for_resume_survives_stdin_closing_after_resume_is_typed(monkeypatch):
    # Reproduces a real bug: `printf 'resume\n' | comp-use replay ...` supplies exactly
    # one line then closes stdin. The first input() (reading "resume") succeeds, but the
    # second input() (the optional note) then hits EOF - this must degrade to an empty
    # note, not crash, since the note was already documented as optional.
    calls = iter(["resume"])

    def fake_input(_prompt):
        try:
            return next(calls)
        except StopIteration:
            raise EOFError()

    monkeypatch.setattr(builtins, "input", fake_input)
    transport = LocalSharedBrowserTransport()

    note = transport.wait_for_resume()

    assert note == ""


def test_wait_for_resume_returns_the_typed_note_normally(monkeypatch):
    calls = iter(["resume", "reviewed and approved"])
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(calls))
    transport = LocalSharedBrowserTransport()

    note = transport.wait_for_resume()

    assert note == "reviewed and approved"


def test_queue_transport_notify_invokes_the_callback_with_the_exact_request():
    received = []
    transport = QueueTransport(on_notify=received.append)
    request = InterventionRequest(run_id="r1", capability_or_goal="lookup_member", reason="stuck")

    transport.notify(request)

    assert received == [request]


def test_queue_transport_wait_for_resume_blocks_until_resume_is_called():
    transport = QueueTransport(on_notify=lambda r: None)
    result = {}

    def waiter():
        result["note"] = transport.wait_for_resume()

    waiter_thread = threading.Thread(target=waiter)
    waiter_thread.start()
    time.sleep(0.05)
    assert "note" not in result  # still blocked - nobody has called resume() yet

    transport.resume("handled it via the API")
    waiter_thread.join(timeout=1)

    assert result["note"] == "handled it via the API"


def test_queue_transport_takeover_starts_unrequested():
    transport = QueueTransport(on_notify=lambda r: None)
    assert transport.takeover_requested() is False


def test_queue_transport_request_takeover_then_clear_takeover_round_trips():
    transport = QueueTransport(on_notify=lambda r: None)

    transport.request_takeover()
    assert transport.takeover_requested() is True

    transport.clear_takeover()
    assert transport.takeover_requested() is False


def test_control_transport_default_takeover_methods_are_inert_no_ops():
    # The base ControlTransport (and LocalSharedBrowserTransport, which never
    # overrides them) has no HTTP endpoint to wire a takeover request to - these
    # must be harmless no-ops, not NotImplementedError, so a CLI-local run never
    # crashes on the loop's takeover_requested() poll.
    transport = LocalSharedBrowserTransport()
    transport.request_takeover()
    assert transport.takeover_requested() is False
    transport.clear_takeover()
