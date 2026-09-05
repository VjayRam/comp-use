from comp_use.chat.agent import ChatAgent, _RESOLVE_PENDING_TOOL as _RESOLVE_PENDING_TOOL_FOR_TEST
from comp_use.chat.session import ChatSessionManager, PendingAction
from comp_use.llm_client import FakeLLMClient

_CATALOG = [
    {"capability_name": "lookup_member", "description": "Looks up a member.",
     "target_base_url": "https://web-sample.interface-hiring.com",
     "input_schema": [{"name": "member_id", "type": "string", "required": True}]},
    {"capability_name": "other_site_thing", "description": "Something else.",
     "target_base_url": "http://localhost:5000", "input_schema": []},
]

_ONE_SITE_CATALOG = [
    {"capability_name": "lookup_member", "description": "Looks up a member.",
     "target_base_url": "https://web-sample.interface-hiring.com",
     "input_schema": [{"name": "member_id", "type": "string", "required": True},
                       {"name": "password", "type": "string", "required": False}]},
]


class _RecordingLLM:
    """A minimal test double that captures what ChatAgent sent, instead of
    scripting a reply - used for tests that assert on the OUTGOING request
    (e.g. which capabilities were offered), not just the returned decision."""

    def __init__(self, reply):
        self.reply = reply
        self.seen_tools = None
        self.seen_messages = None
        self.seen_tool_choice = None

    def chat(self, messages, tools, tool_choice="auto"):
        self.seen_messages = messages
        self.seen_tools = tools
        self.seen_tool_choice = tool_choice
        return self.reply


def _agent():
    return ChatAgent(FakeLLMClient())  # target-site turns never call the LLM


def _session_with_site(site="https://web-sample.interface-hiring.com"):
    session = ChatSessionManager().create()
    session.target_site = site
    return session


def test_first_turn_asks_for_a_target_site_listing_known_sites():
    agent = _agent()
    session = ChatSessionManager().create()

    result = agent.turn(session, "check a balance", _CATALOG)

    assert "https://web-sample.interface-hiring.com" in result.reply_text
    assert "http://localhost:5000" in result.reply_text
    assert session.target_site is None
    assert result.to_execute is None


def test_picking_a_known_site_by_number_sets_target_site():
    agent = _agent()
    session = ChatSessionManager().create()
    agent.turn(session, "check a balance", _CATALOG)  # first turn: asks for a site

    result = agent.turn(session, "1", _CATALOG)

    assert session.target_site == "https://web-sample.interface-hiring.com"
    assert "https://web-sample.interface-hiring.com" in result.reply_text


def test_picking_a_known_site_by_exact_url_sets_target_site():
    agent = _agent()
    session = ChatSessionManager().create()
    agent.turn(session, "check a balance", _CATALOG)

    agent.turn(session, "http://localhost:5000", _CATALOG)

    assert session.target_site == "http://localhost:5000"


def test_entering_a_brand_new_url_sets_target_site_to_its_origin():
    agent = _agent()
    session = ChatSessionManager().create()
    agent.turn(session, "check a balance", _CATALOG)

    agent.turn(session, "https://new-target.example.com/some/path", _CATALOG)

    assert session.target_site == "https://new-target.example.com"


def test_an_unrecognized_reply_re_asks_without_setting_target_site():
    agent = _agent()
    session = ChatSessionManager().create()
    agent.turn(session, "check a balance", _CATALOG)

    result = agent.turn(session, "banana", _CATALOG)

    assert session.target_site is None
    assert "https://web-sample.interface-hiring.com" in result.reply_text


def test_target_site_prompt_offers_a_new_url_option_when_catalog_is_empty():
    agent = _agent()
    session = ChatSessionManager().create()

    result = agent.turn(session, "check a balance", [])

    assert "URL" in result.reply_text


