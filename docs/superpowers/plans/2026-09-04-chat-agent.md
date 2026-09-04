# Chat Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the conversational chat agent described in the spec — natural-language intent to capability invocation, with an explicit confirm/cancel/amend gate before running anything and a shared live-run view (embedded in chat, reused from the dashboard) that surfaces escalation for both replay and discovery runs.

**Architecture:** A new `comp_use/chat/` package (`catalog.py`, `session.py`, `agent.py`) that is a pure client of the existing capability/replay/discovery APIs — it never touches `DiscoveryAgent`/`ReplayEngine` directly. Two new FastAPI endpoints in `server/app.py` own the only side-effecting step (starting a run via the existing `RunManager`), exactly mirroring how `invoke_capability`/`discover_capability` already do it. The frontend gets a real `Chat.tsx` and a `RunPanel` component extracted from the dashboard's existing run-detail view so both surfaces share one implementation.

**Tech Stack:** Python/FastAPI/pytest (backend, matching the existing codebase), React/TypeScript/Vite (frontend, matching the existing dashboard). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-04-chat-agent-design.md`

## Global Constraints

- Session state is in-memory only — no new Postgres table, no persistence across a server restart or page refresh (spec: "Purpose" / "Architecture").
- Chat never sets `confirm_risky=True` on any invoke/discover call — a risky step inside an already-confirmed run must still escalate normally (spec: "Guardrail preservation").
- `session.target_site` is asked once per session (not per request) and scopes the capability catalog before the model ever sees candidates; `propose_invoke`'s tool schema carries no site argument (spec: "Target site").
- Confirmation messages are composed deterministically in Python, never by the LLM, and mask password-like param values (spec: "Turn-by-turn mechanics").
- `resolve_pending` is a forced single-function tool call, reusing the existing `tool_choice`-forced pattern — never inferred from free-form "auto" choice (spec: "Turn-by-turn mechanics").
- No frontend test suite exists in this repo; this plan does not introduce one. Frontend tasks are verified manually in-browser.

## Cut from this plan (spec-covered, deliberately deferred)

The spec's "Discovery-specific details" section describes chaining a successful
discovery straight into an approve-and-run offer, in the same confirm loop as
`propose_invoke`. This plan does not implement that chaining: `ChatAgent.turn()`
is a synchronous, stateless-per-request function, and a discovery run finishes
on a background thread (`RunManager`) well after the turn that started it
returned — there is no request in flight for the agent to attach a follow-up
proposal to. Wiring that up correctly needs either a client-side "poll the
run, then send a synthetic follow-up chat message when it completes" step or a
server-side callback into the session, either of which is a second mechanism
on top of everything else in this plan.

Cut instead of built now: once a chat-started discovery run finishes, the
user sees it (draft status, version) in the embedded `RunPanel` exactly as
they would on the dashboard, and can approve + invoke it from there — the
dashboard's `VersionManager` already has full approve/set-default/invoke
support (built earlier this session), so nothing is actually unreachable,
just not chained automatically inside the chat turn. Next step with more
time: have the frontend, on seeing a discovery run's `RunPanel` reach
`status: "done"` with `succeeded: true`, send an automatic follow-up chat
message on the user's behalf (e.g. "the recording finished — approve and run
it now?") so the confirm loop picks it up like any other turn.

---

## Task 1: `LLMClient.chat()` — generic multi-tool / forced-tool chat method

**Files:**
- Modify: `comp_use/llm_client.py`
- Test: `tests/test_llm_client.py`

**Interfaces:**
- Produces: `parse_chat_message(message: dict) -> dict` — returns `{"type": "tool_call", "name": str, "arguments": dict}` or `{"type": "text", "text": str}`.
- Produces: `LLMClient.chat(self, messages: list[dict], tools: list[dict], tool_choice: dict | str = "auto") -> dict` (same return shape as `parse_chat_message`), implemented on `LLMClient` (raises `NotImplementedError`), `FakeLLMClient`, `_OpenAICompatibleClient` (inherited by `OpenRouterClient`/`NvidiaNimClient`), and `FallbackLLMClient`.
- Produces: `FakeLLMClient.__init__(self, scripted_actions: list[dict] | None = None, scripted_drift_diagnoses: list[dict] | None = None, scripted_chat_turns: list[dict] | None = None)` — `scripted_actions` becomes optional (existing call sites pass it explicitly and are unaffected).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_llm_client.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_llm_client.py -q -k "chat or parse_chat"`
Expected: FAIL (`parse_chat_message`/`.chat()` don't exist yet)

- [ ] **Step 3: Implement**

In `comp_use/llm_client.py`, add after `_normalize_drift_diagnosis` (before `class LLMClient:`):

```python
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
```

Replace `class LLMClient:` block with:

```python
class LLMClient:
    def decide_next_action(self, goal: str, observed_tree: str, screenshot_b64: str | None, history: list[dict]) -> dict:
        raise NotImplementedError

    def diagnose_drift(self, expected_locator: dict, screenshot_b64: str) -> dict:
        raise NotImplementedError

    def chat(self, messages: list[dict], tools: list[dict], tool_choice: dict | str = "auto") -> dict:
        raise NotImplementedError
```

Replace `class FakeLLMClient(LLMClient):` block with:

```python
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
```

In `class _OpenAICompatibleClient(LLMClient):`, add this method (anywhere after `diagnose_drift`, before `_post_chat`):

```python
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
```

In `class FallbackLLMClient(LLMClient):`, add this method (after `diagnose_drift`):

```python
    def chat(self, messages: list[dict], tools: list[dict], tool_choice: dict | str = "auto") -> dict:
        try:
            return self.primary.chat(messages=messages, tools=tools, tool_choice=tool_choice)
        except Exception as exc:
            print(f"[chat] primary provider failed ({type(exc).__name__}: {exc}); falling back...", flush=True)
            return self.fallback.chat(messages=messages, tools=tools, tool_choice=tool_choice)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_llm_client.py -q`
Expected: PASS (all tests in the file, old and new)

- [ ] **Step 5: Commit**

```bash
cd C:/Vijay/PyCode/comp-use
git add comp_use/llm_client.py tests/test_llm_client.py
git commit -m "Add generic tool-calling chat() method to LLMClient for the chat agent"
```

---

## Task 2: Promote `list_capability_names`, rename `_list_versions` and `_build_llm_client` to public

Small, targeted refactor: the chat catalog builder (Task 3) needs a module-level "list every capability name" function, which today only exists as a private closure inside `create_app()`. It also needs the version-listing helper and the LLM-client builder, both of which already exist but are private-by-convention despite being about to gain a second caller outside `comp_use/cli.py`.

**Files:**
- Modify: `comp_use/cli.py`
- Modify: `comp_use/server/app.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `list_capability_names(artifacts_dir: Path) -> list[str]` in `comp_use/cli.py` (dual-mode: Postgres via `pg_store.list_capability_names()` when enabled, else directory listing).
- Produces: `list_versions(capability_name: str, artifacts_dir: Path) -> list[int]` in `comp_use/cli.py` (renamed from `_list_versions`, same behavior).
- Produces: `build_llm_client(settings: Settings) -> LLMClient` in `comp_use/cli.py` (renamed from `_build_llm_client`, same behavior).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`:

```python
def test_list_capability_names_returns_every_capability_with_a_saved_version(tmp_path):
    from comp_use.cli import list_capability_names
    save_artifact(_artifact(capability_name="lookup_member", version=1), tmp_path)
    save_artifact(_artifact(capability_name="transfer_funds", version=1), tmp_path)
    assert sorted(list_capability_names(tmp_path)) == ["lookup_member", "transfer_funds"]


def test_list_capability_names_is_empty_when_artifacts_dir_does_not_exist(tmp_path):
    from comp_use.cli import list_capability_names
    assert list_capability_names(tmp_path / "does_not_exist") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_cli.py -q -k list_capability_names`
Expected: FAIL (`ImportError: cannot import name 'list_capability_names'`)

- [ ] **Step 3: Implement the rename and new function in `comp_use/cli.py`**

Rename `_list_versions` to `list_versions` (the function definition at line 191, and its one internal call site inside `set_default_version` at line 213). Find-and-replace both occurrences of `_list_versions` with `list_versions` in `comp_use/cli.py`.

Add this new function directly above `list_versions`:

```python
def list_capability_names(artifacts_dir: Path) -> list[str]:
    if pg_store.db_enabled():
        return pg_store.list_capability_names()
    artifacts_dir = Path(artifacts_dir)
    if not artifacts_dir.exists():
        return []
    return sorted(p.name for p in artifacts_dir.iterdir() if p.is_dir())
```

Rename `_build_llm_client` to `build_llm_client` (the function definition, and its two internal call sites — search `comp_use/cli.py` for `_build_llm_client(` and replace both call sites plus the `def` line).

- [ ] **Step 4: Update `comp_use/server/app.py` to reuse the promoted function**

Replace the `_capability_names` closure body (around line 82-88):

```python
    def _capability_names() -> list[str]:
        if pg_store.db_enabled():
            return pg_store.list_capability_names()
        artifacts_dir = settings.artifacts_dir
        if not artifacts_dir.exists():
            return []
        return sorted(p.name for p in artifacts_dir.iterdir() if p.is_dir())
```

with:

```python
    def _capability_names() -> list[str]:
        return list_capability_names(settings.artifacts_dir)
```

Add `list_capability_names` to the existing `from comp_use.cli import (...)` block in `comp_use/server/app.py` (alphabetically among the existing names: `approve_artifact, clear_default_version, delete_capability, list_capability_names, load_artifact, reject_artifact, retire_artifact, run_discover, run_replay, set_default_version`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_cli.py tests/test_server.py -q`
Expected: PASS (including every pre-existing test — this step is a pure rename/reuse, no behavior change)

- [ ] **Step 6: Commit**

```bash
cd C:/Vijay/PyCode/comp-use
git add comp_use/cli.py comp_use/server/app.py tests/test_cli.py
git commit -m "Promote list_capability_names/list_versions/build_llm_client to public, reused by the chat catalog"
```

---

## Task 3: `comp_use/chat/catalog.py` — site-scoped capability catalog builder

**Files:**
- Create: `comp_use/chat/__init__.py` (empty)
- Create: `comp_use/chat/catalog.py`
- Test: `tests/test_chat_catalog.py`

**Interfaces:**
- Consumes: `list_capability_names(artifacts_dir) -> list[str]`, `list_versions(name, artifacts_dir) -> list[int]`, `load_artifact(name, artifacts_dir, version=None) -> Artifact` (all from `comp_use.cli`, Task 2).
- Produces: `build_chat_catalog(settings: Settings) -> list[dict]`. Each dict: `{"capability_name": str, "description": str, "target_base_url": str, "input_schema": [{"name": str, "type": str, "required": bool}, ...]}`. Only capabilities with at least one approved version are included (nothing else is invocable, so nothing else belongs in the chat agent's search space).

- [ ] **Step 1: Write the failing test**

Create `tests/test_chat_catalog.py`:

```python
from comp_use.chat.catalog import build_chat_catalog
from comp_use.cli import save_artifact
from comp_use.config import Settings
from comp_use.schemas import Artifact, Checkpoint, CheckpointType, InputParam


def _artifact(capability_name="lookup_member", version=1, status="approved", is_default=False, base_url="http://localhost:5000"):
    return Artifact(
        capability_name=capability_name,
        version=version,
        status=status,
        is_default=is_default,
        description="Looks up a member.",
        target={"app": "mock_bank", "base_url": base_url},
        input_schema=[InputParam(name="member_id", type="string", required=True)],
        steps=[],
        success_checkpoint=Checkpoint(type=CheckpointType.URL_MATCHES, url_pattern="member"),
        created_from_run_id="run_1",
    )


def test_build_chat_catalog_includes_approved_capabilities_with_their_site_and_schema(tmp_path):
    save_artifact(_artifact(), tmp_path)
    settings = Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence")

    catalog = build_chat_catalog(settings)

    assert catalog == [{
        "capability_name": "lookup_member",
        "description": "Looks up a member.",
        "target_base_url": "http://localhost:5000",
        "input_schema": [{"name": "member_id", "type": "string", "required": True}],
    }]


def test_build_chat_catalog_skips_a_capability_with_no_approved_version(tmp_path):
    save_artifact(_artifact(status="draft"), tmp_path)
    settings = Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence")

    assert build_chat_catalog(settings) == []


def test_build_chat_catalog_prefers_the_is_default_version_over_the_highest_number(tmp_path):
    save_artifact(_artifact(version=1, status="approved", is_default=True, base_url="http://a"), tmp_path)
    save_artifact(_artifact(version=2, status="approved", is_default=False, base_url="http://b"), tmp_path)
    settings = Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence")

    catalog = build_chat_catalog(settings)

    assert len(catalog) == 1
    assert catalog[0]["target_base_url"] == "http://a"


def test_build_chat_catalog_is_empty_when_no_capabilities_exist(tmp_path):
    settings = Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence")
    assert build_chat_catalog(settings) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_chat_catalog.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'comp_use.chat'`)

- [ ] **Step 3: Implement**

Create `comp_use/chat/__init__.py` (empty file).

Create `comp_use/chat/catalog.py`:

```python
"""Builds the capability catalog the chat agent reasons over - distinct from
the /capabilities HTTP endpoint's CapabilitySummary shape (comp_use/server/
app.py's list_capabilities()): this adds target_base_url, which the chat
agent needs to scope its search to session.target_site (see
comp_use/chat/agent.py), and drops fields the model has no use for
(output_schema, has_pending_draft)."""
from comp_use.cli import list_capability_names, list_versions, load_artifact
from comp_use.config import Settings


def build_chat_catalog(settings: Settings) -> list[dict]:
    out = []
    for name in list_capability_names(settings.artifacts_dir):
        versions = list_versions(name, settings.artifacts_dir)
        approved_candidates = []  # descending version order
        for v in reversed(versions):
            try:
                candidate = load_artifact(name, settings.artifacts_dir, version=v)
            except Exception:
                # Same reasoning as list_capabilities(): one malformed artifact
                # must never take down the whole catalog for every other
                # capability too.
                continue
            if candidate.status == "approved":
                approved_candidates.append(candidate)
        if not approved_candidates:
            continue  # nothing invocable yet - not part of the chat agent's search space
        chosen = next(
            (c for c in approved_candidates if c.is_default), approved_candidates[0],
        )
        out.append({
            "capability_name": name,
            "description": chosen.description,
            "target_base_url": chosen.target.get("base_url", ""),
            "input_schema": [
                {"name": p.name, "type": p.type, "required": p.required} for p in chosen.input_schema
            ],
        })
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_chat_catalog.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Vijay/PyCode/comp-use
git add comp_use/chat/ tests/test_chat_catalog.py
git commit -m "Add site-scoped capability catalog builder for the chat agent"
```

---

## Task 4: `comp_use/chat/session.py` — in-memory session state

**Files:**
- Create: `comp_use/chat/session.py`
- Test: `tests/test_chat_session.py`

**Interfaces:**
- Produces: `PendingAction` dataclass — `kind: Literal["invoke", "discover"]`, `capability_name: str`, `params: dict | None = None`, `goal: str | None = None`, `param_hints: list[str] | None = None`.
- Produces: `ChatSession` dataclass — `session_id: str`, `messages: list[dict]` (each `{"role": "user"|"assistant", "content": str}`), `target_site: str | None = None`, `pending_action: PendingAction | None = None`, `last_run_id: str | None = None`.
- Produces: `ChatSessionManager` — `.create() -> ChatSession`, `.get(session_id: str) -> ChatSession | None`. Thread-safe (same `threading.Lock` pattern as `RunManager`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_chat_session.py`:

```python
from comp_use.chat.session import ChatSessionManager, PendingAction


def test_create_returns_a_session_with_a_unique_id_and_empty_state():
    manager = ChatSessionManager()
    session = manager.create()
    assert session.session_id
    assert session.messages == []
    assert session.target_site is None
    assert session.pending_action is None
    assert session.last_run_id is None


def test_get_returns_the_same_session_object_created_earlier():
    manager = ChatSessionManager()
    created = manager.create()
    fetched = manager.get(created.session_id)
    assert fetched is created


def test_get_returns_none_for_an_unknown_session_id():
    manager = ChatSessionManager()
    assert manager.get("does_not_exist") is None


def test_two_created_sessions_have_distinct_ids():
    manager = ChatSessionManager()
    a = manager.create()
    b = manager.create()
    assert a.session_id != b.session_id


def test_pending_action_holds_invoke_fields():
    action = PendingAction(kind="invoke", capability_name="lookup_member", params={"member_id": "123"})
    assert action.kind == "invoke"
    assert action.params == {"member_id": "123"}
    assert action.goal is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_chat_session.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'comp_use.chat.session'`)

- [ ] **Step 3: Implement**

Create `comp_use/chat/session.py`:

```python
"""In-memory chat session state - deliberately not persisted to Postgres or
disk (see the spec's "Purpose"/"Architecture": a single ephemeral session per
conversation, matching RunManager's own in-memory-only pattern). A server
restart drops every active chat, the same trade-off RunManager already has."""
import threading
import uuid
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class PendingAction:
    """A proposed invoke or discovery run awaiting the user's confirm/cancel/
    amend (see comp_use/chat/agent.py's resolve_pending handling)."""
    kind: Literal["invoke", "discover"]
    capability_name: str
    params: dict | None = None
    goal: str | None = None
    param_hints: list[str] | None = None


@dataclass
class ChatSession:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    target_site: str | None = None
    pending_action: PendingAction | None = None
    last_run_id: str | None = None


class ChatSessionManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, ChatSession] = {}

    def create(self) -> ChatSession:
        session = ChatSession(session_id=uuid.uuid4().hex)
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> ChatSession | None:
        with self._lock:
            return self._sessions.get(session_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_chat_session.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Vijay/PyCode/comp-use
git add comp_use/chat/session.py tests/test_chat_session.py
git commit -m "Add in-memory ChatSession/ChatSessionManager for the chat agent"
```

---

## Task 5: `comp_use/chat/agent.py` — target-site selection

The first behavior a session needs, before any capability matching can happen: asking which site to work against, from a pick-list of sites already seen in the catalog plus a free-form URL. Fully deterministic — no LLM call.

**Files:**
- Create: `comp_use/chat/agent.py`
- Test: `tests/test_chat_agent.py`

**Interfaces:**
- Consumes: `ChatSession`, `PendingAction` (Task 4); `LLMClient` (Task 1); catalog dicts shaped as in Task 3.
- Produces: `ChatTurnResult` dataclass — `reply_text: str`, `to_execute: PendingAction | None = None`.
- Produces: `ChatAgent` class — `__init__(self, llm: LLMClient)`, `.turn(self, session: ChatSession, user_message: str, catalog: list[dict]) -> ChatTurnResult`. This task implements only the target-site branch; `turn()` routes to it whenever `session.target_site is None` and otherwise raises `NotImplementedError` for now (replaced in Tasks 6-8).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_chat_agent.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_chat_agent.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'comp_use.chat.agent'`)

- [ ] **Step 3: Implement**

Create `comp_use/chat/agent.py`:

```python
"""The chat agent's turn logic - see docs/superpowers/specs/2026-09-04-chat-
agent-design.md for the full design. Deliberately a pure client of the
existing capability/replay/discovery HTTP surface: ChatAgent never imports
DiscoveryAgent/ReplayEngine, and never starts a run itself - a confirmed
PendingAction comes back on ChatTurnResult.to_execute for the caller (the
/chat/sessions/{id}/message endpoint, see comp_use/server/app.py) to run
through the exact same RunManager.start() path invoke_capability/
discover_capability already use."""
from dataclasses import dataclass
from urllib.parse import urlparse

from comp_use.chat.session import ChatSession, PendingAction
from comp_use.llm_client import LLMClient


@dataclass
class ChatTurnResult:
    reply_text: str
    to_execute: PendingAction | None = None


def _known_sites(catalog: list[dict]) -> list[str]:
    seen: list[str] = []
    for c in catalog:
        site = c["target_base_url"]
        if site and site not in seen:
            seen.append(site)
    return seen


def _target_site_prompt(sites: list[str]) -> str:
    if not sites:
        return "Which site should I work against? Reply with a URL."
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(sites))
    return (
        "Which site should I work against?\n"
        f"{numbered}\n"
        f"{len(sites) + 1}. A new URL - reply with it directly"
    )


