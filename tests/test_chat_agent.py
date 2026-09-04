from comp_use.chat.agent import ChatAgent
from comp_use.chat.session import ChatSessionManager
from comp_use.llm_client import FakeLLMClient

_CATALOG = [
    {"capability_name": "lookup_member", "description": "Looks up a member.",
     "target_base_url": "https://web-sample.interface-hiring.com",
     "input_schema": [{"name": "member_id", "type": "string", "required": True}]},
    {"capability_name": "other_site_thing", "description": "Something else.",
     "target_base_url": "http://localhost:5000", "input_schema": []},
]


def _agent():
    return ChatAgent(FakeLLMClient())  # target-site turns never call the LLM


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
