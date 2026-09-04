import json

from comp_use.config import load_settings
from comp_use.discovery.agent import DiscoveryAgent
from comp_use.escalation.controller import EscalationController
from comp_use.escalation.transport import ControlTransport
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.llm_client import FakeLLMClient
from comp_use.schemas import ActionType


class FakeTransport(ControlTransport):
    def __init__(self):
        self.notified = []
        self.resumed = False

    def notify(self, request):
        self.notified.append(request)

    def wait_for_resume(self):
        self.resumed = True
        return "unblocked the agent"


class FakeSurface:
    def __init__(self, raise_on_action_call=None, raise_on_screenshot_call=None):
        self.actions = []
        self.url = "http://localhost:5000/member/search"
        self.raise_on_action_call = raise_on_action_call
        self._act_calls = 0
        self.raise_on_screenshot_call = raise_on_screenshot_call
        self._screenshot_calls = 0

    def act(self, action, locator, target, text):
        self._act_calls += 1
        if self.raise_on_action_call == self._act_calls:
            raise TimeoutError(f"locator not found for {action}")
        self.actions.append((action, locator, target, text))
        if action == ActionType.NAVIGATE:
            self.url = target

    def observe(self):
        from comp_use.surface import ObservedState
        return ObservedState(accessibility_tree="fake tree", url=self.url)

    def check_checkpoint(self, checkpoint):
        return True

    def screenshot(self):
        self._screenshot_calls += 1
        if self.raise_on_screenshot_call == self._screenshot_calls:
            raise TimeoutError("Page.screenshot: Timeout 30000ms exceeded.")
        return b"fakepng"

    def current_url(self):
        return self.url


def test_agent_runs_until_finish_and_records_steps(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "text", "value": {"text": "Search"}},
             "target": None, "text": None, "value_source": None, "done": False},
            {"action": "type_text", "locator": {"strategy": "role", "value": {"role": "textbox", "name": "Member ID"}},
             "target": None, "text": "12345", "value_source": {"type": "goal_parameter", "param_name": "member_id", "param_type": "string"}, "done": False},
            {"action": "finish", "locator": None, "target": None, "text": None, "value_source": None, "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_test")
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10)

    trace = agent.run(goal="Look up member 12345", start_url="http://localhost:5000/member/search")

    assert trace.succeeded is True
    assert len(trace.steps) == 2
    assert trace.steps[1].value_source.param_name == "member_id"


def test_agent_skips_step_instead_of_baking_in_literal_when_value_source_is_malformed(tmp_path):
    # A value_source that fails Pydantic validation (e.g. a typo'd "type") must not
    # be silently treated as "no value_source" - that would bake this turn's literal
    # discovery-time text (a real member ID here) into the artifact instead of
    # binding it to a replay-time parameter. It must be skipped and retried instead.
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "type_text", "locator": {"strategy": "role", "value": {"role": "textbox", "name": "Member ID"}},
             "target": None, "text": "12345", "value_source": {"type": "goal_param", "param_name": "member_id"}, "done": False},
            {"action": "type_text", "locator": {"strategy": "role", "value": {"role": "textbox", "name": "Member ID"}},
             "target": None, "text": "12345", "value_source": {"type": "goal_parameter", "param_name": "member_id", "param_type": "string"}, "done": False},
            {"action": "finish", "locator": None, "target": None, "text": None, "value_source": None, "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_bad_value_source")
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10)

    trace = agent.run(goal="Look up member 12345", start_url="http://localhost:5000/member/search")

    assert trace.succeeded is True
    assert len(trace.steps) == 1  # the malformed first attempt was skipped, not recorded
    assert trace.steps[0].value_source.param_name == "member_id"
    assert trace.steps[0].value is None  # bound to the param, not baked in as a literal


def test_agent_stops_at_max_steps_without_finish():
    settings = load_settings()
    surface = FakeSurface()
    # Distinct locators per step (not the same one repeated) - keeps this test
    # isolated to the max_steps path, not also tripping loop detection (which has
    # its own dedicated test below).
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "text", "value": {"text": f"x{i}"}},
             "target": None, "text": None, "value_source": None, "done": False}
            for i in range(5)
        ]
    )
    guardrail = Guardrail(settings)
    from comp_use.evidence import EvidenceLogger
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        settings.evidence_dir = d
        evidence = EvidenceLogger(settings, guardrail, run_id="run_test2")
        agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=3)
        trace = agent.run(goal="do something", start_url="http://localhost:5000/member/search")
        assert trace.succeeded is False
        assert len(trace.steps) == 3