def test_plain_text_reply_is_passed_through_and_appended_to_history():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[{"type": "text", "text": "Sure, which member?"}]))
    session = _session_with_site()

    result = agent.turn(session, "check a balance", _ONE_SITE_CATALOG)

    assert result.reply_text == "Sure, which member?"
    assert result.to_execute is None
    assert session.messages[-1] == {"role": "assistant", "content": "Sure, which member?"}


def test_propose_invoke_with_complete_params_asks_for_confirmation_and_sets_pending():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "lookup_member", "params": {"member_id": "100234"}}},
    ]))
    session = _session_with_site()

    result = agent.turn(session, "check member 100234", _ONE_SITE_CATALOG)

    assert "lookup_member" in result.reply_text
    assert "100234" in result.reply_text
    assert result.to_execute is None  # not executed yet - awaiting confirm
    assert session.pending_action.kind == "invoke"
    assert session.pending_action.capability_name == "lookup_member"
    assert session.pending_action.params == {"member_id": "100234"}


def test_propose_invoke_masks_sensitive_param_values_in_the_confirmation_text():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "lookup_member", "params": {"member_id": "100234", "password": "hunter2"}}},
    ]))
    session = _session_with_site()

    result = agent.turn(session, "sign on and check member 100234", _ONE_SITE_CATALOG)

    assert "hunter2" not in result.reply_text
    assert "••••••" in result.reply_text
    assert session.pending_action.params["password"] == "hunter2"  # stored value stays real


def test_propose_invoke_with_missing_required_param_asks_for_it_without_setting_pending():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "lookup_member", "params": {}}},
    ]))
    session = _session_with_site()

    result = agent.turn(session, "check a member", _ONE_SITE_CATALOG)

    assert "member_id" in result.reply_text
    assert session.pending_action is None


def test_propose_invoke_for_an_unknown_capability_reports_it_without_setting_pending():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "does_not_exist", "params": {}}},
    ]))
    session = _session_with_site()

    result = agent.turn(session, "do something odd", _ONE_SITE_CATALOG)

    assert "does_not_exist" in result.reply_text
    assert session.pending_action is None


def test_normal_turn_only_offers_capabilities_matching_the_session_target_site():
    two_site_catalog = _ONE_SITE_CATALOG + [
        {"capability_name": "other_site_thing", "description": "d",
         "target_base_url": "http://localhost:5000", "input_schema": []},
    ]
    llm = _RecordingLLM({"type": "text", "text": "ok"})
    agent = ChatAgent(llm)
    session = _session_with_site("https://web-sample.interface-hiring.com")

    agent.turn(session, "do the other site thing", two_site_catalog)

    system_content = llm.seen_messages[0]["content"]
    assert "lookup_member" in system_content
    assert "other_site_thing" not in system_content


def test_propose_discovery_asks_for_confirmation_and_sets_pending_with_a_slugified_name():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_discovery",
         "arguments": {"capability_name": "Close a Share!", "goal": "close a share for a member"}},
    ]))
    session = _session_with_site()

    result = agent.turn(session, "close a share for member 100234", _ONE_SITE_CATALOG)

    assert "close_a_share" in result.reply_text
    assert session.target_site in result.reply_text
    assert result.to_execute is None
    assert session.pending_action.kind == "discover"
    assert session.pending_action.capability_name == "close_a_share"
    assert session.pending_action.goal == "close a share for a member"


def test_propose_discovery_passes_through_param_hints():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_discovery",
         "arguments": {"capability_name": "close_a_share", "goal": "close a share", "param_hints": ["share_id"]}},
    ]))
    session = _session_with_site()

    agent.turn(session, "close a share", _ONE_SITE_CATALOG)

    assert session.pending_action.param_hints == ["share_id"]


def test_slugify_collapses_non_alphanumerics_and_lowercases():
    from comp_use.chat.agent import _slugify
    assert _slugify("Close a Share!") == "close_a_share"
    assert _slugify("  already_snake_case  ") == "already_snake_case"
    assert _slugify("!!!") == "capability"


