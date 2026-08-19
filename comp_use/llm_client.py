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
                "extract_as": {
                    "type": ["string", "null"],
                    "description": "Only for action=extract: a short snake_case name for the value being "
                    "read off the page (e.g. 'confirmation_number', 'txn_id'), so the caller can retrieve it later.",
                },
                "done": {"type": "boolean"},
            },
            "required": ["action", "done"],
        },
    },
}

_SYSTEM_PROMPT = """You control a web browser. Reply with ONE JSON object (no markdown).

type_text example:
{"action":"type_text","locator":{"strategy":"role","value":{"role":"textbox","name":"Member ID"}},"target":null,"text":"12345","value_source":{"type":"goal_parameter","param_name":"member_id","param_type":"string"},"done":false}

click example:
{"action":"click","locator":{"strategy":"role","value":{"role":"button","name":"Search"}},"target":null,"text":null,"value_source":null,"done":false}

extract example (use this when a confirmation/result page shows a value the caller
should get back, like a confirmation number or transaction ID - the locator must
point at the VALUE itself, not its label; a CSS locator like this one, matching the
cell right after a label cell, is often the most reliable way to do that):
{"action":"extract","locator":{"strategy":"css","value":{"css":"td:text-is('Confirmation Number') + td"}},"target":null,"text":null,"value_source":null,"extract_as":"confirmation_number","done":false}

finish example:
{"action":"finish","locator":null,"target":null,"text":null,"value_source":null,"done":true}

Rules:
- locator MUST include strategy and value. Never send locator as {}.
- click/type_text/select_option/extract require a complete locator.
- type_text requires text and value_source.
- extract requires extract_as (a short snake_case name for the value being read).
- When the goal is done, action=finish and done=true.
"""


def parse_decision(data: dict) -> dict:
    message = data["choices"][0]["message"]
    if message.get("tool_calls"):
        raw = message["tool_calls"][0]["function"]["arguments"]
        if isinstance(raw, dict):
            return raw
        return json.loads(raw)
    content = (message.get("content") or "").strip()
    if content.startswith("```"):
        lines = content.splitlines()
        inner = lines[1:]
        if inner and inner[-1].strip().startswith("```"):
            inner = inner[:-1]
        content = "\n".join(inner)
    return json.loads(content)


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
        text_block = (
            f"Goal: {goal}\n"
            f"Current page (accessibility tree): {observed_tree}\n"
            f"History: {json.dumps(history)}\n"
            "Return the next JSON action now."
        )
        if screenshot_b64 is not None:
            # Vision fallback: the accessibility tree alone hasn't produced a usable
            # locator (see DiscoveryAgent.needs_vision_fallback). Give the model a
            # screenshot alongside the tree and route to a vision-capable model -
            # the default text model isn't guaranteed to accept image content.
            text_block = (
                "The accessibility tree alone was not enough to locate the right "
                "element last turn. Use the screenshot below to identify it visually, "
                "then still describe it with a role/text/css locator.\n" + text_block
            )
            user_content = [
                {"type": "text", "text": text_block},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
            ]
            model = self.settings.openrouter_vision_model
        else:
            user_content = text_block
            model = self.settings.openrouter_model

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        print(f"[discover] asking {model} ...", flush=True)
        response = requests.post(
            self.ENDPOINT,
            headers={
                "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                "HTTP-Referer": "http://localhost:5000",
                "X-Title": "comp-use",
            },
            json={
                "model": model,
                "messages": messages,
                "tools": [_TOOL_SCHEMA],
            },
            timeout=60,
        )
        response.raise_for_status()
        return parse_decision(response.json())
