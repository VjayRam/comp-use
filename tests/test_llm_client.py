from unittest.mock import patch, MagicMock

from comp_use.config import load_settings
from comp_use.llm_client import FakeLLMClient, OpenRouterClient


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
