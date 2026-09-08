import collections
import json
import threading
import time

import requests

from comp_use.config import Settings

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 1.0

# NVIDIA NIM's published ceiling for this account. Paced client-side because a 429
# costs more than the wait does: _post_with_retry burns its retry budget on it, and
# once that is exhausted FallbackLLMClient fails the whole call over to OpenRouter,
# whose free tier caps the ACCOUNT at 50 requests/day - so a burst here can exhaust
# tomorrow's fallback too. Observed live as `skipped_decision` events with
# "429 ... openrouter.ai", which count toward DiscoveryAgent's dead-end threshold
# and can end a run that was otherwise going fine.
NVIDIA_REQUESTS_PER_MINUTE = 40


class RateLimiter:
    """A thread-safe sliding-window limiter: never more than `max_requests`
    acquisitions in any `per_seconds` window.

    A sliding window rather than a token bucket because the provider's own limit is
    stated that way ("requests per minute"), and a bucket's burst allowance is
    exactly what trips it. Shared process-wide rather than per client instance
    (see _NVIDIA_RATE_LIMITER): the quota belongs to the API key, and every
    concurrent run builds its own client, so a per-instance limiter would let N
    runs send N x the limit.

    Never holds the lock while sleeping - waiters would otherwise serialize behind
    each other and each sleep the full window in turn."""

    def __init__(self, max_requests: int, per_seconds: float = 60.0):
        self._max_requests = max_requests
        self._per_seconds = per_seconds
        self._lock = threading.Lock()
        self._starts: collections.deque[float] = collections.deque()

    def acquire(self) -> float:
        """Blocks until a slot is free. Returns how long it waited, in seconds, so
        the caller can say so rather than looking hung."""
        waited = 0.0
        while True:
            with self._lock:
                # monotonic, not time(): a clock adjustment mid-run must not hand out
                # a burst of free slots or stall every caller for a wall-clock hour.
                now = time.monotonic()
                while self._starts and now - self._starts[0] >= self._per_seconds:
                    self._starts.popleft()
                if len(self._starts) < self._max_requests:
                    self._starts.append(now)
                    return waited
                sleep_for = self._per_seconds - (now - self._starts[0])
            # Re-checked on the next pass rather than assumed: another thread may
            # have taken the slot this one just waited for.
            time.sleep(sleep_for)
            waited += sleep_for


