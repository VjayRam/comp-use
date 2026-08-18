from comp_use.config import load_settings
from comp_use.discovery.agent import DiscoveryAgent
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.llm_client import FakeLLMClient
from comp_use.schemas import ActionType


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
