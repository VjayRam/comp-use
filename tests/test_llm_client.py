from unittest.mock import patch, MagicMock

from comp_use.config import load_settings
from comp_use.llm_client import _SYSTEM_PROMPT, _TOOL_SCHEMA, FakeLLMClient, OpenRouterClient, parse_decision
from comp_use.schemas import Locator, LocatorStrategy


def test_fake_llm_client_returns_scripted_actions_in_order():
    client = FakeLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "text", "value": {"text": "Search"}},
             "target": None, "text": None, "value_source": None, "done": False},
            {"action": "finish", "locator": None, "target": None, "text": None,
             "value_source": None, "done": True},
        ]
    )
    first = client.decide_next_action(goal="find member", observed_tree="tree", screenshot_b64=None, history=[])
    second = client.decide_next_action(goal="find member", observed_tree="tree", screenshot_b64=None, history=[])
    assert first["action"] == "click"
    assert second["done"] is True


@patch("comp_use.llm_client.requests.post")
def test_openrouter_client_parses_tool_call_response(mock_post):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "function": {
                        "arguments": (
                            '{"action": "click", "locator": {"strategy": "text", '
                            '"value": {"text": "Search"}}, "target": null, "text": null, '
                            '"value_source": null, "done": false}'
                        )
                    }
                }]
            }
        }]
    }
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response

    settings = load_settings()
    settings.openrouter_api_key = "test-key"
    client = OpenRouterClient(settings)

    result = client.decide_next_action(
        goal="find member", observed_tree="tree", screenshot_b64=None, history=[]
    )
    assert result["action"] == "click"
    assert mock_post.called


@patch("comp_use.llm_client.requests.post")
def test_openrouter_client_parses_json_content_without_tool_calls(mock_post):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{
            "message": {
                "content": (
                    '{"action": "type_text", "locator": {"strategy": "role", '
                    '"value": {"role": "textbox", "name": "Member ID"}}, '
                    '"text": "12345", "done": false}'
                )
            }
        }]
    }
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response

    settings = load_settings()
    settings.openrouter_api_key = "test-key"
    client = OpenRouterClient(settings)
    result = client.decide_next_action(
        goal="find member", observed_tree="tree", screenshot_b64=None, history=[]
    )
    assert result["action"] == "type_text"
    assert result["locator"]["strategy"] == "role"


def test_parse_decision_tolerates_trailing_text_after_the_json_object():
    # Reproduces a real live crash: a free-tier model appended extra
    # commentary after a perfectly valid JSON decision, and json.loads()
    # requires the ENTIRE string to be exactly one JSON value - "Extra data"
    # crashed the whole discover run instead of using the decision that was
    # actually there.
    data = {
        "choices": [{
            "message": {
                "content": '{"action": "finish", "locator": null, "target": null, '
                           '"text": null, "value_source": null, "done": true} '
                           "Let me know if you need anything else!"
            }
        }]
    }
    result = parse_decision(data)
    assert result["action"] == "finish"
    assert result["done"] is True


def test_tool_schema_declares_extract_as():
    properties = _TOOL_SCHEMA["function"]["parameters"]["properties"]
    assert "extract_as" in properties
    assert "extract" in properties["action"]["enum"]


def test_system_prompt_explains_and_demonstrates_extract():
    assert "extract" in _SYSTEM_PROMPT
    assert "extract_as" in _SYSTEM_PROMPT
    # a worked example, not just a mention in prose - mirrors how type_text/click/finish are taught
    assert '"action":"extract"' in _SYSTEM_PROMPT


@patch("comp_use.llm_client.requests.post")
def test_openrouter_client_sends_image_content_block_when_screenshot_provided(mock_post):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{
            "message": {
                "content": '{"action": "click", "locator": {"strategy": "role", '
                           '"value": {"role": "button", "name": "Search"}}, "done": false}'
            }
        }]
    }
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response

    settings = load_settings()
    settings.openrouter_api_key = "test-key"
    settings.openrouter_model = "text-only-model"
    settings.openrouter_vision_model = "vision-model"
    client = OpenRouterClient(settings)

    client.decide_next_action(
        goal="find member", observed_tree="tree", screenshot_b64="ZmFrZXBuZw==", history=[]
    )

    assert mock_post.called
    sent_payload = mock_post.call_args.kwargs["json"]
    assert sent_payload["model"] == "vision-model"
    user_content = sent_payload["messages"][1]["content"]
    assert isinstance(user_content, list)
    types = [block["type"] for block in user_content]
    assert "text" in types
    assert "image_url" in types
    image_block = next(b for b in user_content if b["type"] == "image_url")
    assert image_block["image_url"]["url"] == "data:image/png;base64,ZmFrZXBuZw=="