def _match_site_choice(user_message: str, sites: list[str]) -> str | None:
    text = user_message.strip()
    if text.isdigit():
        idx = int(text) - 1
        return sites[idx] if 0 <= idx < len(sites) else None
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urlparse(text)
        return f"{parsed.scheme}://{parsed.netloc}"
    return next((s for s in sites if s == text), None)


class ChatAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def turn(self, session: ChatSession, user_message: str, catalog: list[dict]) -> ChatTurnResult:
        session.messages.append({"role": "user", "content": user_message})

        if session.target_site is None:
            return self._handle_target_site_selection(session, user_message, catalog)

        raise NotImplementedError("normal-turn and pending-resolution handling land in later tasks")

    def _handle_target_site_selection(self, session: ChatSession, user_message: str, catalog: list[dict]) -> ChatTurnResult:
        sites = _known_sites(catalog)
        chosen = _match_site_choice(user_message, sites)
        if chosen is None:
            reply = _target_site_prompt(sites)
            session.messages.append({"role": "assistant", "content": reply})
            return ChatTurnResult(reply_text=reply)
        session.target_site = chosen
        reply = f"Working against {chosen}. What would you like to do?"
        session.messages.append({"role": "assistant", "content": reply})
        return ChatTurnResult(reply_text=reply)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_chat_agent.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Vijay/PyCode/comp-use
