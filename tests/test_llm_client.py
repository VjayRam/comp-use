import threading
from unittest.mock import patch, MagicMock

from comp_use import llm_client
from comp_use.config import load_settings
from comp_use.llm_client import (
    _SYSTEM_PROMPT, _TOOL_SCHEMA, NVIDIA_REQUESTS_PER_MINUTE, FakeLLMClient,
    FallbackLLMClient, NvidiaNimClient, OpenRouterClient, RateLimiter,
    parse_decision, LLMClient,
)
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


@patch("comp_use.llm_client.requests.post")
def test_nvidia_nim_client_hits_its_own_endpoint_with_bearer_auth_only(mock_post):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": '{"action": "finish", "done": true}'}}]
    }
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response

    settings = load_settings()
    settings.nvidia_api_key = "nvidia-test-key"
    settings.nvidia_model = "meta/llama-3.1-8b-instruct"
    client = NvidiaNimClient(settings)

    result = client.decide_next_action(goal="find member", observed_tree="tree", screenshot_b64=None, history=[])

    assert result["action"] == "finish"
    called_url = mock_post.call_args.args[0] if mock_post.call_args.args else mock_post.call_args.kwargs.get("url")
    assert called_url == NvidiaNimClient.ENDPOINT
    assert called_url != OpenRouterClient.ENDPOINT
    sent_headers = mock_post.call_args.kwargs["headers"]
    assert sent_headers["Authorization"] == "Bearer nvidia-test-key"
    # unlike OpenRouter, NIM needs no HTTP-Referer/X-Title
    assert "HTTP-Referer" not in sent_headers
    sent_payload = mock_post.call_args.kwargs["json"]
    assert sent_payload["model"] == "meta/llama-3.1-8b-instruct"


@patch("comp_use.llm_client.requests.post")
def test_nvidia_nim_client_routes_vision_calls_to_its_own_vision_model(mock_post):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": '{"action": "click", "locator": {"strategy": "role", "value": {"role": "button", "name": "Search"}}, "done": false}'}}]
    }
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response

    settings = load_settings()
    settings.nvidia_api_key = "nvidia-test-key"
    settings.nvidia_vision_model = "meta/llama-3.2-11b-vision-instruct"
    client = NvidiaNimClient(settings)

    client.decide_next_action(goal="find member", observed_tree="tree", screenshot_b64="ZmFrZXBuZw==", history=[])

    sent_payload = mock_post.call_args.kwargs["json"]
    assert sent_payload["model"] == "meta/llama-3.2-11b-vision-instruct"


def test_fallback_client_uses_primary_result_when_primary_succeeds():
    primary = FakeLLMClient(scripted_actions=[{"action": "finish", "done": True}])
    fallback = FakeLLMClient(scripted_actions=[])  # would raise IndexError if ever called
    client = FallbackLLMClient(primary, fallback)

    result = client.decide_next_action(goal="g", observed_tree="t", screenshot_b64=None, history=[])

    assert result["done"] is True


class _RaisingLLMClient:
    def __init__(self, exc):
        self._exc = exc

    def decide_next_action(self, goal, observed_tree, screenshot_b64, history):
        raise self._exc

    def diagnose_drift(self, expected_locator, screenshot_b64):
        raise self._exc


def test_fallback_client_switches_to_fallback_when_primary_raises():
    # The motivating case: OpenRouter's free-tier rate limit hits (a 429,
    # modeled here as a generic exception since the fallback logic doesn't
    # care about the exception type - any primary failure triggers it).
    primary = _RaisingLLMClient(RuntimeError("429 Too Many Requests"))
    fallback = FakeLLMClient(scripted_actions=[{"action": "finish", "done": True}])
    client = FallbackLLMClient(primary, fallback)

    result = client.decide_next_action(goal="g", observed_tree="t", screenshot_b64=None, history=[])

    assert result["done"] is True


def test_fallback_client_propagates_when_both_providers_fail():
    primary = _RaisingLLMClient(RuntimeError("primary down"))
    fallback = _RaisingLLMClient(ValueError("fallback also down"))
    client = FallbackLLMClient(primary, fallback)

    try:
        client.decide_next_action(goal="g", observed_tree="t", screenshot_b64=None, history=[])
        assert False, "expected the fallback's exception to propagate"
    except ValueError as exc:
        assert "fallback also down" in str(exc)


