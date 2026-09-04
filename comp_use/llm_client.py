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
                "locator": {
                    "type": ["object", "null"],
                    "description": "Required for click/type_text/select_option/extract.",
                    "properties": {
                        "strategy": {"type": "string", "enum": ["role", "text", "css"]},
                        "value": {
                            "type": "object",
                            "description": "For strategy=role: {\"role\":..., \"name\":...}. "
                            "For strategy=text: {\"text\":...}. For strategy=css: {\"css\":...}.",
                        },
                        "fallback": {"type": ["object", "null"], "description": "A second Locator to try if this one fails."},
                    },
                    "required": ["strategy", "value"],
                },
                "target": {"type": ["string", "null"]},
                "text": {"type": ["string", "null"]},
                "value_source": {
                    "type": ["object", "null"],
                    "description": "Leave null for a value that never varies between calls. Set this "
                    "whenever the value SHOULD vary (credentials, account/share numbers, amounts, "
                    "emails, phone numbers, member IDs, ...).",
                    "properties": {
                        "type": {"type": "string", "enum": ["goal_parameter"]},
                        "param_name": {"type": "string", "description": "snake_case name for this parameter."},
                        "param_type": {"type": ["string", "null"], "enum": ["string", "number", "boolean", None]},
                        "reason": {
                            "type": ["string", "null"],
                            "description": "Optional one-line note on why this value varies per call.",
                        },
                    },
                    "required": ["type", "param_name"],
                },
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
- type_text/select_option require text (the value being typed/selected).
- value_source is binary: leave it null for a value that is genuinely always the
  same no matter who calls this capability or when (e.g. a dropdown with only one
  real option). Set it to {"type":"goal_parameter","param_name":...,"param_type":...}
  for EVERY value that should vary between calls - this includes login credentials
  (operator ID, password, branch), account/share numbers, dollar amounts, email
  addresses, phone numbers, member/customer IDs, reason codes, memos/notes -
  basically any value a caller would reasonably want to supply differently next time.
  When in doubt, parameterize it: a value wrongly marked as varying just means an
  extra required input the caller can pass the same literal for; a value wrongly left
  fixed makes the whole capability unusable for anyone/anything else, which is worse.
- value_source.type must be exactly "goal_parameter" - there is no other value.
- strategy must be exactly "role", "text", or "css" - no other value (never "xpath").
- A role locator with an empty or missing name is only safe when exactly one element on
  the page has that role. Legacy table-based forms often have multiple unlabeled inputs
  with the same role (e.g. two textboxes for username and password, with the label as
  plain text in an adjacent table cell, not a real <label>). If a role locator would be
  ambiguous, use a css locator keyed on the input's own name/type/id attribute instead,
  e.g. {"strategy":"css","value":{"css":"input[name='operator']"}} or
  {"strategy":"css","value":{"css":"input[type='password']"}}. If the page includes a
  "Form field attributes" section, use the name/id shown there to build this locator.