git add comp_use/chat/agent.py tests/test_chat_agent.py
git commit -m "Add ChatAgent target-site selection (deterministic, no LLM call)"
```

---

## Task 6: `comp_use/chat/agent.py` — `propose_invoke` handling

**Files:**
- Modify: `comp_use/chat/agent.py`
- Modify: `tests/test_chat_agent.py`

**Interfaces:**
- Consumes: `is_sensitive_param_name(name: str | None) -> bool` from `comp_use.guardrail`.
- Produces: `_handle_normal_turn(session, user_message, catalog, llm) -> ChatTurnResult` (routes to `_propose_invoke`/`_propose_discovery`/plain text based on the model's tool choice; `propose_discovery` branch raises `NotImplementedError` until Task 7). `turn()` now calls this when `session.pending_action is None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_chat_agent.py`:

```python
_ONE_SITE_CATALOG = [
    {"capability_name": "lookup_member", "description": "Looks up a member.",
     "target_base_url": "https://web-sample.interface-hiring.com",
     "input_schema": [{"name": "member_id", "type": "string", "required": True},
                       {"name": "password", "type": "string", "required": False}]},
]


def _session_with_site(site="https://web-sample.interface-hiring.com"):
    session = ChatSessionManager().create()
    session.target_site = site
    return session


def test_plain_text_reply_is_passed_through_and_appended_to_history():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[{"type": "text", "text": "Sure, which member?"}]))
    session = _session_with_site()

    result = agent.turn(session, "check a balance", _ONE_SITE_CATALOG)

    assert result.reply_text == "Sure, which member?"
    assert result.to_execute is None
    assert session.messages[-1] == {"role": "assistant", "content": "Sure, which member?"}


def test_propose_invoke_with_complete_params_asks_for_confirmation_and_sets_pending():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "lookup_member", "params": {"member_id": "100234"}}},
    ]))
    session = _session_with_site()

    result = agent.turn(session, "check member 100234", _ONE_SITE_CATALOG)

    assert "lookup_member" in result.reply_text
    assert "100234" in result.reply_text
    assert result.to_execute is None  # not executed yet - awaiting confirm
    assert session.pending_action.kind == "invoke"
    assert session.pending_action.capability_name == "lookup_member"
    assert session.pending_action.params == {"member_id": "100234"}


