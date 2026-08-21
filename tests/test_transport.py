import builtins

import pytest

from comp_use.escalation.transport import LocalSharedBrowserTransport


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
