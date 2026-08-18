import json

import requests

from comp_use.config import Settings

_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "decide_next_action",
        "description": "Choose the next UI action to accomplish the goal.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["navigate", "click", "type_text", "select_option", "extract", "finish"]},
                "locator": {"type": ["object", "null"]},
                "target": {"type": ["string", "null"]},
                "text": {"type": ["string", "null"]},
                "value_source": {"type": ["object", "null"]},
                "done": {"type": "boolean"},
            },
            "required": ["action", "done"],
        },
    },
}


class LLMClient:
    def decide_next_action(self, goal: str, observed_tree: str, screenshot_b64: str | None, history: list[dict]) -> dict:
        raise NotImplementedError


class FakeLLMClient(LLMClient):
    def __init__(self, scripted_actions: list[dict]):
        self._actions = list(scripted_actions)
        self._index = 0

    def decide_next_action(self, goal: str, observed_tree: str, screenshot_b64: str | None, history: list[dict]) -> dict:
        action = self._actions[self._index]
        self._index += 1
        return action


class OpenRouterClient(LLMClient):
    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, settings: Settings):
        self.settings = settings

    def decide_next_action(self, goal: str, observed_tree: str, screenshot_b64: str | None, history: list[dict]) -> dict:
        messages = [
            {
                "role": "system",
                "content": (
                    "You control a web browser to accomplish a goal. Call decide_next_action "
                    "with exactly one next action.\n"
                    "Rules:\n"
                    "- click, type_text, and select_option REQUIRE a locator object, e.g. "
                    '{"strategy": "role", "value": {"role": "textbox", "name": "Member ID"}} '
                    'or {"strategy": "text", "value": {"text": "Search"}}.\n'
                    "- type_text also requires text and value_source "
                    '{"type": "goal_parameter", "param_name": "...", "param_type": "string"}.\n'
                    "- navigate requires target as a full URL on http://localhost:5000.\n"
                    "- When the goal is complete, set done=true and action=finish.\n"
                    "- Never omit locator for type_text or click."
                ),
            },
            {"role": "user", "content": f"Goal: {goal}\nCurrent page (accessibility tree): {observed_tree}\nHistory: {json.dumps(history)}"},
        ]
        response = requests.post(
            self.ENDPOINT,
            headers={"Authorization": f"Bearer {self.settings.openrouter_api_key}"},
            json={
                "model": self.settings.openrouter_model,
                "messages": messages,
                "tools": [_TOOL_SCHEMA],
                "tool_choice": {"type": "function", "function": {"name": "decide_next_action"}},
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        call = data["choices"][0]["message"]["tool_calls"][0]
        return json.loads(call["function"]["arguments"])