def test_propose_invoke_masks_sensitive_param_values_in_the_confirmation_text():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "lookup_member", "params": {"member_id": "100234", "password": "hunter2"}}},
    ]))
    session = _session_with_site()

    result = agent.turn(session, "sign on and check member 100234", _ONE_SITE_CATALOG)

    assert "hunter2" not in result.reply_text
    assert "\u2022\u2022\u2022\u2022\u2022\u2022" in result.reply_text
    assert session.pending_action.params["password"] == "hunter2"  # stored value stays real


def test_propose_invoke_with_missing_required_param_asks_for_it_without_setting_pending():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "lookup_member", "params": {}}},
    ]))
    session = _session_with_site()

    result = agent.turn(session, "check a member", _ONE_SITE_CATALOG)

    assert "member_id" in result.reply_text
    assert session.pending_action is None


def test_propose_invoke_for_an_unknown_capability_reports_it_without_setting_pending():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "does_not_exist", "params": {}}},
    ]))
    session = _session_with_site()

    result = agent.turn(session, "do something odd", _ONE_SITE_CATALOG)

    assert "does_not_exist" in result.reply_text
    assert session.pending_action is None


class _RecordingLLM:
    """A minimal test double that captures what ChatAgent sent, instead of
    scripting a reply - used for tests that assert on the OUTGOING request
    (e.g. which capabilities were offered), not just the returned decision."""

    def __init__(self, reply):
        self.reply = reply
        self.seen_tools = None
        self.seen_messages = None

    def chat(self, messages, tools, tool_choice="auto"):
        self.seen_messages = messages
        self.seen_tools = tools
        return self.reply


def test_normal_turn_only_offers_capabilities_matching_the_session_target_site():
    two_site_catalog = _ONE_SITE_CATALOG + [
        {"capability_name": "other_site_thing", "description": "d",
         "target_base_url": "http://localhost:5000", "input_schema": []},
    ]
    llm = _RecordingLLM({"type": "text", "text": "ok"})
    agent = ChatAgent(llm)
    session = _session_with_site("https://web-sample.interface-hiring.com")

    agent.turn(session, "do the other site thing", two_site_catalog)

    system_content = llm.seen_messages[0]["content"]
    assert "lookup_member" in system_content
    assert "other_site_thing" not in system_content
```

Put `_RecordingLLM` near the top of the test file (after the imports and `_CATALOG`/`_ONE_SITE_CATALOG` constants) so later tasks can reuse it too.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_chat_agent.py -q`
Expected: FAIL (`NotImplementedError` from the Task 5 stub)

- [ ] **Step 3: Implement**

In `comp_use/chat/agent.py`, add imports at the top:

```python
import json

from comp_use.guardrail import is_sensitive_param_name
```

Add tool schema constants after the imports, before `_known_sites`:

```python
_PROPOSE_INVOKE_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_invoke",
        "description": "Propose running an existing recorded capability to satisfy the user's request.",
        "parameters": {
            "type": "object",
            "properties": {
                "capability_name": {"type": "string"},
                "params": {"type": "object", "description": "Typed arguments for the capability, keyed by parameter name."},
            },
            "required": ["capability_name", "params"],
        },
    },
}

_PROPOSE_DISCOVERY_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_discovery",
        "description": "Propose recording a NEW capability because nothing in the catalog covers the user's request.",
        "parameters": {
            "type": "object",
            "properties": {
                "capability_name": {"type": "string", "description": "A new snake_case name for this capability."},
                "goal": {"type": "string", "description": "The natural-language goal to give the discovery agent."},
                "param_hints": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["capability_name", "goal"],
        },
    },
}


def _system_prompt(session: ChatSession, site_catalog: list[dict]) -> str:
    return (
        f"You are a chat agent driving MERIDIAN-style capability APIs against {session.target_site}.\n"
        "Available capabilities for this site (JSON):\n"
        f"{json.dumps(site_catalog)}\n\n"
        "If one of these capabilities satisfies the user's request, call propose_invoke with its "
        "name and every param value you can determine from the conversation. If none of them do, "
        "call propose_discovery. If you need more information before you can call either, just "
        "reply in plain text asking for it."
    )


def _build_messages(session: ChatSession, site_catalog: list[dict]) -> list[dict]:
    return [{"role": "system", "content": _system_prompt(session, site_catalog)}] + session.messages
```

Replace the `raise NotImplementedError(...)` line in `turn()` with:

```python
        if session.pending_action is not None:
            raise NotImplementedError("pending-resolution handling lands in Task 8")

        return self._handle_normal_turn(session, catalog)
```

Add these methods to `ChatAgent` (after `_handle_target_site_selection`):

```python
    def _handle_normal_turn(self, session: ChatSession, catalog: list[dict]) -> ChatTurnResult:
        site_catalog = [c for c in catalog if c["target_base_url"] == session.target_site]
        result = self.llm.chat(
            messages=_build_messages(session, site_catalog),
            tools=[_PROPOSE_INVOKE_TOOL, _PROPOSE_DISCOVERY_TOOL],
            tool_choice="auto",
        )

        if result["type"] == "text":
            reply = result["text"]
            session.messages.append({"role": "assistant", "content": reply})
            return ChatTurnResult(reply_text=reply)

        args = result["arguments"]
        if result["name"] == "propose_invoke":
            return self._propose_invoke(session, site_catalog, args["capability_name"], args.get("params") or {})
        if result["name"] == "propose_discovery":
            raise NotImplementedError("propose_discovery handling lands in Task 7")

        reply = "Sorry, I couldn't figure out how to help with that - could you rephrase?"
        session.messages.append({"role": "assistant", "content": reply})
        return ChatTurnResult(reply_text=reply)

    def _propose_invoke(self, session: ChatSession, site_catalog: list[dict], capability_name: str, params: dict) -> ChatTurnResult:
        capability = next((c for c in site_catalog if c["capability_name"] == capability_name), None)
        if capability is None:
            reply = f"I don't have a capability called `{capability_name}` for {session.target_site}."
            session.messages.append({"role": "assistant", "content": reply})
            return ChatTurnResult(reply_text=reply)

        missing = [p["name"] for p in capability["input_schema"] if p["required"] and p["name"] not in params]
        if missing:
            reply = f"To run `{capability_name}` I still need: {', '.join(missing)}."
            session.messages.append({"role": "assistant", "content": reply})
            return ChatTurnResult(reply_text=reply)

        shown_params = {k: ("\u2022\u2022\u2022\u2022\u2022\u2022" if is_sensitive_param_name(k) else v) for k, v in params.items()}
        reply = f"I'll run `{capability_name}` with {shown_params} \u2014 proceed? (yes/no)"
        session.messages.append({"role": "assistant", "content": reply})
        session.pending_action = PendingAction(kind="invoke", capability_name=capability_name, params=params)
        return ChatTurnResult(reply_text=reply)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_chat_agent.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Vijay/PyCode/comp-use
git add comp_use/chat/agent.py tests/test_chat_agent.py
git commit -m "Add ChatAgent propose_invoke handling with param validation and masked confirmation"
```

---

## Task 7: `comp_use/chat/agent.py` — `propose_discovery` handling

**Files:**
- Modify: `comp_use/chat/agent.py`
- Modify: `tests/test_chat_agent.py`

**Interfaces:**
- Produces: `_slugify(name: str) -> str`; `_propose_discovery(session, capability_name, goal, param_hints) -> ChatTurnResult`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_chat_agent.py`:

```python
def test_propose_discovery_asks_for_confirmation_and_sets_pending_with_a_slugified_name():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_discovery",
         "arguments": {"capability_name": "Close a Share!", "goal": "close a share for a member"}},
    ]))
    session = _session_with_site()

    result = agent.turn(session, "close a share for member 100234", _ONE_SITE_CATALOG)

    assert "close_a_share" in result.reply_text
    assert session.target_site in result.reply_text
    assert result.to_execute is None
    assert session.pending_action.kind == "discover"
    assert session.pending_action.capability_name == "close_a_share"
    assert session.pending_action.goal == "close a share for a member"