- NEVER build a locator keyed on a field's CURRENT value (e.g.
  input[value='old@example.com']) - that value is about to change (you're editing it)
  or won't be the same on a future run with different inputs, so the locator will fail
  the very next time this flow runs. Use name/id/type instead. This applies to every
  action, not just type_text.
- If a page has a dropdown (combobox) whose choice matters to the goal (e.g. a share
  to act on, a reason code, a category), you MUST select_option on it explicitly - do
  not leave it at whatever option happens to be pre-selected and move on. Only skip a
  dropdown if the goal genuinely doesn't care what it's set to.
- extract requires extract_as (a short snake_case name for the value being read).
- After a successful extract that captured everything the goal asked for, your NEXT
  action must be finish (action=finish, done=true) - do not repeat the same extract.
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
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Real models sometimes append trailing commentary after a perfectly
        # valid JSON object ("...}\nLet me know if you need anything else!").
        # json.loads() requires the whole string to be exactly one JSON value;
        # raw_decode() parses just the leading value and reports where it
        # ended, which is exactly what's needed here - use the decision that
        # was actually there instead of discarding it over trailing text.
        return json.JSONDecoder().raw_decode(content)[0]


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


def parse_chat_message(message: dict) -> dict:
    """Counterpart to parse_decision() for the chat agent's turns, which use
    tool_choice="auto" - the model may legitimately reply in plain prose
    instead of calling a tool. Unlike parse_decision(), this never tries to
    JSON-parse plain content; a chat reply that happens to start with "{" is
    still just prose, not a decision payload."""
    if message.get("tool_calls"):
        call = message["tool_calls"][0]["function"]
        raw = call["arguments"]
        arguments = raw if isinstance(raw, dict) else json.loads(raw)
        return {"type": "tool_call", "name": call["name"], "arguments": arguments}
    return {"type": "text", "text": (message.get("content") or "").strip()}


class LLMClient:
    def decide_next_action(self, goal: str, observed_tree: str, screenshot_b64: str | None, history: list[dict]) -> dict:
        raise NotImplementedError

    def diagnose_drift(self, expected_locator: dict, screenshot_b64: str) -> dict:
        raise NotImplementedError

    def chat(self, messages: list[dict], tools: list[dict], tool_choice: dict | str = "auto") -> dict:
        raise NotImplementedError


class FakeLLMClient(LLMClient):
    def __init__(
        self,
        scripted_actions: list[dict] | None = None,
        scripted_drift_diagnoses: list[dict] | None = None,
        scripted_chat_turns: list[dict] | None = None,
    ):
        self._actions = list(scripted_actions or [])
        self._index = 0
        self._drift_diagnoses = list(scripted_drift_diagnoses or [])
        self._drift_index = 0
        self._chat_turns = list(scripted_chat_turns or [])
        self._chat_index = 0

    def decide_next_action(self, goal: str, observed_tree: str, screenshot_b64: str | None, history: list[dict]) -> dict:
        action = self._actions[self._index]
        self._index += 1
        return action

    def diagnose_drift(self, expected_locator: dict, screenshot_b64: str) -> dict:
        diagnosis = self._drift_diagnoses[self._drift_index]
        self._drift_index += 1
        return diagnosis

    def chat(self, messages: list[dict], tools: list[dict], tool_choice: dict | str = "auto") -> dict:
        turn = self._chat_turns[self._chat_index]
        self._chat_index += 1
        return turn


class _OpenAICompatibleClient(LLMClient):
    """Shared logic for any provider exposing an OpenAI-compatible chat
    completions endpoint (tool calling + optional image content blocks) -
    OpenRouter and NVIDIA NIM both implement this exact shape. Provider-
    specific bits (endpoint, auth headers, which model names to use, a label
    for logging) are supplied by subclasses; the request/response handling,
    prompts, and tool schemas are identical either way."""

    def _endpoint(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict:
        raise NotImplementedError

    def _text_model(self) -> str:
        raise NotImplementedError

    def _vision_model(self) -> str:
        raise NotImplementedError

    def _provider_label(self) -> str:
        raise NotImplementedError

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
            model = self._vision_model()
        else:
            user_content = text_block
            model = self._text_model()

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
        raw = self._post_chat(self._vision_model(), messages, _DRIFT_TOOL_SCHEMA)
        return _normalize_drift_diagnosis(raw)

    def chat(self, messages: list[dict], tools: list[dict], tool_choice: dict | str = "auto") -> dict:
        print(f"[chat] asking {self._provider_label()}:{self._text_model()} ...", flush=True)
        response = requests.post(
            self._endpoint(),
            headers=self._headers(),
            json={
                "model": self._text_model(),
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
            },
            timeout=60,
        )
        response.raise_for_status()
        return parse_chat_message(response.json()["choices"][0]["message"])

    def _post_chat(self, model: str, messages: list[dict], tool_schema: dict) -> dict:
        print(f"[discover] asking {self._provider_label()}:{model} ...", flush=True)
        # Force the model to actually use the tool call rather than optionally
        # replying in free text - previously "tools" was offered without a
        # "tool_choice", so a weaker/free-tier model could (and sometimes did)
        # respond in prose instead, falling through to parse_decision()'s much
        # weaker free-text JSON scraping. Derived from tool_schema itself (not a
        # hardcoded name) since this same method also serves the drift-diagnosis
        # tool call, which has a different function name.
        tool_choice = {"type": "function", "function": {"name": tool_schema["function"]["name"]}}
        response = requests.post(
            self._endpoint(),
            headers=self._headers(),
            json={
                "model": model,
                "messages": messages,
                "tools": [tool_schema],
                "tool_choice": tool_choice,
            },
            timeout=60,
        )
        response.raise_for_status()
        return parse_decision(response.json())


class OpenRouterClient(_OpenAICompatibleClient):
    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, settings: Settings):
        self.settings = settings

    def _endpoint(self) -> str:
        return self.ENDPOINT

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "HTTP-Referer": "http://localhost:5000",
            "X-Title": "comp-use",
        }

    def _text_model(self) -> str:
        return self.settings.openrouter_model

    def _vision_model(self) -> str:
        return self.settings.openrouter_vision_model

    def _provider_label(self) -> str:
        return "openrouter"