def test_fallback_client_applies_to_diagnose_drift_too():
    primary = _RaisingLLMClient(RuntimeError("429 Too Many Requests"))
    fallback = FakeLLMClient(scripted_actions=[], scripted_drift_diagnoses=[{"found": False, "reasoning": "n/a"}])
    client = FallbackLLMClient(primary, fallback)

    result = client.diagnose_drift(expected_locator={"strategy": "role", "value": {}}, screenshot_b64="Zg==")

    assert result["found"] is False


def test_parse_chat_message_returns_tool_call_shape():
    from comp_use.llm_client import parse_chat_message
    message = {
        "tool_calls": [{"function": {"name": "propose_invoke", "arguments": '{"capability_name": "x", "params": {}}'}}]
    }
    result = parse_chat_message(message)
    assert result == {"type": "tool_call", "name": "propose_invoke", "arguments": {"capability_name": "x", "params": {}}}


def test_parse_chat_message_returns_text_shape_when_no_tool_call():
    from comp_use.llm_client import parse_chat_message
    message = {"content": "Sure, which member number?"}
    result = parse_chat_message(message)
    assert result == {"type": "text", "text": "Sure, which member number?"}


def test_parse_chat_message_never_json_parses_plain_text():
    # Unlike parse_decision(), a chat reply that happens to start with "{" but
    # isn't actually JSON (real prose) must come back as text, not raise/mangle it.
    from comp_use.llm_client import parse_chat_message
    message = {"content": "{today's} plan is to check the balance first"}
    result = parse_chat_message(message)
    assert result == {"type": "text", "text": "{today's} plan is to check the balance first"}


def test_fake_llm_client_chat_returns_scripted_turns_in_order():
    client = FakeLLMClient(
        scripted_chat_turns=[
            {"type": "text", "text": "Which site?"},
            {"type": "tool_call", "name": "propose_invoke", "arguments": {"capability_name": "x", "params": {}}},
        ]
    )
    first = client.chat(messages=[], tools=[])
    second = client.chat(messages=[], tools=[])
    assert first["type"] == "text"
    assert second["name"] == "propose_invoke"


def test_fake_llm_client_scripted_actions_defaults_to_empty_list():
    # scripted_actions must stay optional so chat-only tests can construct a
    # FakeLLMClient without a discovery-loop script.
    client = FakeLLMClient(scripted_chat_turns=[{"type": "text", "text": "hi"}])
    assert client.chat(messages=[], tools=[])["text"] == "hi"


@patch("comp_use.llm_client.requests.post")
def test_openrouter_client_chat_sends_auto_tool_choice_by_default(mock_post):
    mock_response = MagicMock()
    mock_response.json.return_value = {"choices": [{"message": {"content": "okay"}}]}
    mock_post.return_value = mock_response

    client = OpenRouterClient(load_settings())
    result = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "t"}}])

    assert result == {"type": "text", "text": "okay"}
    sent_json = mock_post.call_args.kwargs["json"]
    assert sent_json["tool_choice"] == "auto"


@patch("comp_use.llm_client.requests.post")
def test_openrouter_client_chat_honors_a_forced_tool_choice(mock_post):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"tool_calls": [{"function": {"name": "resolve_pending", "arguments": '{"decision": "confirm"}'}}]}}]
    }
    mock_post.return_value = mock_response

    client = OpenRouterClient(load_settings())
    forced = {"type": "function", "function": {"name": "resolve_pending"}}
    result = client.chat(messages=[], tools=[{"type": "function", "function": {"name": "resolve_pending"}}], tool_choice=forced)

    assert result == {"type": "tool_call", "name": "resolve_pending", "arguments": {"decision": "confirm"}}
    assert mock_post.call_args.kwargs["json"]["tool_choice"] == forced


def test_fallback_llm_client_chat_falls_back_on_primary_exception():
    class RaisingClient(LLMClient):
        def chat(self, messages, tools, tool_choice="auto"):
            raise RuntimeError("rate limited")

    fallback = FakeLLMClient(scripted_chat_turns=[{"type": "text", "text": "from fallback"}])
    client = FallbackLLMClient(RaisingClient(), fallback)

    result = client.chat(messages=[], tools=[])

    assert result == {"type": "text", "text": "from fallback"}