def test_propose_discovery_passes_through_param_hints():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_discovery",
         "arguments": {"capability_name": "close_a_share", "goal": "close a share", "param_hints": ["share_id"]}},
    ]))
    session = _session_with_site()

    agent.turn(session, "close a share", _ONE_SITE_CATALOG)

    assert session.pending_action.param_hints == ["share_id"]


def test_slugify_collapses_non_alphanumerics_and_lowercases():
    from comp_use.chat.agent import _slugify
    assert _slugify("Close a Share!") == "close_a_share"
    assert _slugify("  already_snake_case  ") == "already_snake_case"
    assert _slugify("!!!") == "capability"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_chat_agent.py -q -k discovery`
Expected: FAIL (`NotImplementedError` from Task 6's stub, `_slugify` doesn't exist)

- [ ] **Step 3: Implement**

In `comp_use/chat/agent.py`, add `import re` to the imports.

Add `_slugify` near `_known_sites`:

```python
def _slugify(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return text or "capability"
```

Replace the `raise NotImplementedError("propose_discovery handling lands in Task 7")` line in `_handle_normal_turn` with:

```python
        if result["name"] == "propose_discovery":
            return self._propose_discovery(session, args["capability_name"], args["goal"], args.get("param_hints"))
```

Add this method to `ChatAgent` (after `_propose_invoke`):

```python
    def _propose_discovery(self, session: ChatSession, capability_name: str, goal: str, param_hints: list[str] | None) -> ChatTurnResult:
        slug = _slugify(capability_name)
        reply = (
            f"No existing capability for {session.target_site} covers that. "
            f"I'll record a new one \u2014 `{slug}` \u2014 against {session.target_site} with goal: \"{goal}\". "
            "Proceed? (yes/no)"
        )
        session.messages.append({"role": "assistant", "content": reply})
        session.pending_action = PendingAction(kind="discover", capability_name=slug, goal=goal, param_hints=param_hints)
        return ChatTurnResult(reply_text=reply)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_chat_agent.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Vijay/PyCode/comp-use
git add comp_use/chat/agent.py tests/test_chat_agent.py
git commit -m "Add ChatAgent propose_discovery handling with capability-name slugification"
```

---

## Task 8: `comp_use/chat/agent.py` — `resolve_pending` (confirm/cancel/amend)

**Files:**
- Modify: `comp_use/chat/agent.py`
- Modify: `tests/test_chat_agent.py`

**Interfaces:**
- Produces: the final `turn()` wiring — routes to `_handle_pending_resolution` whenever `session.pending_action is not None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_chat_agent.py`:

```python
def test_confirm_clears_pending_and_returns_it_as_to_execute():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "lookup_member", "params": {"member_id": "100234"}}},
        {"type": "tool_call", "name": "resolve_pending", "arguments": {"decision": "confirm"}},
    ]))
    session = _session_with_site()
    agent.turn(session, "check member 100234", _ONE_SITE_CATALOG)

    result = agent.turn(session, "yes", _ONE_SITE_CATALOG)

    assert session.pending_action is None
    assert result.to_execute is not None
    assert result.to_execute.kind == "invoke"
    assert result.to_execute.capability_name == "lookup_member"


def test_cancel_clears_pending_without_executing_anything():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "lookup_member", "params": {"member_id": "100234"}}},
        {"type": "tool_call", "name": "resolve_pending", "arguments": {"decision": "cancel"}},
    ]))
    session = _session_with_site()
    agent.turn(session, "check member 100234", _ONE_SITE_CATALOG)

    result = agent.turn(session, "no, never mind", _ONE_SITE_CATALOG)

    assert session.pending_action is None
    assert result.to_execute is None


def test_amend_clears_pending_and_falls_through_to_a_normal_turn():
    agent = ChatAgent(FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "lookup_member", "params": {"member_id": "100234"}}},
        {"type": "tool_call", "name": "resolve_pending", "arguments": {"decision": "amend"}},
        {"type": "tool_call", "name": "propose_invoke",
         "arguments": {"capability_name": "lookup_member", "params": {"member_id": "999999"}}},
    ]))
    session = _session_with_site()
    agent.turn(session, "check member 100234", _ONE_SITE_CATALOG)

    result = agent.turn(session, "actually make it 999999", _ONE_SITE_CATALOG)

    assert session.pending_action.params == {"member_id": "999999"}
    assert result.to_execute is None


def test_resolve_pending_is_called_with_a_forced_tool_choice():
    llm = _RecordingLLM({"type": "tool_call", "name": "resolve_pending", "arguments": {"decision": "confirm"}})
    agent = ChatAgent(llm)
    session = _session_with_site()
    session.pending_action = PendingAction(kind="invoke", capability_name="lookup_member", params={"member_id": "100234"})

    agent.turn(session, "yes", _ONE_SITE_CATALOG)

    assert llm.seen_tools == [_RESOLVE_PENDING_TOOL_FOR_TEST]
    assert llm.seen_tool_choice == {"type": "function", "function": {"name": "resolve_pending"}}
```

This last test needs the recording fake to also capture `tool_choice`, and needs the real tool schema constant importable for comparison. Update `_RecordingLLM` (defined earlier in the same file) to add `self.seen_tool_choice = None` in `__init__` and set it inside `chat()`:

```python
class _RecordingLLM:
    def __init__(self, reply):
        self.reply = reply
        self.seen_tools = None
        self.seen_messages = None
        self.seen_tool_choice = None

    def chat(self, messages, tools, tool_choice="auto"):
        self.seen_messages = messages
        self.seen_tools = tools
        self.seen_tool_choice = tool_choice
        return self.reply
```

Add this import at the top of `tests/test_chat_agent.py`:

```python
from comp_use.chat.agent import _RESOLVE_PENDING_TOOL as _RESOLVE_PENDING_TOOL_FOR_TEST
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_chat_agent.py -q -k "confirm or cancel or amend or resolve_pending"`
Expected: FAIL (`NotImplementedError` from Task 6's `turn()` stub, `_RESOLVE_PENDING_TOOL` doesn't exist)

- [ ] **Step 3: Implement**

In `comp_use/chat/agent.py`, add the tool constant after `_PROPOSE_DISCOVERY_TOOL`:

```python
_RESOLVE_PENDING_TOOL = {
    "type": "function",
    "function": {
        "name": "resolve_pending",
        "description": "Interpret the user's reply to a pending confirmation.",
        "parameters": {
            "type": "object",
            "properties": {"decision": {"type": "string", "enum": ["confirm", "cancel", "amend"]}},
            "required": ["decision"],
        },
    },
}
```

Add a helper next to `_build_messages`:

```python
def _pending_system_note(pending: PendingAction) -> str:
    return (
        f"There is a pending action awaiting the user's confirmation: {pending.kind} "
        f"`{pending.capability_name}`. Call resolve_pending with decision=confirm if the user "
        "agreed, cancel if they declined, or amend if they want to change something about the request."
    )
```

Update `_build_messages` to accept an optional extra system note:

```python
def _build_messages(session: ChatSession, site_catalog: list[dict], extra_system: str | None = None) -> list[dict]:
    system = _system_prompt(session, site_catalog)
    if extra_system:
        system += "\n\n" + extra_system
    return [{"role": "system", "content": system}] + session.messages
```

Update the one existing call site inside `_handle_normal_turn` (`_build_messages(session, site_catalog)`) — no change needed there, `extra_system` defaults to `None`.

Replace the `raise NotImplementedError("pending-resolution handling lands in Task 8")` line in `turn()` with:

```python
        if session.pending_action is not None:
            return self._handle_pending_resolution(session, user_message, catalog)