class NvidiaNimClient(_OpenAICompatibleClient):
    """NVIDIA NIM's hosted inference API (integrate.api.nvidia.com) - also
    OpenAI-compatible chat completions with tool calling. Exists as a second
    provider so discovery can fail over here when OpenRouter's free-tier rate
    limits are hit, rather than the whole run stalling on one provider (see
    FallbackLLMClient)."""

    ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

    def __init__(self, settings: Settings):
        self.settings = settings

    def _endpoint(self) -> str:
        return self.ENDPOINT

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.settings.nvidia_api_key}"}

    def _text_model(self) -> str:
        return self.settings.nvidia_model

    def _vision_model(self) -> str:
        return self.settings.nvidia_vision_model

    def _provider_label(self) -> str:
        return "nvidia-nim"


class FallbackLLMClient(LLMClient):
    """Tries `primary` first; if it raises for ANY reason (a rate limit is
    the motivating case, but this also covers timeouts, transient 5xxs, and
    malformed responses), tries `fallback` instead. If `fallback` also
    raises, its exception propagates - already handled gracefully further up
    (DiscoveryAgent.run() wraps every LLM call and treats a failure as a
    retryable skip, not a crash). This class only decides which provider
    answers a given call; it never decides what a caller should do if both
    providers are down."""

    def __init__(self, primary: LLMClient, fallback: LLMClient):
        self.primary = primary
        self.fallback = fallback

    def decide_next_action(self, goal: str, observed_tree: str, screenshot_b64: str | None, history: list[dict]) -> dict:
        try:
            return self.primary.decide_next_action(
                goal=goal, observed_tree=observed_tree, screenshot_b64=screenshot_b64, history=history
            )
        except Exception as exc:
            print(f"[discover] primary provider failed ({type(exc).__name__}: {exc}); falling back...", flush=True)
            return self.fallback.decide_next_action(
                goal=goal, observed_tree=observed_tree, screenshot_b64=screenshot_b64, history=history
            )

    def diagnose_drift(self, expected_locator: dict, screenshot_b64: str) -> dict:
        try:
            return self.primary.diagnose_drift(expected_locator=expected_locator, screenshot_b64=screenshot_b64)
        except Exception as exc:
            print(f"[discover] primary provider failed ({type(exc).__name__}: {exc}); falling back...", flush=True)
            return self.fallback.diagnose_drift(expected_locator=expected_locator, screenshot_b64=screenshot_b64)

    def chat(self, messages: list[dict], tools: list[dict], tool_choice: dict | str = "auto") -> dict:
        try:
            return self.primary.chat(messages=messages, tools=tools, tool_choice=tool_choice)
        except Exception as exc:
            print(f"[chat] primary provider failed ({type(exc).__name__}: {exc}); falling back...", flush=True)
            return self.fallback.chat(messages=messages, tools=tools, tool_choice=tool_choice)
