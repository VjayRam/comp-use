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


_DRIFT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "diagnose_drift",
        "description": "Determine whether an expected UI control still exists on the page, possibly "
        "under a different locator (renamed, moved, restyled), or is genuinely gone.",
        "parameters": {
            "type": "object",
            "properties": {
                "found": {
                    "type": "boolean",
                    "description": "true only if a control serving the SAME purpose is visible, even if "
                    "its role/name changed. false if nothing plausible is on the page.",
                },
                "locator": {
                    "type": ["object", "null"],
                    "description": "Required when found=true: {\"strategy\": \"role\"|\"text\"|\"css\", "
                    "\"value\": {...}} describing the replacement control.",
                },
                "reasoning": {"type": "string"},
            },
            "required": ["found"],
        },
    },
}

_DRIFT_SYSTEM_PROMPT = """You are diagnosing a UI automation failure. An automated replay expected to
find a specific control on this page and could not. You are shown a screenshot of the ACTUAL current
page and a description of the control that was EXPECTED.

Decide: is there a control on the current page that serves the same purpose as the expected one, just
under a different name/role/position (the page drifted - a redesign, a relabeled button, ...)? Or is
the expected control genuinely gone/never reachable from here (a real break, not drift)?

Reply with ONE flat JSON object (no markdown, no wrapping key, no nesting) matching this shape exactly:

found example (a matching control IS visible - locator is REQUIRED whenever found is true, describing
that control's role and accessible name so it can be clicked programmatically):
{"found":true,"locator":{"strategy":"role","value":{"role":"button","name":"Confirm Transfer"}},"reasoning":"same position, relabeled from Cofirm to Confirm"}

not-found example (nothing plausible is visible):
{"found":false,"locator":null,"reasoning":"no control resembling the expected one is visible"}

Only set found=true if you can point at a specific, visible control - never guess, and never omit
locator when found is true. When in doubt, found=false; a missed drift-repair opportunity is far
cheaper than a wrong one silently applied.
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


def _normalize_drift_diagnosis(raw: dict) -> dict:
    """Some smaller/free models echo the tool's own name back as a wrapping
    key instead of returning a flat object (seen live: `{"diagnose_drift":
    {"found": true, ...}}`), or use "reason" instead of the schema's
    "reasoning". Defensive, not a workaround for a bug on our side - this is
    the model deviating from the requested shape, and the fix is to tolerate
    it rather than silently discard a correct diagnosis."""
    if set(raw.keys()) == {"diagnose_drift"} and isinstance(raw["diagnose_drift"], dict):
        raw = raw["diagnose_drift"]
    if "reasoning" not in raw and "reason" in raw:
        raw = {**raw, "reasoning": raw["reason"]}
    return raw


class LLMClient:
    def decide_next_action(self, goal: str, observed_tree: str, screenshot_b64: str | None, history: list[dict]) -> dict:
        raise NotImplementedError

    def diagnose_drift(self, expected_locator: dict, screenshot_b64: str) -> dict:
        raise NotImplementedError


class FakeLLMClient(LLMClient):
    def __init__(self, scripted_actions: list[dict], scripted_drift_diagnoses: list[dict] | None = None):
        self._actions = list(scripted_actions)
        self._index = 0
        self._drift_diagnoses = list(scripted_drift_diagnoses or [])
        self._drift_index = 0

    def decide_next_action(self, goal: str, observed_tree: str, screenshot_b64: str | None, history: list[dict]) -> dict:
        action = self._actions[self._index]
        self._index += 1
        return action

    def diagnose_drift(self, expected_locator: dict, screenshot_b64: str) -> dict:
        diagnosis = self._drift_diagnoses[self._drift_index]
        self._drift_index += 1
        return diagnosis


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
        return self._post_chat(model, messages, _TOOL_SCHEMA)

    def diagnose_drift(self, expected_locator: dict, screenshot_b64: str) -> dict:
        # Always vision - there is no text-only path here, a screenshot is the
        # entire point (the expected locator, by definition, already failed to
        # resolve, so there's nothing more the accessibility tree can tell us).
        text_block = (
            f"Expected control (locator that failed to resolve): {json.dumps(expected_locator)}\n"
            "Look at the screenshot below and decide if this control still exists, "
            "possibly under a different name/role/position."
        )
        user_content = [
            {"type": "text", "text": text_block},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
        ]
        messages = [
            {"role": "system", "content": _DRIFT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        raw = self._post_chat(self.settings.openrouter_vision_model, messages, _DRIFT_TOOL_SCHEMA)
        return _normalize_drift_diagnosis(raw)

    def _post_chat(self, model: str, messages: list[dict], tool_schema: dict) -> dict:
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
                "tools": [tool_schema],
            },
            timeout=60,
        )
        response.raise_for_status()
        return parse_decision(response.json())