```

Add this method to `ChatAgent` (after `_handle_target_site_selection`, before `_handle_normal_turn`):

```python
    def _handle_pending_resolution(self, session: ChatSession, user_message: str, catalog: list[dict]) -> ChatTurnResult:
        site_catalog = [c for c in catalog if c["target_base_url"] == session.target_site]
        pending = session.pending_action
        result = self.llm.chat(
            messages=_build_messages(session, site_catalog, extra_system=_pending_system_note(pending)),
            tools=[_RESOLVE_PENDING_TOOL],
            tool_choice={"type": "function", "function": {"name": "resolve_pending"}},
        )
        decision = result["arguments"]["decision"] if result["type"] == "tool_call" else "amend"

        if decision == "confirm":
            session.pending_action = None
            reply = f"Starting {pending.kind} for `{pending.capability_name}`..."
            session.messages.append({"role": "assistant", "content": reply})
            return ChatTurnResult(reply_text=reply, to_execute=pending)

        if decision == "cancel":
            session.pending_action = None
            reply = "Okay, cancelled."
            session.messages.append({"role": "assistant", "content": reply})
            return ChatTurnResult(reply_text=reply)

        session.pending_action = None
        return self._handle_normal_turn(session, catalog)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_chat_agent.py -q`
Expected: PASS (every test in the file)

- [ ] **Step 5: Run the full backend test suite**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest -q`
Expected: PASS (no regressions in the rest of the suite)

- [ ] **Step 6: Commit**

```bash
cd C:/Vijay/PyCode/comp-use
git add comp_use/chat/agent.py tests/test_chat_agent.py
git commit -m "Add ChatAgent resolve_pending confirm/cancel/amend handling (forced tool choice)"
```

---

## Task 9: `POST /chat/sessions` and `POST /chat/sessions/{id}/message` endpoints

**Files:**
- Modify: `comp_use/server/app.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `ChatSessionManager`, `ChatAgent`, `ChatTurnResult`, `build_chat_catalog`, `build_llm_client` (Tasks 1-8, and the Task 2 rename).
- Produces: `POST /chat/sessions` → `{"session_id": str}` (201). `POST /chat/sessions/{session_id}/message` (body `{"message": str}`) → `{"reply": str, "run_id": str | None}`. 404 for an unknown `session_id`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_server.py`:

```python
def test_create_chat_session_returns_a_session_id(tmp_path):
    client, _ = _client(tmp_path)
    response = client.post("/chat/sessions")
    assert response.status_code == 201
    assert response.json()["session_id"]


def test_send_chat_message_404s_for_an_unknown_session_id(tmp_path):
    client, _ = _client(tmp_path)
    response = client.post("/chat/sessions/does_not_exist/message", json={"message": "hi"})
    assert response.status_code == 404


def test_send_chat_message_asks_for_a_target_site_on_the_first_turn(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="approved"), settings.artifacts_dir)
    client.app.state.chat_agent = ChatAgent(FakeLLMClient())  # target-site turns never call the LLM

    session_id = client.post("/chat/sessions").json()["session_id"]
    response = client.post(f"/chat/sessions/{session_id}/message", json={"message": "check a balance"})

    assert response.status_code == 200
    assert "mock_bank" not in response.json()["reply"]  # sanity: not echoing internal target.app label
    assert "http://localhost:5000" in response.json()["reply"]
    assert response.json()["run_id"] is None


def test_send_chat_message_starts_a_run_on_confirm(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="approved"), settings.artifacts_dir)
    monkeypatch.setattr(
        app_module, "run_replay",
        lambda capability_name, params, confirm_risky, diagnose_drift_on_failure, transport, version=None, run_id=None:
            ReplayResult(outcome=OutcomeType.SUCCESS, outputs={"balance": "100"}),
    )
    fake_llm = FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_invoke", "arguments": {"capability_name": "lookup_member", "params": {"member_id": "12345"}}},
        {"type": "tool_call", "name": "resolve_pending", "arguments": {"decision": "confirm"}},
    ])
    client.app.state.chat_agent = ChatAgent(fake_llm)

    session_id = client.post("/chat/sessions").json()["session_id"]
    client.post(f"/chat/sessions/{session_id}/message", json={"message": "http://localhost:5000"})  # pick the site
    client.post(f"/chat/sessions/{session_id}/message", json={"message": "check member 12345"})
    response = client.post(f"/chat/sessions/{session_id}/message", json={"message": "yes"})

    assert response.status_code == 200
    run_id = response.json()["run_id"]
    assert run_id is not None
    body = _wait_for_status(client, run_id, "done")
    assert body["result"]["outcome"] == "success"


def test_send_chat_message_starts_a_discovery_run_on_confirm(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="approved"), settings.artifacts_dir)
    started_with = {}

    def fake_run_discover(goal, start_url, capability_name, confirm_risky, transport, interactive, param_hints=None, run_id=None):
        started_with["start_url"] = start_url
        started_with["goal"] = goal
        return None  # discovery "failed" - fine, this test only checks the run got started with the right args

    monkeypatch.setattr(app_module, "run_discover", fake_run_discover)
    fake_llm = FakeLLMClient(scripted_chat_turns=[
        {"type": "tool_call", "name": "propose_discovery", "arguments": {"capability_name": "close_a_share", "goal": "close a share"}},
        {"type": "tool_call", "name": "resolve_pending", "arguments": {"decision": "confirm"}},
    ])
    client.app.state.chat_agent = ChatAgent(fake_llm)

    session_id = client.post("/chat/sessions").json()["session_id"]
    client.post(f"/chat/sessions/{session_id}/message", json={"message": "http://localhost:5000"})
    client.post(f"/chat/sessions/{session_id}/message", json={"message": "close a share"})
    response = client.post(f"/chat/sessions/{session_id}/message", json={"message": "yes"})

    run_id = response.json()["run_id"]
    assert run_id is not None
    _wait_for_status(client, run_id, "done")
    assert started_with["start_url"] == "http://localhost:5000"
    assert started_with["goal"] == "close a share"
```

Add the needed imports at the top of `tests/test_server.py`: `from comp_use.chat.agent import ChatAgent` and `from comp_use.llm_client import FakeLLMClient`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_server.py -q -k chat_session`
Expected: FAIL (404 on `/chat/sessions` - route doesn't exist yet)

- [ ] **Step 3: Implement**

In `comp_use/server/app.py`, update the `from comp_use.cli import (...)` block to also import `build_llm_client` (renamed in Task 2):

```python
from comp_use.cli import (
    approve_artifact,
    build_llm_client,
    clear_default_version,
    delete_capability,
    list_capability_names,
    load_artifact,
    reject_artifact,
    retire_artifact,
    run_discover,
    run_replay,
    set_default_version,
)
```

Add new imports directly below it:

```python
from comp_use.chat.agent import ChatAgent
from comp_use.chat.catalog import build_chat_catalog
from comp_use.chat.session import ChatSessionManager
```

Add a request model next to `ResumeRequest`:

```python
class ChatMessageRequest(BaseModel):
    message: str
```

In `create_app()`, add session/agent state right after `app.state.run_manager = RunManager()`:

```python
    app.state.chat_sessions = ChatSessionManager()
    app.state.chat_agent = ChatAgent(build_llm_client(settings))