def test_rate_limiter_allows_up_to_the_limit_without_waiting():
    limiter = RateLimiter(max_requests=3, per_seconds=60.0)

    assert [limiter.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]


def test_rate_limiter_makes_the_request_over_the_limit_wait_for_the_window(monkeypatch):
    """The wait is asserted through a fake clock rather than by really sleeping -
    a test that slept 60s to prove a 60s window would never be run."""
    now = {"t": 1000.0}
    slept: list[float] = []
    monkeypatch.setattr(llm_client.time, "monotonic", lambda: now["t"])

    def fake_sleep(seconds):
        slept.append(seconds)
        now["t"] += seconds

    monkeypatch.setattr(llm_client.time, "sleep", fake_sleep)

    limiter = RateLimiter(max_requests=2, per_seconds=60.0)
    limiter.acquire()          # t=1000, slot 1
    now["t"] = 1010.0
    limiter.acquire()          # t=1010, slot 2 - window is now full

    now["t"] = 1020.0
    waited = limiter.acquire()

    # The oldest start was at t=1000, so its slot frees at t=1060: 40s from t=1020.
    assert slept == [40.0]
    assert waited == 40.0


def test_rate_limiter_frees_slots_once_they_age_out_of_the_window(monkeypatch):
    now = {"t": 500.0}
    monkeypatch.setattr(llm_client.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(llm_client.time, "sleep", lambda seconds: None)

    limiter = RateLimiter(max_requests=2, per_seconds=60.0)
    limiter.acquire()
    limiter.acquire()

    # Both starts are now older than the window, so neither should count.
    now["t"] = 561.0
    assert limiter.acquire() == 0.0
    assert limiter.acquire() == 0.0


def test_rate_limiter_is_thread_safe_and_never_exceeds_the_limit():
    """The limiter is shared process-wide across concurrent runs, so the invariant
    that matters is the one under contention, not the one on a single thread."""
    limiter = RateLimiter(max_requests=20, per_seconds=60.0)
    granted: list[float] = []
    lock = threading.Lock()

    def worker():
        # Never blocks: 4 threads x 5 acquisitions == the limit exactly. A limiter
        # that lost an increment to a race would let a 21st through and hang here.
        for _ in range(5):
            waited = limiter.acquire()
            with lock:
                granted.append(waited)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads)
    assert len(granted) == 20
    assert granted == [0.0] * 20


def test_only_the_nvidia_client_paces_its_requests():
    settings = load_settings()

    assert NvidiaNimClient(settings)._rate_limiter() is not None
    # The fallback provider is reached rarely and only when the primary is already
    # in trouble - throttling it would add latency exactly when it hurts.
    assert OpenRouterClient(settings)._rate_limiter() is None


def test_the_nvidia_limiter_is_shared_across_client_instances():
    """A per-instance limiter would let N concurrent runs each send N x the limit,
    since every run builds its own client. The quota belongs to the API key."""
    settings = load_settings()

    assert NvidiaNimClient(settings)._rate_limiter() is NvidiaNimClient(settings)._rate_limiter()


def test_the_nvidia_limiter_is_configured_for_forty_requests_a_minute():
    assert NVIDIA_REQUESTS_PER_MINUTE == 40
    assert llm_client._NVIDIA_RATE_LIMITER._max_requests == 40
    assert llm_client._NVIDIA_RATE_LIMITER._per_seconds == 60.0


def test_every_retry_attempt_consumes_a_rate_limit_slot(monkeypatch):
    """Pacing only the first attempt would let a retry storm sail past the limit -
    a 429 is answered by up to _MAX_RETRIES more real requests to the provider."""
    calls = {"acquired": 0}

    class CountingLimiter(RateLimiter):
        def acquire(self):
            calls["acquired"] += 1
            return 0.0

    client = NvidiaNimClient(load_settings())
    monkeypatch.setattr(client, "_rate_limiter", lambda: CountingLimiter(40))
    monkeypatch.setattr(llm_client.time, "sleep", lambda seconds: None)

    responses = [MagicMock(status_code=429), MagicMock(status_code=429), MagicMock(status_code=200)]
    monkeypatch.setattr(llm_client.requests, "post", lambda *a, **k: responses.pop(0))

    client._post_with_retry({"model": "m", "messages": []})

    assert calls["acquired"] == 3  # the initial attempt plus both retries