def test_agent_skips_type_text_without_locator_then_continues(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "type_text", "text": "12345", "done": False},
            {"action": "type_text", "locator": {"strategy": "role", "value": {"role": "textbox", "name": "Member ID"}},
             "target": None, "text": "12345", "value_source": {"type": "goal_parameter", "param_name": "member_id", "param_type": "string"}, "done": False},
            {"action": "finish", "locator": None, "target": None, "text": None, "value_source": None, "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_skip")
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10)

    trace = agent.run(goal="Look up member 12345", start_url="http://localhost:5000/member/search")

    assert trace.succeeded is True
    assert len(trace.steps) == 1
    assert trace.steps[0].action == ActionType.TYPE_TEXT
    assert surface.actions[1][0] == ActionType.TYPE_TEXT
    assert surface.actions[1][1] is not None


def test_agent_records_literal_value_for_literal_and_captures_example_for_goal_parameter(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {
                "action": "type_text",
                "locator": {"strategy": "role", "value": {"role": "textbox", "name": "Note"}},
                "target": None,
                "text": "Opened at teller request",
                "value_source": None,
                "done": False,
            },
            {
                "action": "type_text",
                "locator": {"strategy": "role", "value": {"role": "textbox", "name": "Member ID"}},
                "target": None,
                "text": "12345",
                "value_source": {"type": "goal_parameter", "param_name": "member_id", "param_type": "string"},
                "done": False,
            },
            {"action": "finish", "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_values")
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10)

    trace = agent.run(goal="Open sub-account", start_url="http://localhost:5000/member/search")

    # value_source=None means "literal" - ValueSource has only one meaning now
    # (goal_parameter), see schemas.py.
    assert trace.steps[0].value_source is None
    assert trace.steps[0].value == "Opened at teller request"
    assert trace.steps[1].value_source.type == "goal_parameter"
    # goal_parameter steps must NOT persist their discovery-time literal (a real
    # member ID here) onto the Step - replay always substitutes the caller's own
    # params, and the recorded example has no business being saved/committed there.
    assert trace.steps[1].value is None
    # It IS captured off to the side, for compile_artifact() to surface as
    # InputParam.example - see EXT_TASK_FIXES.md's ValueSource cleanup.
    assert trace.parameter_examples == {"member_id": "12345"}


def test_agent_skips_empty_locator_object(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "type_text", "locator": {}, "text": "12345", "done": False},
            {"action": "finish", "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_empty_loc")
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=5)
    trace = agent.run(goal="Look up member 12345", start_url="http://localhost:5000/member/search")
    assert trace.succeeded is True
    assert len(trace.steps) == 0


def test_agent_escalates_when_max_steps_reached_without_finish(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    # Distinct locators per step - isolates this test to the max_steps path, not
    # loop detection (dedicated test below).
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "text", "value": {"text": f"x{i}"}},
             "target": None, "text": None, "value_source": None, "done": False}
            for i in range(5)
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_stuck")
    transport = FakeTransport()
    escalation = EscalationController(evidence, transport)
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=3, escalation=escalation)

    trace = agent.run(goal="do something", start_url="http://localhost:5000/member/search")

    assert trace.succeeded is False
    assert len(transport.notified) == 1
    request = transport.notified[0]
    assert request.run_id == "run_stuck"
    assert request.capability_or_goal == "do something"
    assert "max_steps" in request.reason
    assert transport.resumed is True


class TakeoverThenClearTransport(FakeTransport):
    """Reports a pending takeover exactly once - like the real QueueTransport,
    where clear_takeover() (called inside EscalationController.escalate()) turns
    it back off so it can't fire a second, redundant escalation."""

    def __init__(self):
        super().__init__()
        self._pending = True

    def takeover_requested(self):
        return self._pending

    def clear_takeover(self):
        self._pending = False