def test_confirm_clears_pending_and_returns_it_as_to_execute():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "lookup_member", "params": {"member_id": "100234"}}},
        {"type": "tool_call", "name": "resolve_pending", "arguments": {"decision": "confirm"}},
    ]))
    session = _session_with_site()
    agent.turn(session, "check member 100234", _ONE_SITE_CATALOG)

    result = agent.turn(session, "yes", _ONE_SITE_CATALOG)

    assert session.pending_action is None
    assert result.to_execute is not None
    assert result.to_execute.kind == "invoke"
    assert result.to_execute.capability_name == "lookup_member"


def test_cancel_clears_pending_without_executing_anything():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "lookup_member", "params": {"member_id": "100234"}}},
        {"type": "tool_call", "name": "resolve_pending", "arguments": {"decision": "cancel"}},
    ]))
    session = _session_with_site()
    agent.turn(session, "check member 100234", _ONE_SITE_CATALOG)

    result = agent.turn(session, "no, never mind", _ONE_SITE_CATALOG)

    assert session.pending_action is None
    assert result.to_execute is None


def test_amend_clears_pending_and_falls_through_to_a_normal_turn():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "lookup_member", "params": {"member_id": "100234"}}},
        {"type": "tool_call", "name": "resolve_pending", "arguments": {"decision": "amend"}},
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "lookup_member", "params": {"member_id": "999999"}}},
    ]))
    session = _session_with_site()
    agent.turn(session, "check member 100234", _ONE_SITE_CATALOG)

    result = agent.turn(session, "actually make it 999999", _ONE_SITE_CATALOG)

    assert session.pending_action.params == {"member_id": "999999"}
    assert result.to_execute is None


def test_resolve_pending_is_called_with_a_forced_tool_choice():
    # An exact "yes" is now resolved deterministically without ever touching the
    # LLM (see _parse_explicit_decision) - only a reply that isn't an exact
    # affirmative/negative reaches this forced tool call, so this test uses one.
    llm = _RecordingLLM({"type": "tool_call", "name": "resolve_pending", "arguments": {"decision": "confirm"}})
    agent = ChatAgent(llm)
    session = _session_with_site()
    session.pending_action = PendingAction(kind="invoke", capability_name="lookup_member", params={"member_id": "100234"})

    agent.turn(session, "sure, go for it", _ONE_SITE_CATALOG)

    assert llm.seen_tools == [_RESOLVE_PENDING_TOOL_FOR_TEST]
    assert llm.seen_tool_choice == {"type": "function", "function": {"name": "resolve_pending"}}


def test_exact_yes_confirms_without_calling_the_llm():
    # The core of the fix: a free-tier model not honoring the forced resolve_pending
    # tool call previously meant "yes" could loop forever (decision defaulted to
    # "amend"). An exact "yes"/"y"/"confirm"/etc. must resolve to "confirm" without
    # ever reaching the LLM, so model flakiness can't affect it at all.
    llm = _RecordingLLM({"type": "text", "text": "unused"})
    agent = ChatAgent(llm)
    session = _session_with_site()
    session.pending_action = PendingAction(kind="invoke", capability_name="lookup_member", params={"member_id": "100234"})

    result = agent.turn(session, "yes", _ONE_SITE_CATALOG)

    assert llm.seen_tools is None
    assert session.pending_action is None
    assert result.to_execute is not None
    assert result.to_execute.capability_name == "lookup_member"


def test_exact_no_cancels_without_calling_the_llm():
    llm = _RecordingLLM({"type": "text", "text": "unused"})
    agent = ChatAgent(llm)
    session = _session_with_site()
    session.pending_action = PendingAction(kind="invoke", capability_name="lookup_member", params={"member_id": "100234"})

    result = agent.turn(session, "no", _ONE_SITE_CATALOG)

    assert llm.seen_tools is None
    assert session.pending_action is None
    assert result.to_execute is None
