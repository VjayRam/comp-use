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
    def __init__(self):
        self.actions = []
        self.url = "http://localhost:5000/member/search"

    def act(self, action, locator, target, text):
        self.actions.append((action, locator, target, text))
        if action == ActionType.NAVIGATE:
            self.url = target

    def observe(self):
        from comp_use.surface import ObservedState
        return ObservedState(accessibility_tree="fake tree", url=self.url)

    def check_checkpoint(self, checkpoint):
        return True

    def screenshot(self):
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


def test_agent_stops_at_max_steps_without_finish():
    settings = load_settings()
    surface = FakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "text", "value": {"text": "x"}},
             "target": None, "text": None, "value_source": None, "done": False},
        ] * 5
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


def test_agent_records_literal_value_on_step_for_fixed_and_goal_parameter_text(tmp_path):
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
                "value_source": {"type": "fixed", "reason": "boilerplate note"},
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

    assert trace.steps[0].value_source.type == "fixed"
    assert trace.steps[0].value == "Opened at teller request"
    assert trace.steps[1].value_source.type == "goal_parameter"
    # goal_parameter steps must NOT persist their discovery-time literal (a real
    # member ID here) into the artifact - replay always substitutes the caller's
    # own params, and the recorded example has no business being saved/committed.
    assert trace.steps[1].value is None


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
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "text", "value": {"text": "x"}},
             "target": None, "text": None, "value_source": None, "done": False},
        ] * 5
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
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "text", "value": {"text": "x"}},
             "target": None, "text": None, "value_source": None, "done": False},
        ] * 3
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