def test_agent_escalates_for_a_pending_takeover_before_the_next_decision(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    # Never consulted for the takeover step itself (the check happens before any
    # decision is made) - only for the one decision made after resuming.
    llm = FakeLLMClient(scripted_actions=[{"action": "finish", "done": True}])
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_takeover")
    transport = TakeoverThenClearTransport()
    escalation = EscalationController(evidence, transport)
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=5, escalation=escalation)

    trace = agent.run(goal="do something", start_url="http://localhost:5000/member/search")

    assert len(transport.notified) == 1
    assert transport.notified[0].reason == "manual takeover requested by operator"
    assert transport.resumed is True
    assert trace.succeeded is True


def test_agent_escalates_on_loop_of_valid_but_unproductive_decisions(tmp_path):
    """The exact same (url, action, locator) decided 3+ times must escalate as a
    loop, distinct from the dead-end/max_steps paths - both of which only trip on
    INVALID or missing decisions, never on a model that keeps making perfectly
    valid-looking decisions that just never progress. Observed live against
    MERIDIAN CORE's Place Hold flow (fill form -> Continue -> fail -> "Return to
    previous screen" -> refill -> Continue -> fail again), which cycled for the
    entire step budget without the pre-existing dead-end check ever firing."""
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "text", "value": {"text": "x"}},
             "target": None, "text": None, "value_source": None, "done": False},
        ] * 5
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_loop")
    transport = FakeTransport()
    escalation = EscalationController(evidence, transport)
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=5, escalation=escalation)

    agent.run(goal="do something", start_url="http://localhost:5000/member/search")

    loop_requests = [r for r in transport.notified if "loop detected" in r.reason]
    assert len(loop_requests) >= 1
    assert loop_requests[0].run_id == "run_loop"
    assert "attempted 3 times" in loop_requests[0].reason


def test_agent_aborts_and_escalates_when_a_click_navigates_off_allowlist(tmp_path):
    # The PRE-action allowlist check validates the url we're already on - it can't
    # catch a click that navigates somewhere new. This is the post-action check that
    # closes that gap: it must abort the run (not just skip the decision, since the
    # browser has already left the allowlisted domain and can't safely continue) and
    # escalate to a human.
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"

    class HijackingSurface(FakeSurface):
        def act(self, action, locator, target, text):
            super().act(action, locator, target, text)
            if action == ActionType.CLICK:
                self.url = "http://evil.example.com/hijacked"

    surface = HijackingSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "role", "value": {"role": "button", "name": "Search"}},
             "target": None, "text": None, "value_source": None, "done": False},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_hijacked")
    transport = FakeTransport()
    escalation = EscalationController(evidence, transport)
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10, escalation=escalation)

    trace = agent.run(goal="do something", start_url="http://localhost:5000/member/search")

    assert trace.succeeded is False
    assert trace.steps == []  # the off-allowlist click must never be recorded as a valid step
    assert len(transport.notified) == 1
    assert "post-action allowlist violation" in transport.notified[0].reason
    assert trace.final_url == "http://evil.example.com/hijacked"


class _RaisingTransport(ControlTransport):
    """Reproduces a real, live-observed failure: LocalSharedBrowserTransport raises
    RuntimeError when stdin isn't interactive. wait_for_resume() is the call that
    actually raises in production; raising from notify() here is an equally valid
    stand-in for "the transport itself failed" and keeps this test simple."""

    def notify(self, request):
        raise RuntimeError("stdin is closed/non-interactive")

    def wait_for_resume(self):
        raise AssertionError("should never be reached - notify() already raised")


def test_agent_returns_a_clean_trace_instead_of_crashing_when_escalation_transport_fails(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "role", "value": {"role": "button", "name": "Confirm Transfer"}},
             "done": False},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_escalation_transport_fails")
    escalation = EscalationController(evidence, _RaisingTransport())
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10, escalation=escalation)

    # Must return a clean RunTrace, never let the transport's RuntimeError escape
    # run() as a raw traceback.
    trace = agent.run(goal="Transfer funds", start_url="http://localhost:5000/member/search")

    assert trace.succeeded is False
    events = [
        json.loads(line)
        for line in (tmp_path / "evidence" / "run_escalation_transport_fails" / "log.jsonl").read_text().splitlines()
    ]
    failed_events = [e for e in events if e["event_type"] == "escalation_transport_failed"]
    assert len(failed_events) == 1
    assert "RuntimeError" in failed_events[0]["data"]["error"]


