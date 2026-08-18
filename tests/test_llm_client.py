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