```

Add the two endpoints. Place them right after the existing `@app.delete("/capabilities/{name}")` block, before `return app`:

```python
    @app.post("/chat/sessions", status_code=201)
    def create_chat_session():
        session = app.state.chat_sessions.create()
        return {"session_id": session.session_id}

    @app.post("/chat/sessions/{session_id}/message")
    def send_chat_message(session_id: str, body: ChatMessageRequest):
        session = app.state.chat_sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session_id '{session_id}'")

        catalog = build_chat_catalog(settings)
        result = app.state.chat_agent.turn(session, body.message, catalog)

        run_id = None
        pending = result.to_execute
        if pending is not None:
            if pending.kind == "invoke":
                run_id = f"invoke_{int(time.time() * 1000)}"

                def target(transport, _name=pending.capability_name, _params=pending.params, _run_id=run_id):
                    return run_replay(_name, _params, False, False, transport, run_id=_run_id)

                app.state.run_manager.start("invoke", pending.capability_name, target, run_id=run_id)
            else:
                run_id = f"discover_{int(time.time() * 1000)}"

                def target(
                    transport, _name=pending.capability_name, _goal=pending.goal,
                    _hints=pending.param_hints, _run_id=run_id, _start_url=session.target_site,
                ):
                    artifact = run_discover(
                        _goal, _start_url, _name, False, transport, False,
                        param_hints=_hints, run_id=_run_id,
                    )
                    if artifact is None:
                        return {"succeeded": False, "artifact_version": None}
                    return {"succeeded": True, "artifact_version": artifact.version}

                app.state.run_manager.start("discover", pending.capability_name, target, run_id=run_id)
            session.last_run_id = run_id

        return {"reply": result.reply_text, "run_id": run_id}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest tests/test_server.py -q -k chat_session`
Expected: PASS

- [ ] **Step 5: Run the full backend test suite**

Run: `cd C:/Vijay/PyCode/comp-use && .venv/Scripts/python.exe -m pytest -q`
Expected: PASS (no regressions)

- [ ] **Step 6: Commit**

```bash
cd C:/Vijay/PyCode/comp-use
git add comp_use/server/app.py tests/test_server.py
git commit -m "Add POST /chat/sessions and /chat/sessions/{id}/message endpoints"
```

---

## Task 10: Frontend — extract `RunPanel` from the dashboard's `RunDetailPanel`

Pure refactor, no behavior change: the live-run view (polling, status, escalation box, event log, embedded noVNC feed, take-control, delete) becomes a standalone component so Task 11 can embed it in chat.

**Files:**
- Create: `frontend/src/components/RunPanel.tsx`
- Modify: `frontend/src/pages/Dashboard.tsx`

**Interfaces:**
- Produces: `export function RunPanel({ runId, onDeleted }: { runId: string; onDeleted?: () => void })` — identical behavior to the current `RunDetailPanel`, except `onDeleted` is now optional (defaults to a no-op) so it can be embedded in chat without a "go back to empty state" concept.

- [ ] **Step 1: Create `frontend/src/components/RunPanel.tsx`**

Move the entire `RunDetailPanel` function body (currently `frontend/src/pages/Dashboard.tsx` lines 186-383, i.e. `eventToLine` through the end of the `RunDetailPanel` function) into this new file, renamed to `RunPanel`, with its own imports:

```tsx
import { useEffect, useState } from "react";
import { api, type RunDetail, type RunEvent } from "../api";
import { BrowserFeedFrame } from "./BrowserFeedFrame";

const ACTIVE_STATUSES = new Set(["running", "escalated"]);

function statusColor(status: string): string {
  if (status === "done") return "dot-green";
  if (status === "error") return "dot-red";
  if (status === "escalated") return "dot-amber";
  return "dot-blue";
}

function eventToLine(e: RunEvent): string {
  switch (e.event_type) {
    case "decision": {
      const d = e.data as { action?: string };
      return `decision: ${d.action ?? "?"}`;
    }
    case "skipped_decision":
      return `skipped: ${(e.data as { reason?: string }).reason ?? ""}`;
    case "loop_detected":
      return `LOOP DETECTED: ${JSON.stringify(e.data)}`;
    case "sandbox_started":
      return `sandbox started`;
    case "replay_step":
      return `step ${(e.data as { index?: number }).index} -> ${(e.data as { action?: string }).action}`;
    default:
      return `${e.event_type}: ${JSON.stringify(e.data).slice(0, 120)}`;
  }
}