def test_agent_escalates_on_repeated_skipped_decisions(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "type_text", "text": "12345", "done": False},
            {"action": "type_text", "text": "12345", "done": False},
            {"action": "type_text", "text": "12345", "done": False},
            {"action": "finish", "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_dead_end")
    transport = FakeTransport()
    escalation = EscalationController(evidence, transport)
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10, escalation=escalation)

    trace = agent.run(goal="do something", start_url="http://localhost:5000/member/search")

    assert len(transport.notified) == 1
    assert "dead_end" in transport.notified[0].reason
    assert trace.succeeded is True


def test_agent_escalation_carries_a_real_screenshot_path(tmp_path):
    from pathlib import Path

    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    # Distinct locators per step - isolates this test to the max_steps path, not
    # loop detection (dedicated test below).
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "text", "value": {"text": f"x{i}"}},
             "target": None, "text": None, "value_source": None, "done": False}
            for i in range(3)
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_stuck_screenshot")
    transport = FakeTransport()
    escalation = EscalationController(evidence, transport)
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=3, escalation=escalation)

    agent.run(goal="do something", start_url="http://localhost:5000/member/search")

    assert len(transport.notified) == 1
    request = transport.notified[0]
    assert request.screenshot_path is not None
    assert Path(request.screenshot_path).exists()


def test_agent_records_extract_as_on_extract_step(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {
                "action": "extract",
                "locator": {"strategy": "role", "value": {"role": "generic", "name": "confirmation-number"}},
                "target": None,
                "text": None,
                "value_source": None,
                "extract_as": "confirmation_number",
                "done": False,
            },
            {"action": "finish", "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_extract")
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10)

    trace = agent.run(goal="Open sub-account", start_url="http://localhost:5000/member/search")

    assert len(trace.steps) == 1
    assert trace.steps[0].action == ActionType.EXTRACT
    assert trace.steps[0].extract_as == "confirmation_number"


class _RecordingLLMClient:
    """Wraps FakeLLMClient's scripted actions but records the screenshot_b64
    each call was made with, so tests can assert on the vision-fallback trigger."""

    def __init__(self, scripted_actions):
        self._inner = FakeLLMClient(scripted_actions=scripted_actions)
        self.screenshot_calls = []

    def decide_next_action(self, goal, observed_tree, screenshot_b64, history):
        self.screenshot_calls.append(screenshot_b64)
        return self._inner.decide_next_action(
            goal=goal, observed_tree=observed_tree, screenshot_b64=screenshot_b64, history=history
        )