_NVIDIA_RATE_LIMITER = RateLimiter(NVIDIA_REQUESTS_PER_MINUTE)

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
                        # Naming the keys and forbidding {} in the SCHEMA, not just in
                        # the prompt text: as a bare {"type": "object"} this accepted an
                        # empty object, so `{"strategy":"text","value":{}}` was valid
                        # output. Two different models emitted exactly that, repeatedly,
                        # until the run dead-ended - they were not disobeying the rules,
                        # they were satisfying the schema they were given.
                        "value": {
                            "type": "object",
                            "description": "For strategy=role: {\"role\":..., \"name\":...}. "
                            "For strategy=text: {\"text\":...}. For strategy=css: {\"css\":...}. "
                            "Must never be empty.",
                            "properties": {
                                "role": {"type": "string", "description": "ARIA role, for strategy=role."},
                                "name": {"type": "string", "description": "Accessible name, for strategy=role."},
                                "text": {"type": "string", "description": "Visible text to match, for strategy=text."},
                                "css": {"type": "string", "description": "CSS selector, for strategy=css."},
                            },
                            "minProperties": 1,
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
                "derive_as": {
                    "type": ["string", "null"],
                    "description": "Only for action=extract, and only when the goal asks for a computed "
                    "value this page does NOT show directly (e.g. a 'total' summed from individual line "
                    "items with no total row of their own) - a short snake_case name for that computed "
                    "value (e.g. 'total_balance'). Leave null when the page already shows the value you "
                    "need directly extract that instead, with a normal extract_as. Never compute the "
                    "arithmetic yourself and put the answer in extract_as - pair derive_as with derive_op "
                    "so the real replay engine computes it deterministically from what you extracted.",
                },
                "derive_op": {
                    "type": ["string", "null"],
                    "enum": ["sum_currency", None],
                    "description": "Required when derive_as is set: 'sum_currency' sums every "
                    "$X,XXX.XX-shaped amount found in this same extract_as's text.",
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

extract-with-derive example (use this when the goal asks for a computed value the
page never shows as a single number itself - e.g. "read the balances and report the
total" against a table that only lists individual line items, no total row):
{"action":"extract","locator":{"strategy":"css","value":{"css":"table.shares"}},"target":null,"text":null,"value_source":null,"extract_as":"shares_table","derive_as":"total_balance","derive_op":"sum_currency","done":false}

finish example:
{"action":"finish","locator":null,"target":null,"text":null,"value_source":null,"done":true}

Rules:
- locator MUST include strategy and value. Never send locator as {}.
- value MUST NOT be empty either, and MUST carry the one key its strategy needs:
  strategy="role" -> {"role":...} (plus "name" unless the role is unique on the page),
  strategy="text" -> {"text":"the visible text to match"},
  strategy="css"  -> {"css":"a CSS selector"}.
  {"strategy":"text","value":{}} is rejected and wastes a turn - if you cannot name the
  text you want, use a css locator instead.
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
- "text" is a STRATEGY, not an ARIA role. To match visible text use
  {"strategy":"text","value":{"text":"Signed on as ..."}}. Never send
  {"strategy":"role","value":{"role":"text",...}} - no element has role "text", so it
  matches nothing and costs a 30-second timeout before failing.
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
  dropdown if the goal genuinely doesn't care what it's set to. This holds for EVERY
  such dropdown on the form, not just the first one you act on.
- A FIELD THAT ALREADY CONTAINS A VALUE IS NOT ALREADY DONE. Edit forms arrive
  pre-filled with the record's current data (an e-mail box already showing the member's
  current e-mail, a reason-code dropdown already on some default). If the goal says to
  set that field, you MUST type/select the new value into it anyway - what is sitting
  there now belongs to the record you happen to be looking at today, and on the next
  run it will be a different record's data. Skipping a pre-filled field because it
  "looks right" records a capability that submits the form unchanged: it reports
  success and changes nothing, which is worse than failing.
- Before you submit a form (Save/Continue/Post), check the goal for EVERY value it said
  would vary per call, and make sure you have actually typed or selected each one on
  this form. If the goal names an e-mail, a phone, an address, a reason code and an
  amount, all five must have been entered before you submit.
- extract requires extract_as (a short snake_case name for the value being read).
- NEVER anchor an extract locator on the VALUE you are trying to read. Anchor it on the
  stable LABEL or structure next to that value, which is the same on every run. The
  value itself is different every time, so a locator built from it matches nothing on
  the very next run and the capability fails at its final and most important step.
  WRONG: {"strategy":"text","value":{"text":"CN480332"}} - the next confirmation number
  is not CN480332.
  RIGHT: {"strategy":"css","value":{"css":"td:text-is('Confirmation:') + td"}} - reads
  whatever sits beside the "Confirmation:" label, whatever its value happens to be.
  The same applies to any other captured value: a new account/share id, a balance, a
  name, a date, a reference code.
- Do NOT extract with a whole-page locator like "body", "html" or a top-level wrapper.
  That returns the entire screen - navigation, headers, footers and all - as one blob,
  when what the caller asked for is one specific value or table. Target the smallest
  element that contains exactly what the goal asked to report.
- A generic class/tag selector (e.g. "table.box", "div.panel") is often reused by a
  legacy page for SEVERAL unrelated widgets on the same screen - a search-results
  page, for example, may re-render the search form itself (now empty, ready for a
  new query) above the actual results, sharing the same wrapper class as the
  results table below it. A bare "table.box" locator resolves to whichever
  instance appears FIRST, which may not be the one you want. Before extracting,
  check the observed tree for whether more than one element could plausibly match
  your selector; if so, anchor it to something that uniquely identifies the DATA
  you actually want - a `:has-text("...")` on something you expect to see there
  (the member number you searched for, a column header only the real results
  have), a more specific descendant path, or an nth-of-type - not just the
  generic wrapper class.
- After an extract, look at what you actually captured (it's echoed back to you
  on the next turn's history) - if it looks like an empty form (field labels with
  no values) rather than real data, your locator matched the wrong instance;
  refine it and extract again before finishing. Never finish believing an
  extract succeeded just because the action itself didn't error - a locator can
  resolve to the WRONG element without ever raising.
- If the goal explicitly asks for a computed/aggregate value (a "total", a "sum") and
  the page shows only individual line items with no total of its own, do NOT add up
  the numbers yourself and extract that as if it were on the page - extract the raw
  content (extract_as) AND set derive_as (a name for the computed value) + derive_op
  (currently only "sum_currency") on that same extract decision. The real replay
  engine performs the arithmetic deterministically from what you extracted; you are
  only ever declaring which computation to run, never producing the number.
- After a successful extract that captured everything the goal asked for, your NEXT
  action must be finish (action=finish, done=true) - do not repeat the same extract.
- Before you send action=finish, ask yourself: does the goal ask to find, look up,
  search for, check, read, view, or report ANY information (a balance, a list, a
  status, a record, search results, a confirmation)? If yes, and you have not yet
  extracted that information with extract_as, you are not done - extract it FIRST,
  then finish on the turn after. Reaching the right page and stopping there is NOT
  the same as completing the goal - the caller only ever receives what you
  extracted, nothing else. A goal that is purely an action with nothing to report
  back (e.g. "sign on", "submit this form") has nothing to extract - only skip
  extraction for goals like that, never for a goal that asks you to find or view
  something.
- When the goal is done (including, where applicable, extracting its result),
  action=finish and done=true.
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
    # Set by decide_next_action/chat/diagnose_drift after a real HTTP call - the
    # provider's own reported {"prompt_tokens", "completion_tokens", "total_tokens"}
    # for the call that just completed, or None (FakeLLMClient in tests, or a
    # provider response that omitted usage). A side-channel attribute rather than a
    # richer return type so every existing call site (DiscoveryAgent, ChatAgent,
    # tests) keeps working unchanged - callers that care read this right after the
    # call that produced it.
    last_usage: dict | None = None

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
        # Past the end of the script, keep returning its last action. The agent can
        # legitimately ask again after declining a decision once (see the finish/extract
        # push-back in DiscoveryAgent), and a real model asked to reconsider and still
        # sure of its answer just repeats it. Without this, every scripted test ending
        # in `finish` would have to spell that repeat out.
        action = self._actions[min(self._index, len(self._actions) - 1)]
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

    def _rate_limiter(self) -> "RateLimiter | None":
        """The limiter to pace this provider's requests, or None for no pacing.
        Only NVIDIA NIM declares one today - OpenRouter is the fallback and is
        already reached rarely, so throttling it would just add latency to the
        path taken when the primary is in trouble."""
        return None

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

    def _post_with_retry(self, payload: dict) -> requests.Response:
        """A 429 (rate limit) or 5xx is a transient provider condition, not a
        real decision failure - without a retry, FallbackLLMClient treated every
        one exactly like a genuine failure and failed over to the OTHER
        provider, and with only one provider configured it counted as a skipped
        decision toward DiscoveryAgent's dead-end threshold, so a few rate
        limits in a row looked like the agent was stuck when it wasn't. Only
        _RETRYABLE_STATUS_CODES get a retry; anything else (4xx auth/shape
        errors) fails immediately, same as before."""
        last_response = None
        limiter = self._rate_limiter()
        for attempt in range(_MAX_RETRIES + 1):
            if limiter is not None:
                # Inside the loop, not outside it: a retry is a real request to the
                # provider and has to consume a slot too. Pacing only the first
                # attempt would let a retry storm sail straight past the limit.
                waited = limiter.acquire()
                if waited > 0:
                    print(
                        f"[{self._provider_label()}] rate limit: waited {waited:.1f}s "
                        f"for a request slot",
                        flush=True,
                    )
            response = requests.post(
                self._endpoint(), headers=self._headers(), json=payload, timeout=60,
            )
            if response.status_code not in _RETRYABLE_STATUS_CODES:
                response.raise_for_status()
                return response
            last_response = response
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_BACKOFF_SECONDS * (2 ** attempt))
        last_response.raise_for_status()
        return last_response

    def chat(self, messages: list[dict], tools: list[dict], tool_choice: dict | str = "auto") -> dict:
        print(f"[chat] asking {self._provider_label()}:{self._text_model()} ...", flush=True)
        response = self._post_with_retry({
            "model": self._text_model(),
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        })
        data = response.json()
        self.last_usage = data.get("usage")
        return parse_chat_message(data["choices"][0]["message"])

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
        response = self._post_with_retry({
            "model": model,
            "messages": messages,
            "tools": [tool_schema],
            "tool_choice": tool_choice,
        })
        data = response.json()
        self.last_usage = data.get("usage")
        return parse_decision(data)


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

    def _rate_limiter(self) -> "RateLimiter | None":
        return _NVIDIA_RATE_LIMITER


class OpenAIClient(_OpenAICompatibleClient):
    """OpenAI proper (api.openai.com).

    The shortest client in this file, and deliberately so: OpenAI defined the
    chat-completions + tool-calling shape the other two providers imitate, so
    everything _OpenAICompatibleClient already does - forcing a tool call rather
    than accepting free text, the tolerant decision parser, retry-on-429/5xx, the
    image content block on the vision path - applies here unchanged. Only the
    endpoint, auth and model names differ.

    Rate limits are handled the same way they are for the other two providers:
    _post_with_retry() backs off and retries on a 429. OpenAI's own limits are
    per-account and per-tier rather than one published number, so there is no
    single ceiling worth pacing to client-side even if pacing existed here.
    """

    ENDPOINT = "https://api.openai.com/v1/chat/completions"

    def __init__(self, settings: Settings):
        self.settings = settings

    def _endpoint(self) -> str:
        return self.ENDPOINT

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.settings.openai_api_key}"}

    def _text_model(self) -> str:
        return self.settings.openai_model

    def _vision_model(self) -> str:
        return self.settings.openai_vision_model

    def _provider_label(self) -> str:
        return "openai"


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

    @property
    def last_usage(self) -> dict | None:
        # Reflects whichever client actually served the most recent call - a plain
        # instance attribute would need updating at every call site below, so this
        # just defers to the same attribute on the client that ran last.
        return self._last_used.last_usage if getattr(self, "_last_used", None) is not None else None

    def decide_next_action(self, goal: str, observed_tree: str, screenshot_b64: str | None, history: list[dict]) -> dict:
        try:
            result = self.primary.decide_next_action(
                goal=goal, observed_tree=observed_tree, screenshot_b64=screenshot_b64, history=history
            )
            self._last_used = self.primary
            return result
        except Exception as exc:
            print(f"[discover] primary provider failed ({type(exc).__name__}: {exc}); falling back...", flush=True)
            result = self.fallback.decide_next_action(
                goal=goal, observed_tree=observed_tree, screenshot_b64=screenshot_b64, history=history
            )
            self._last_used = self.fallback
            return result

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