export function RunPanel({ runId, onDeleted = () => {} }: { runId: string; onDeleted?: () => void }) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [note, setNote] = useState("");
  const [confirmingTakeover, setConfirmingTakeover] = useState(false);
  const [takeoverBusy, setTakeoverBusy] = useState(false);
  const [takeoverError, setTakeoverError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    async function poll() {
      try {
        const d = await api.getRun(runId);
        const evs = await api.getRunEvents(runId);
        if (cancelled) return;
        setDetail(d);
        setEvents(evs);
        if (ACTIVE_STATUSES.has(d.status)) {
          timer = window.setTimeout(poll, 1500);
        }
      } catch {
        if (!cancelled) timer = window.setTimeout(poll, 3000);
      }
    }
    poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [runId]);

  if (!detail) return <div className="panel">Loading…</div>;

  async function resume() {
    await api.resume(runId, note);
    setNote("");
  }

  async function confirmTakeover() {
    setTakeoverBusy(true);
    setTakeoverError(null);
    try {
      await api.takeover(runId);
      setConfirmingTakeover(false);
    } catch (e) {
      setTakeoverError(e instanceof Error ? e.message : String(e));
    } finally {
      setTakeoverBusy(false);
    }
  }

  async function confirmDeleteRun() {
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await api.deleteRun(runId);
      onDeleted();
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : String(e));
      setDeleteBusy(false);
    }
  }

  const canTakeControl = detail.status === "running" && !!detail.novnc_url;
  const feedInteractive = detail.status === "escalated";
  const showFeed = ACTIVE_STATUSES.has(detail.status) && !!detail.novnc_url;
  const canDelete = !ACTIVE_STATUSES.has(detail.status);

  return (
    <div className="run-detail">
      <div className="run-detail-header">
        <span className="mono">{runId}</span>
        <span className={`dot ${statusColor(detail.status)}`} />
        <span>{detail.status}</span>
        {canDelete && !confirmingDelete && (
          <button className="danger-button" onClick={() => setConfirmingDelete(true)}>
            Delete run
          </button>
        )}
        {canDelete && confirmingDelete && (
          <span className="confirm-row confirm-row-inline">
            <span className="muted small">Delete this run's evidence permanently?</span>
            <button className="danger-button" disabled={deleteBusy} onClick={confirmDeleteRun}>
              {deleteBusy ? "Deleting…" : "Confirm delete"}
            </button>
            <button disabled={deleteBusy} onClick={() => setConfirmingDelete(false)}>
              Cancel
            </button>
          </span>
        )}
        {canTakeControl && !confirmingTakeover && (
          <button className="takeover-button" onClick={() => setConfirmingTakeover(true)}>
            Take control
          </button>
        )}
        {canTakeControl && confirmingTakeover && (
          <span className="confirm-row confirm-row-inline">
            <span className="muted small">Pause the agent and hand you the browser?</span>
            <button className="takeover-button" disabled={takeoverBusy} onClick={confirmTakeover}>
              {takeoverBusy ? "Requesting…" : "Confirm take control"}
            </button>
            <button disabled={takeoverBusy} onClick={() => setConfirmingTakeover(false)}>
              Cancel
            </button>
          </span>
        )}
      </div>
      {takeoverError && <p className="error">{takeoverError}</p>}
      {deleteError && <p className="error">{deleteError}</p>}

      {detail.escalation && (
        <div className="escalation-box">
          <strong>{detail.escalation.reason === "manual takeover requested by operator" ? "You're in control:" : "Escalated:"}</strong>{" "}
          {detail.escalation.reason}
          {detail.escalation.screenshot_url && (
            <img src={detail.escalation.screenshot_url} alt="escalation screenshot" className="escalation-shot" />
          )}
          <div className="resume-form">
            <input
              placeholder="What did you do? (optional)"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
            <button onClick={resume}>
              {detail.escalation.reason === "manual takeover requested by operator" ? "Hand back to agent" : "Resume"}
            </button>
          </div>
        </div>
      )}

      {detail.result && (
        <div className="result-box">
          <strong>outcome:</strong> {detail.result.outcome}
          {detail.result.detail && <div className="muted">{detail.result.detail}</div>}
          {Object.keys(detail.result.outputs ?? {}).length > 0 && (
            <pre>{JSON.stringify(detail.result.outputs, null, 2)}</pre>
          )}
        </div>
      )}
      {detail.discover_result && (
        <div className="result-box">
          <strong>discovery:</strong> {detail.discover_result.succeeded ? "succeeded" : "failed"}
          {detail.discover_result.artifact_version != null && ` (v${detail.discover_result.artifact_version})`}
        </div>
      )}
      {detail.error && <div className="error-box">{detail.error}</div>}

      <div className={showFeed ? "split" : "split split-full"}>
        <div className="log-panel">
          {events.length === 0 && <p className="muted">waiting for events…</p>}
          {events.map((e, i) => (
            <div key={i} className="log-line">
              {eventToLine(e)}
            </div>
          ))}
        </div>
        {showFeed && (
          <div className="feed-panel">
            <BrowserFeedFrame url={detail.novnc_url!} interactive={feedInteractive} />
          </div>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Update `frontend/src/pages/Dashboard.tsx`**

Delete the `eventToLine` function and the entire `RunDetailPanel` function (lines 186-383 in the current file — everything from `// ---- Run detail: status + event log + browser feed + resume ----` through the closing `}` of `RunDetailPanel`).

Update the import line at the top from:

```tsx
import { useEffect, useMemo, useState } from "react";
import { api, type CapabilitySummary, type RunDetail, type RunEvent, type RunSummary, type VersionSummary } from "../api";
import { BrowserFeedFrame } from "../components/BrowserFeedFrame";
import type { Page } from "../components/Nav";
```

to:

```tsx
import { useEffect, useMemo, useState } from "react";
import { api, type CapabilitySummary, type RunSummary, type VersionSummary } from "../api";
import { RunPanel } from "../components/RunPanel";
import type { Page } from "../components/Nav";
```

(`RunDetail`/`RunEvent` types and `BrowserFeedFrame` are no longer referenced directly in this file — they moved into `RunPanel.tsx`. `statusColor` is still used by the sidebar's run list, so it stays in `Dashboard.tsx`.)

Update the render call from:

```tsx
          <RunDetailPanel key={selectedRunId} runId={selectedRunId} onDeleted={() => setSelectedRunId(null)} />
```

to:

```tsx
          <RunPanel key={selectedRunId} runId={selectedRunId} onDeleted={() => setSelectedRunId(null)} />
```

- [ ] **Step 3: Type-check**

Run: `cd C:/Vijay/PyCode/comp-use/frontend && npx tsc --noEmit`
Expected: no output (clean)

- [ ] **Step 4: Manually verify the dashboard is unchanged**

Start the backend (`cd C:/Vijay/PyCode/comp-use && set -a && source .env 2>/dev/null; set +a && ALLOWED_URL_PREFIXES=https://web-sample.interface-hiring.com COMP_USE_SANDBOX=1 .venv/Scripts/python.exe -m comp_use.cli serve --port 8126`) and the frontend (`cd frontend && npm run dev`), then in a browser: select a capability, invoke it, confirm the run detail panel (status, event log, feed once a run is active, escalation box, take-control, delete run) all render and behave exactly as before this refactor.

- [ ] **Step 5: Commit**

```bash
cd C:/Vijay/PyCode/comp-use
git add frontend/src/components/RunPanel.tsx frontend/src/pages/Dashboard.tsx
git commit -m "Extract RunPanel from Dashboard's RunDetailPanel for reuse in chat"
```

---

## Task 11: Frontend — `api.ts` chat methods and the real `Chat.tsx`

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/pages/Chat.tsx`
- Modify: `frontend/src/index.css`

**Interfaces:**
- Produces: `api.startChatSession(): Promise<{ session_id: string }>`, `api.sendChatMessage(sessionId: string, message: string): Promise<{ reply: string; run_id: string | null }>`.
- Produces: `export interface ChatMessage { role: "user" | "assistant"; content: string; run_id: string | null }` in `api.ts`.

- [ ] **Step 1: Add to `frontend/src/api.ts`**

Add the interface, near the other interfaces (after `VersionSummary`):

```ts
export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  run_id: string | null;
}
```

Add the two methods to the `api` object (after `deleteCapability`):

```ts
  startChatSession: () => post<{ session_id: string }>("/chat/sessions", {}),
  sendChatMessage: (sessionId: string, message: string) =>
    post<{ reply: string; run_id: string | null }>(`/chat/sessions/${sessionId}/message`, { message }),
```

- [ ] **Step 2: Replace `frontend/src/pages/Chat.tsx`**

```tsx
import { useState } from "react";
import { api, type ChatMessage } from "../api";
import { RunPanel } from "../components/RunPanel";

export function Chat() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function ensureSession(): Promise<string> {
    if (sessionId) return sessionId;
    const { session_id } = await api.startChatSession();
    setSessionId(session_id);
    return session_id;
  }

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    setInput("");
    setMessages((m) => [...m, { role: "user", content: text, run_id: null }]);
    try {
      const id = await ensureSession();
      const { reply, run_id } = await api.sendChatMessage(id, text);
      setMessages((m) => [...m, { role: "assistant", content: reply, run_id }]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="chat-page">
      <div className="chat-messages">
        {messages.length === 0 && <p className="muted">Tell me what you'd like to do.</p>}
        {messages.map((m, i) => (
          <div key={i} className={`chat-bubble chat-${m.role}`}>
            <p>{m.content}</p>
            {m.run_id && <RunPanel runId={m.run_id} />}
          </div>
        ))}
        {error && <p className="error">{error}</p>}
      </div>
      <div className="chat-input-row">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") send();
          }}
          placeholder="What would you like to do?"
        />
        <button disabled={busy} onClick={send}>
          {busy ? "…" : "Send"}
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Add chat page CSS to `frontend/src/index.css`**

Append at the end of the file:

```css
.chat-page { display: flex; flex-direction: column; height: 100%; min-height: 0; padding: 1rem; }
.chat-messages { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 0.75rem; }
.chat-bubble {
  max-width: 720px; padding: 0.6rem 0.85rem; border-radius: 10px; font-size: 0.9rem;
  border: 1px solid var(--border);
}
.chat-bubble p { margin: 0; white-space: pre-wrap; }
.chat-user { align-self: flex-end; background: var(--accent); color: white; border: none; }
.chat-assistant { align-self: flex-start; background: var(--panel); }
.chat-bubble .run-detail { margin-top: 0.6rem; }
.chat-input-row { display: flex; gap: 0.5rem; margin-top: 0.75rem; }
.chat-input-row input {
  flex: 1; background: var(--panel); border: 1px solid var(--border); color: var(--text);
  border-radius: 6px; padding: 0.55rem 0.75rem;
}
.chat-input-row button {
  background: var(--accent); border: none; color: white; padding: 0.55rem 1.1rem; border-radius: 6px;
}
```

- [ ] **Step 4: Type-check**

Run: `cd C:/Vijay/PyCode/comp-use/frontend && npx tsc --noEmit`
Expected: no output (clean)

- [ ] **Step 5: Manually verify in-browser**

With the backend and frontend both running: click "+ New workflow" (routes to Chat), send a message, confirm the target-site prompt appears, reply with a site (number or URL), ask for a task that matches an existing capability, confirm the masked-param confirmation text and the yes/no flow, reply "yes", and confirm an embedded `RunPanel` appears inline and updates live. Then try a request that matches nothing and confirm the propose_discovery confirmation text appears.

- [ ] **Step 6: Commit**

```bash
cd C:/Vijay/PyCode/comp-use
git add frontend/src/api.ts frontend/src/pages/Chat.tsx frontend/src/index.css
git commit -m "Implement the chat page: session lifecycle, confirm flow, embedded RunPanel"
```

---

## Task 12: End-to-end verification against the live MERIDIAN CORE target

Not a code task — confirms the whole chain works against the real target site, not just mocked tests.

- [ ] **Step 1:** Restart the backend with `COMP_USE_SANDBOX=1` and the real `ALLOWED_URL_PREFIXES=https://web-sample.interface-hiring.com`, and the frontend dev server.
- [ ] **Step 2:** In the browser, click "+ New workflow", pick `https://web-sample.interface-hiring.com` from the site list (it should appear as a known site from the existing MERIDIAN capabilities), and ask to check a member's balance in plain language (e.g. "what's the balance for member 100234").
- [ ] **Step 3:** Confirm the agent proposes invoking `meridian_check_balance` (or whichever capability matches) with `member_number: 100234`, confirm with "yes", and watch the embedded `RunPanel` run to completion with a structured result.
- [ ] **Step 4:** In the same session, ask for something no capability covers (e.g. "close member 100234's checking share") and confirm the agent proposes discovery instead, states the auto-slugified capability name, and — on "yes" — the embedded `RunPanel` shows a live discovery run against the sandbox with the noVNC feed visible.
- [ ] **Step 5:** Trigger an escalation from the chat-started run (e.g. take control via the embedded panel's "Take control" button, or let a risky step pause), and confirm the escalation box and interactive feed render correctly inline in the chat thread, matching the dashboard's existing behavior.
- [ ] **Step 6:** No commit for this task — it's verification only. If any step surfaces a bug, fix it as a small follow-up commit referencing which verification step it fixes.