def test_agent_survives_screenshot_capture_failure_during_vision_fallback(tmp_path):
    # Reproduces a real live crash: after a missing_locator skip sets
    # needs_vision_fallback, the screenshot capture itself (not any locator or
    # action) timed out - Page.screenshot() failing independent of any element
    # resolution problem - and this call sat outside the try/except that
    # protects surface.act(), crashing the whole discover run uncaught.
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface(raise_on_screenshot_call=1)
    llm = _RecordingLLMClient(
        scripted_actions=[
            {"action": "type_text", "text": "12345", "done": False},  # no locator -> triggers vision fallback next turn
            {"action": "click", "locator": {"strategy": "role", "value": {"role": "button", "name": "Search"}}, "done": False},
            {"action": "finish", "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_screenshot_failure")
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10)

    trace = agent.run(goal="Look up member 12345", start_url="http://localhost:5000/member/search")

    assert trace.succeeded is True
    # the screenshot capture failed, so the model degrades to text-only for
    # that turn instead of crashing - it never received a (broken) image
    assert llm.screenshot_calls[1] is None


def test_agent_retries_with_screenshot_after_missing_locator_skip(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    llm = _RecordingLLMClient(
        scripted_actions=[
            {"action": "type_text", "text": "12345", "done": False},  # no locator -> skip
            {"action": "click", "locator": {"strategy": "role", "value": {"role": "button", "name": "Search"}}, "done": False},
            {"action": "finish", "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_vision_fallback")
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10)

    agent.run(goal="Look up member 12345", start_url="http://localhost:5000/member/search")

    assert llm.screenshot_calls[0] is None
    assert llm.screenshot_calls[1] is not None
    assert llm.screenshot_calls[2] is None


def test_agent_escalates_before_acting_on_a_risky_decision(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    order: list[str] = []

    class OrderedFakeSurface(FakeSurface):
        def act(self, action, locator, target, text):
            if action == ActionType.CLICK:
                order.append("act")
            super().act(action, locator, target, text)

        def screenshot(self):
            order.append("screenshot")
            return super().screenshot()

    class OrderedFakeTransport(FakeTransport):
        def notify(self, request):
            order.append("escalate")
            super().notify(request)

    surface = OrderedFakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {
                "action": "click",
                "locator": {"strategy": "role", "value": {"role": "button", "name": "Confirm Transfer"}},
                "done": False,
            },
            {"action": "finish", "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_risky_discovery")
    transport = OrderedFakeTransport()
    escalation = EscalationController(evidence, transport)
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10, escalation=escalation)

    trace = agent.run(goal="Transfer funds", start_url="http://localhost:5000/member/search")

    assert len(transport.notified) == 1
    request = transport.notified[0]
    assert "risk_tier=risky" in request.reason
    assert request.screenshot_path is not None
    # the escalation (and its screenshot) must happen BEFORE the risky click is acted on
    assert order == ["screenshot", "escalate", "act"]
    assert trace.steps[0].risk_tier.value == "risky"


def test_agent_skips_risky_escalation_when_confirm_risky_is_true(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {
                "action": "click",
                "locator": {"strategy": "role", "value": {"role": "button", "name": "Confirm Transfer"}},
                "done": False,
            },
            {"action": "finish", "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_risky_confirmed")
    transport = FakeTransport()
    escalation = EscalationController(evidence, transport)
    agent = DiscoveryAgent(
        surface, llm, guardrail, evidence, max_steps=10, escalation=escalation, confirm_risky=True
    )

    agent.run(goal="Transfer funds", start_url="http://localhost:5000/member/search")

    assert len(transport.notified) == 0


def test_agent_action_failure_does_not_crash_and_is_recorded(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    # act call #1 is the initial NAVIGATE in agent.run(); call #2 is the first
    # decision's click, which will raise, simulating a locator that looked
    # valid but didn't actually resolve at Playwright-action time.
    surface = FakeSurface(raise_on_action_call=2)
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "role", "value": {"role": "button", "name": "Search"}}, "done": False},
            {"action": "finish", "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_action_failure")
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10)

    trace = agent.run(goal="Look up member 12345", start_url="http://localhost:5000/member/search")

    assert trace.succeeded is True
    assert len(trace.steps) == 0  # the failed click was never recorded as a step


def test_agent_action_failure_evidence_names_the_exception_type(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface(raise_on_action_call=2)
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "role", "value": {"role": "button", "name": "Search"}}, "done": False},
            {"action": "finish", "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_action_failure_type")
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10)

    agent.run(goal="Look up member 12345", start_url="http://localhost:5000/member/search")

    log_path = evidence.run_dir / "log.jsonl"
    import json
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    skipped = [e for e in events if e["event_type"] == "skipped_decision" and e["data"]["reason"] == "action_failed"]
    assert len(skipped) == 1
    # a bare exception message alone doesn't say what kind of failure it was -
    # the type name distinguishes a genuine code bug from an environmental one.
    assert skipped[0]["data"]["error"].startswith("TimeoutError:")


class _FlakyLLMClient:
    """Raises on specific calls (1-indexed), then defers to a FakeLLMClient
    for the rest - simulates a real network/parse failure mid-run without
    needing a real network."""

    def __init__(self, scripted_actions, raise_on_call):
        self._inner = FakeLLMClient(scripted_actions=scripted_actions)
        self._raise_on_call = raise_on_call
        self._calls = 0

    def decide_next_action(self, goal, observed_tree, screenshot_b64, history):
        self._calls += 1
        if self._calls == self._raise_on_call:
            raise ValueError("Extra data: line 1 column 105 (char 104)")
        return self._inner.decide_next_action(
            goal=goal, observed_tree=observed_tree, screenshot_b64=screenshot_b64, history=history
        )