@patch("comp_use.llm_client.requests.post")
def test_openrouter_client_uses_text_model_and_plain_string_when_no_screenshot(mock_post):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": '{"action": "finish", "done": true}'}}]
    }
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response

    settings = load_settings()
    settings.openrouter_api_key = "test-key"
    settings.openrouter_model = "text-only-model"
    settings.openrouter_vision_model = "vision-model"
    client = OpenRouterClient(settings)

    client.decide_next_action(goal="find member", observed_tree="tree", screenshot_b64=None, history=[])

    sent_payload = mock_post.call_args.kwargs["json"]
    assert sent_payload["model"] == "text-only-model"
    assert isinstance(sent_payload["messages"][1]["content"], str)


def test_fake_llm_client_returns_scripted_drift_diagnosis():
    client = FakeLLMClient(
        scripted_actions=[],
        scripted_drift_diagnoses=[{"found": True, "locator": {"strategy": "role", "value": {"role": "button", "name": "Submit Transfer"}}}],
    )
    result = client.diagnose_drift(
        expected_locator={"strategy": "role", "value": {"role": "button", "name": "Confirm Transfer"}},
        screenshot_b64="ZmFrZXBuZw==",
    )
    assert result["found"] is True
    assert result["locator"]["value"]["name"] == "Submit Transfer"


@patch("comp_use.llm_client.requests.post")
def test_openrouter_client_diagnose_drift_sends_screenshot_and_uses_vision_model(mock_post):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "function": {
                        "arguments": '{"found": true, "locator": {"strategy": "role", '
                                     '"value": {"role": "button", "name": "Submit Transfer"}}, '
                                     '"reasoning": "renamed but same position"}'
                    }
                }]
            }
        }]
    }
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response

    settings = load_settings()
    settings.openrouter_api_key = "test-key"
    settings.openrouter_model = "text-only-model"
    settings.openrouter_vision_model = "vision-model"
    client = OpenRouterClient(settings)

    result = client.diagnose_drift(
        expected_locator={"strategy": "role", "value": {"role": "button", "name": "Confirm Transfer"}},
        screenshot_b64="ZmFrZXBuZw==",
    )

    assert result["found"] is True
    assert result["locator"]["value"]["name"] == "Submit Transfer"
    sent_payload = mock_post.call_args.kwargs["json"]
    assert sent_payload["model"] == "vision-model"  # always vision - a screenshot is mandatory input here
    user_content = sent_payload["messages"][1]["content"]
    assert isinstance(user_content, list)
    assert any(b["type"] == "image_url" for b in user_content)


@patch("comp_use.llm_client.requests.post")
def test_openrouter_client_diagnose_drift_tolerates_wrapped_response_and_reason_key(mock_post):
    # Reproduces a real response shape observed live from a free-tier vision
    # model: it echoed the tool's own name back as a wrapping key instead of a
    # flat object, and used "reason" instead of the schema's "reasoning". The
    # model's diagnosis itself was still usable and shouldn't be discarded.
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{
            "message": {
                "content": '{"diagnose_drift": {"found": true, '
                           '"locator": {"strategy": "role", "value": {"role": "button", "name": "Confirm Transfer"}}, '
                           '"reason": "same position, relabeled"}}'
            }
        }]
    }
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response

    settings = load_settings()
    settings.openrouter_api_key = "test-key"
    client = OpenRouterClient(settings)

    result = client.diagnose_drift(
        expected_locator={"strategy": "role", "value": {"role": "button", "name": "Cofirm Transfer"}},
        screenshot_b64="ZmFrZXBuZw==",
    )

    assert result["found"] is True
    assert result["locator"]["value"]["name"] == "Confirm Transfer"
    assert result["reasoning"] == "same position, relabeled"


@patch("comp_use.llm_client.requests.post")
def test_openrouter_client_diagnose_drift_returns_not_found_when_model_says_so(mock_post):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": '{"found": false, "reasoning": "no similar control visible"}'}}]
    }
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response

    settings = load_settings()
    settings.openrouter_api_key = "test-key"
    client = OpenRouterClient(settings)

    result = client.diagnose_drift(
        expected_locator={"strategy": "role", "value": {"role": "button", "name": "Confirm Transfer"}},
        screenshot_b64="ZmFrZXBuZw==",
    )

    assert result["found"] is False