def test_agent_survives_llm_call_failure_and_treats_it_as_a_skip(tmp_path):
    # Reproduces a real live crash: decide_next_action() raised (a malformed
    # JSON response from a real model) and the call sat completely outside
    # any try/except in the discovery loop - unlike surface.act() and
    # check_allowlist(), which issues 14/15 already protected.
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    llm = _FlakyLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "role", "value": {"role": "button", "name": "Search"}}, "done": False},
            {"action": "finish", "done": True},
        ],
        raise_on_call=1,
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_llm_call_failure")
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10)

    trace = agent.run(goal="Look up member 12345", start_url="http://localhost:5000/member/search")

    assert trace.succeeded is True

    log_path = evidence.run_dir / "log.jsonl"
    import json
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    skipped = [e for e in events if e["event_type"] == "skipped_decision" and e["data"]["reason"] == "llm_call_failed"]
    assert len(skipped) == 1
    assert skipped[0]["data"]["error"].startswith("ValueError:")


def test_agent_console_output_redacts_typed_values(tmp_path, capsys):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {
                "action": "type_text",
                "locator": {"strategy": "role", "value": {"role": "textbox", "name": "From Account"}},
                "text": "ACC-001",
                "value_source": None,
                "done": False,
            },
            {"action": "finish", "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_console_redaction")
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10)

    agent.run(goal="Transfer funds", start_url="http://localhost:5000/member/search")

    captured = capsys.readouterr()
    assert "ACC-001" not in captured.out
    assert "[REDACTED]" in captured.out
    assert "type_text" in captured.out  # the action name itself must survive redaction


def test_agent_treats_string_none_target_as_missing(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {
                "action": "click",
                "locator": {"strategy": "role", "value": {"role": "button", "name": "Search"}},
                "target": "None",
                "text": None,
                "done": False,
            },
            {"action": "finish", "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_none_target")
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=5)
    trace = agent.run(goal="Look up member 12345", start_url="http://localhost:5000/member/search")
    assert trace.succeeded is True
    assert len(trace.steps) == 1
    assert trace.steps[0].action == ActionType.CLICK


class RecordingLLMClient:
    """Records every observed_tree it was handed, so a test can assert the raw
    sensitive value never appeared in what the LLM actually received."""

    def __init__(self, actions):
        self._actions = list(actions)
        self._index = 0
        self.received_trees = []

    def decide_next_action(self, goal, observed_tree, screenshot_b64, history):
        self.received_trees.append(observed_tree)
        action = self._actions[self._index]
        self._index += 1
        return action


def test_sensitive_values_are_tokenized_to_the_llm_but_real_to_the_browser(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"

    class AccountRowSurface(FakeSurface):
        def observe(self):
            from comp_use.surface import ObservedState
            return ObservedState(
                accessibility_tree='link "ACC-000123" row  link "ACC-000456" row',
                url=self.url,
            )

    surface = AccountRowSurface()
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_tok")
    llm = RecordingLLMClient(actions=[])
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=5)

    # Simulate the model doing exactly what a real one would: copying a literal
    # name verbatim out of the (tokenized) tree it was shown, to select that row.
    token = agent.tokenizer.tokenize("ACC-000123")
    llm._actions = [
        {"action": "click", "locator": {"strategy": "text", "value": {"text": token}},
         "target": None, "text": None, "done": False},
        {"action": "finish", "done": True},
    ]

    trace = agent.run(goal="Look up member 12345", start_url="http://localhost:5000/member/search")

    # The LLM never saw the raw account number - only the placeholder token.
    assert all("ACC-000123" not in tree for tree in llm.received_trees)
    assert any(token in tree for tree in llm.received_trees)

    # But the real browser action, and the persisted artifact step, used the real value.
    # actions[0] is the agent's initial navigate; actions[1] is the click under test.
    assert surface.actions[1][1].value["text"] == "ACC-000123"
    assert trace.steps[0].locator.value["text"] == "ACC-000123"
