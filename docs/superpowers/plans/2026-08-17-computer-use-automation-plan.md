# Computer-Use Automation System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a working computer-use automation system: an LLM-driven discovery
agent that operates a mock legacy bank web app, records successful runs as versioned
capability artifacts, and a deterministic replay engine (no LLM) that executes those
artifacts with typed inputs/outputs, guardrails, and human escalation.

**Architecture:** Single Python process, five components behind clean interfaces
(Surface, Guardrail, LLMClient, ControlTransport) plus a Flask mock app as the target.
Discovery and replay share the Surface/Guardrail/Escalation/Evidence layers; only
discovery talks to an LLM.

**Tech Stack:** Python 3.11+, Flask (mock app), Playwright (browser automation),
Pydantic v2 (schemas/config), pytest (tests), `requests` (OpenRouter HTTP calls).

**Spec:** [docs/superpowers/specs/2026-08-17-computer-use-automation-design.md](../specs/2026-08-17-computer-use-automation-design.md)

## Global Constraints

- Python 3.11+, all new code under `comp_use/` (system) and `mock_app/` (target app).
- Pydantic v2 syntax (`model_config`, not `class Config`).
- No LLM calls anywhere in the replay path (spec §6) — enforced by construction:
  `ReplayEngine` must never import `LLMClient`.
- Locators are role/text-first with a recorded CSS fallback (spec §5) — never CSS-only.
- Every step/action carries a `risk_tier` of `safe` or `risky` (spec §8).
- Redaction (spec §8) uses one shared function at both the LLM-call boundary and the
  persistence boundary — never two divergent implementations.
- Target app allowlist defaults to `http://localhost:5000` only (spec §8).
- Artifacts are versioned JSON on disk under `/artifacts/<capability_name>/v<N>.json`
  (spec §3, §5). Evidence is JSONL + screenshots under `/evidence/<run_id>/` (spec §3).

---

## Task 1: Project scaffolding & config

**Files:**
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `comp_use/__init__.py`
- Create: `comp_use/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `comp_use.config.Settings` (Pydantic model) with fields
  `allowed_url_prefixes: list[str]`, `allowed_action_types: list[str]`,
  `redaction_patterns: list[str]`, `risky_confirm_default: bool`,
  `openrouter_api_key: str`, `openrouter_model: str`, `max_discovery_steps: int`,
  `artifacts_dir: Path`, `evidence_dir: Path`.
  `comp_use.config.load_settings() -> Settings` (reads env vars, applies defaults).

- [ ] **Step 1: Create the Python project files**

`requirements.txt`:
```
flask==3.0.3
playwright==1.47.0
pydantic==2.9.2
requests==2.32.3
pytest==8.3.3
python-dotenv==1.0.1
```

`pyproject.toml`:
```toml
[project]
name = "comp-use"
version = "0.1.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`.env.example`:
```
OPENROUTER_API_KEY=
OPENROUTER_MODEL=meta-llama/llama-3.1-8b-instruct:free
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_config.py
import os
from comp_use.config import load_settings


def test_defaults(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = load_settings()
    assert settings.allowed_url_prefixes == ["http://localhost:5000"]
    assert "navigate" in settings.allowed_action_types
    assert settings.max_discovery_steps == 25
    assert settings.artifacts_dir.name == "artifacts"
    assert settings.evidence_dir.name == "evidence"


def test_env_override(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "some/model:free")
    settings = load_settings()
    assert settings.openrouter_api_key == "test-key"
    assert settings.openrouter_model == "some/model:free"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'comp_use'`

- [ ] **Step 4: Write minimal implementation**

```python
# comp_use/__init__.py
```

```python
# comp_use/config.py
import os
from pathlib import Path

from pydantic import BaseModel, Field


class Settings(BaseModel):
    allowed_url_prefixes: list[str] = Field(
        default_factory=lambda: ["http://localhost:5000"]
    )
    allowed_action_types: list[str] = Field(
        default_factory=lambda: [
            "navigate", "click", "type_text", "select_option", "extract",
        ]
    )
    redaction_patterns: list[str] = Field(
        default_factory=lambda: [
            r"\b\d{9,12}\b",
            r"\$[\d,]+\.\d{2}",
        ]
    )
    risky_confirm_default: bool = False
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-3.1-8b-instruct:free"
    max_discovery_steps: int = 25
    artifacts_dir: Path = Path("artifacts")
    evidence_dir: Path = Path("evidence")


def load_settings() -> Settings:
    return Settings(
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        openrouter_model=os.environ.get(
            "OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free"
        ),
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Install dependencies and Playwright browsers**

Run: `pip install -r requirements.txt && playwright install chromium`

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml requirements.txt .env.example comp_use/ tests/test_config.py
git commit -m "Add project scaffolding and settings"
```

---

## Task 2: Core schemas

**Files:**
- Create: `comp_use/schemas.py`
- Test: `tests/test_schemas.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces: `RiskTier`, `LocatorStrategy`, `Locator`, `ValueSource`, `CheckpointType`,
  `Checkpoint`, `ActionType`, `Step`, `InputParam`, `OutputParam`, `Artifact`,
  `OutcomeType`, `ReplayResult`, `InterventionRequest` — all Pydantic models/enums, used
  by every later task.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schemas.py
import pytest
from pydantic import ValidationError

from comp_use.schemas import (
    Artifact, ActionType, Checkpoint, CheckpointType, InputParam, Locator,
    LocatorStrategy, OutcomeType, OutputParam, ReplayResult, RiskTier, Step,
    ValueSource,
)


def test_artifact_round_trips_through_json():
    artifact = Artifact(
        capability_name="open_sub_account",
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        description="Opens a new sub-account for an existing member.",
        input_schema=[
            InputParam(name="member_id", type="string", example="12345"),
        ],
        output_schema=[
            OutputParam(name="sub_account_id", type="string"),
        ],
        steps=[
            Step(
                action=ActionType.NAVIGATE,
                target="/member/search",
                risk_tier=RiskTier.SAFE,
            ),
            Step(
                action=ActionType.TYPE_TEXT,
                locator=Locator(
                    strategy=LocatorStrategy.ROLE,
                    value={"role": "textbox", "name": "Member ID"},
                ),
                value_source=ValueSource(
                    type="goal_parameter", param_name="member_id", param_type="string"
                ),
                risk_tier=RiskTier.SAFE,
            ),
        ],
        success_checkpoint=Checkpoint(
            type=CheckpointType.ELEMENT_VISIBLE,
            locator=Locator(
                strategy=LocatorStrategy.ROLE,
                value={"role": "heading", "name": "Confirmation"},
            ),
        ),
        created_from_run_id="run_abc123",
    )
    dumped = artifact.model_dump_json()
    restored = Artifact.model_validate_json(dumped)
    assert restored.capability_name == "open_sub_account"
    assert restored.steps[1].value_source.param_name == "member_id"


def test_step_requires_risk_tier_default_safe():
    step = Step(action=ActionType.CLICK, locator=Locator(
        strategy=LocatorStrategy.TEXT, value={"text": "Search"}
    ))
    assert step.risk_tier == RiskTier.SAFE


def test_replay_result_outcome_enum_rejects_invalid():
    with pytest.raises(ValidationError):
        ReplayResult(outcome="not_a_real_outcome")


def test_replay_result_valid_outcome():
    result = ReplayResult(outcome=OutcomeType.SUCCESS, outputs={"sub_account_id": "9"})
    assert result.outcome == OutcomeType.SUCCESS
    assert result.outputs["sub_account_id"] == "9"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'comp_use.schemas'`

- [ ] **Step 3: Write the implementation**

```python
# comp_use/schemas.py
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class RiskTier(str, Enum):
    SAFE = "safe"
    RISKY = "risky"


class LocatorStrategy(str, Enum):
    ROLE = "role"
    TEXT = "text"
    CSS = "css"


class Locator(BaseModel):
    strategy: LocatorStrategy
    value: dict[str, Any]
    fallback: Locator | None = None


class ValueSource(BaseModel):
    type: Literal["goal_parameter", "fixed"]
    param_name: str | None = None
    param_type: str | None = None
    reason: str | None = None


class CheckpointType(str, Enum):
    ELEMENT_VISIBLE = "element_visible"
    TEXT_PRESENT = "text_present"
    URL_MATCHES = "url_matches"


class Checkpoint(BaseModel):
    type: CheckpointType
    locator: Locator | None = None
    text: str | None = None
    url_pattern: str | None = None


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE_TEXT = "type_text"
    SELECT_OPTION = "select_option"
    EXTRACT = "extract"


class Step(BaseModel):
    action: ActionType
    target: str | None = None
    locator: Locator | None = None
    value_source: ValueSource | None = None
    extract_as: str | None = None
    risk_tier: RiskTier = RiskTier.SAFE
    checkpoint: Checkpoint | None = None


class InputParam(BaseModel):
    name: str
    type: Literal["string", "number", "boolean"]
    required: bool = True
    description: str = ""
    example: Any = None


class OutputParam(BaseModel):
    name: str
    type: Literal["string", "number", "boolean"]
    description: str = ""


class Artifact(BaseModel):
    capability_name: str
    version: int = 1
    target: dict[str, str]
    description: str = ""
    input_schema: list[InputParam] = Field(default_factory=list)
    output_schema: list[OutputParam] = Field(default_factory=list)
    steps: list[Step]
    success_checkpoint: Checkpoint
    created_from_run_id: str


class OutcomeType(str, Enum):
    VALIDATION_ERROR = "validation_error"
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"


class ReplayResult(BaseModel):
    outcome: OutcomeType
    outputs: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""
    step_index: int | None = None
    expected: str | None = None
    observed: str | None = None


class InterventionRequest(BaseModel):
    run_id: str
    capability_or_goal: str
    current_step: int | None = None
    screenshot_path: str | None = None
    reason: str
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_schemas.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add comp_use/schemas.py tests/test_schemas.py
git commit -m "Add artifact, step, and replay result schemas"
```

---

## Task 3: Mock app — member data + search/detail (read flow)

**Files:**
- Create: `mock_app/__init__.py`
- Create: `mock_app/data.py`
- Create: `mock_app/app.py`
- Create: `mock_app/templates/base.html`
- Create: `mock_app/templates/search.html`
- Create: `mock_app/templates/detail.html`
- Test: `tests/test_mock_app_read.py`

**Interfaces:**
- Produces: `mock_app.data.MEMBERS: dict[str, dict]` (in-memory store, keyed by member
  id, each value has `name`, `accounts: list[dict]` where each account has `id`,
  `type`, `balance`). `mock_app.app.create_app() -> Flask`.
- Routes: `GET /member/search` (search form), `GET /member/search?member_id=<id>`
  (redirects to detail or shows "not found"), `GET /member/<member_id>` (detail page).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mock_app_read.py
from mock_app.app import create_app


def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_search_form_renders():
    c = client()
    resp = c.get("/member/search")
    assert resp.status_code == 200
    assert b"Member ID" in resp.data


def test_search_known_member_redirects_to_detail():
    c = client()
    resp = c.get("/member/search", query_string={"member_id": "12345"})
    assert resp.status_code == 302
    assert "/member/12345" in resp.headers["Location"]


def test_search_unknown_member_shows_not_found():
    c = client()
    resp = c.get("/member/search", query_string={"member_id": "99999"})
    assert resp.status_code == 200
    assert b"No member found" in resp.data


def test_detail_page_shows_name_and_balance():
    c = client()
    resp = c.get("/member/12345")
    assert resp.status_code == 200
    assert b"Jane Doe" in resp.data
    assert b"1500.00" in resp.data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mock_app_read.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mock_app'`

- [ ] **Step 3: Write the implementation**

```python
# mock_app/__init__.py
```

```python
# mock_app/data.py
MEMBERS: dict[str, dict] = {
    "12345": {
        "name": "Jane Doe",
        "accounts": [
            {"id": "ACC-001", "type": "Checking", "balance": 1500.00},
            {"id": "ACC-002", "type": "Savings", "balance": 8200.50},
        ],
    },
    "67890": {
        "name": "John Smith",
        "accounts": [
            {"id": "ACC-101", "type": "Checking", "balance": 320.75},
        ],
    },
}
```

```html
<!-- mock_app/templates/base.html -->
<!doctype html>
<html>
<head><title>{% block title %}Legacy Bank Terminal{% endblock %}</title></head>
<body>
<table border="1" width="100%"><tr><td>
{% block content %}{% endblock %}
</td></tr></table>
</body>
</html>
```

```html
<!-- mock_app/templates/search.html -->
{% extends "base.html" %}
{% block content %}
<form method="get" action="/member/search">
<table>
<tr><td>Member ID</td><td><input type="text" name="member_id"></td></tr>
<tr><td colspan="2"><input type="submit" value="Search"></td></tr>
</table>
</form>
{% if not_found %}<p>No member found with that ID.</p>{% endif %}
{% endblock %}
```

```html
<!-- mock_app/templates/detail.html -->
{% extends "base.html" %}
{% block content %}
<h1>Member Detail</h1>
<table>
<tr><td>Name</td><td>{{ member.name }}</td></tr>
</table>
<table border="1">
<tr><th>Account</th><th>Type</th><th>Balance</th></tr>
{% for acc in member.accounts %}
<tr><td>{{ acc.id }}</td><td>{{ acc.type }}</td><td>{{ "%.2f"|format(acc.balance) }}</td></tr>
{% endfor %}
</table>
<p><a href="/member/{{ member_id }}/sub-account/new">Open Sub-Account</a></p>
<p><a href="/member/{{ member_id }}/transfer">Transfer Funds</a></p>
{% endblock %}
```

```python
# mock_app/app.py
from flask import Flask, redirect, render_template, request, url_for

from mock_app.data import MEMBERS


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/member/search")
    def member_search():
        member_id = request.args.get("member_id")
        if member_id is None:
            return render_template("search.html")
        if member_id not in MEMBERS:
            return render_template("search.html", not_found=True)
        return redirect(url_for("member_detail", member_id=member_id))

    @app.get("/member/<member_id>")
    def member_detail(member_id: str):
        member = MEMBERS[member_id]
        return render_template("detail.html", member=member, member_id=member_id)

    return app
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mock_app_read.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add mock_app/ tests/test_mock_app_read.py
git commit -m "Add mock bank app: member search and detail"
```

---

## Task 4: Mock app — open sub-account flow (write, moderate risk)

**Files:**
- Modify: `mock_app/app.py`
- Modify: `mock_app/data.py`
- Create: `mock_app/templates/new_sub_account.html`
- Create: `mock_app/templates/sub_account_confirmation.html`
- Test: `tests/test_mock_app_sub_account.py`

**Interfaces:**
- Produces: `mock_app.data.next_sub_account_id() -> str` (counter-based ID generator).
- Routes: `GET /member/<member_id>/sub-account/new` (form), `POST` to same route
  (creates account, validates `deposit_amount` > 0, re-renders form with error on
  invalid input, else redirects to `/member/<member_id>/sub-account/<new_id>/confirm`).
  `GET /member/<member_id>/sub-account/<sub_account_id>/confirm` (confirmation page
  showing `sub_account_id` and a `confirmation_number`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mock_app_sub_account.py
from mock_app.app import create_app


def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_new_sub_account_form_renders():
    c = client()
    resp = c.get("/member/12345/sub-account/new")
    assert resp.status_code == 200
    assert b"Deposit Amount" in resp.data


def test_invalid_deposit_amount_shows_validation_error():
    c = client()
    resp = c.post(
        "/member/12345/sub-account/new",
        data={"account_type": "Savings", "deposit_amount": "-5"},
    )
    assert resp.status_code == 200
    assert b"Deposit amount must be greater than zero" in resp.data


def test_valid_submission_redirects_to_confirmation():
    c = client()
    resp = c.post(
        "/member/12345/sub-account/new",
        data={"account_type": "Savings", "deposit_amount": "500"},
    )
    assert resp.status_code == 302
    assert "/confirm" in resp.headers["Location"]


def test_confirmation_page_shows_confirmation_number():
    c = client()
    post_resp = c.post(
        "/member/12345/sub-account/new",
        data={"account_type": "Savings", "deposit_amount": "500"},
        follow_redirects=True,
    )
    assert b"Confirmation Number" in post_resp.data
    assert b"500.00" in post_resp.data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mock_app_sub_account.py -v`
Expected: FAIL with `404 NOT FOUND` assertion errors (routes don't exist yet)

- [ ] **Step 3: Write the implementation**

```python
# mock_app/data.py — append to existing file
_sub_account_counter = {"n": 0}
_confirmation_counter = {"n": 0}


def next_sub_account_id() -> str:
    _sub_account_counter["n"] += 1
    return f"SUB-{_sub_account_counter['n']:04d}"


def next_confirmation_number() -> str:
    _confirmation_counter["n"] += 1
    return f"CONF-{_confirmation_counter['n']:06d}"
```

```html
<!-- mock_app/templates/new_sub_account.html -->
{% extends "base.html" %}
{% block content %}
<h1>Open Sub-Account</h1>
<form method="post" action="/member/{{ member_id }}/sub-account/new">
<table>
<tr><td>Account Type</td><td>
  <select name="account_type">
    <option value="Savings">Savings</option>
    <option value="Checking">Checking</option>
  </select>
</td></tr>
<tr><td>Deposit Amount</td><td><input type="text" name="deposit_amount"></td></tr>
<tr><td colspan="2"><input type="submit" value="Open Account"></td></tr>
</table>
</form>
{% if error %}<p>{{ error }}</p>{% endif %}
{% endblock %}
```

```html
<!-- mock_app/templates/sub_account_confirmation.html -->
{% extends "base.html" %}
{% block content %}
<h1>Confirmation</h1>
<table>
<tr><td>Sub-Account ID</td><td>{{ sub_account_id }}</td></tr>
<tr><td>Confirmation Number</td><td>{{ confirmation_number }}</td></tr>
<tr><td>Deposit Amount</td><td>{{ "%.2f"|format(deposit_amount) }}</td></tr>
</table>
{% endblock %}
```

```python
# mock_app/app.py — add inside create_app(), before "return app"
from mock_app.data import next_confirmation_number, next_sub_account_id

_confirmations: dict[str, dict] = {}

@app.get("/member/<member_id>/sub-account/new")
def new_sub_account_form(member_id: str):
    return render_template("new_sub_account.html", member_id=member_id)

@app.post("/member/<member_id>/sub-account/new")
def new_sub_account_submit(member_id: str):
    account_type = request.form.get("account_type", "Savings")
    try:
        deposit_amount = float(request.form.get("deposit_amount", "0"))
    except ValueError:
        deposit_amount = -1
    if deposit_amount <= 0:
        return render_template(
            "new_sub_account.html",
            member_id=member_id,
            error="Deposit amount must be greater than zero.",
        )
    sub_account_id = next_sub_account_id()
    member = MEMBERS[member_id]
    member["accounts"].append(
        {"id": sub_account_id, "type": account_type, "balance": deposit_amount}
    )
    confirmation_number = next_confirmation_number()
    _confirmations[sub_account_id] = {
        "confirmation_number": confirmation_number,
        "deposit_amount": deposit_amount,
    }
    return redirect(
        url_for(
            "sub_account_confirmation",
            member_id=member_id,
            sub_account_id=sub_account_id,
        )
    )

@app.get("/member/<member_id>/sub-account/<sub_account_id>/confirm")
def sub_account_confirmation(member_id: str, sub_account_id: str):
    info = _confirmations[sub_account_id]
    return render_template(
        "sub_account_confirmation.html",
        sub_account_id=sub_account_id,
        confirmation_number=info["confirmation_number"],
        deposit_amount=info["deposit_amount"],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mock_app_sub_account.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add mock_app/ tests/test_mock_app_sub_account.py
git commit -m "Add mock bank app: open sub-account flow"
```

---

## Task 5: Mock app — funds transfer flow (write, risky, business outcomes)

**Files:**
- Modify: `mock_app/app.py`
- Create: `mock_app/templates/transfer.html`
- Create: `mock_app/templates/transfer_confirmation.html`
- Test: `tests/test_mock_app_transfer.py`

**Interfaces:**
- Routes: `GET /member/<member_id>/transfer` (form: `from_account`, `to_account`,
  `amount`), `POST` same route — three outcomes: validation error (amount <= 0 or
  missing account, re-render form with `error`), business outcome (amount exceeds
  `from_account` balance, re-render form with `insufficient_funds=True`), success
  (redirect to `/member/<member_id>/transfer/<txn_id>/confirm`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mock_app_transfer.py
from mock_app.app import create_app


def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_transfer_form_renders():
    c = client()
    resp = c.get("/member/12345/transfer")
    assert resp.status_code == 200
    assert b"From Account" in resp.data


def test_transfer_missing_amount_is_validation_error():
    c = client()
    resp = c.post(
        "/member/12345/transfer",
        data={"from_account": "ACC-001", "to_account": "ACC-002", "amount": "0"},
    )
    assert resp.status_code == 200
    assert b"Amount must be greater than zero" in resp.data


def test_transfer_exceeding_balance_is_business_outcome():
    c = client()
    resp = c.post(
        "/member/12345/transfer",
        data={"from_account": "ACC-001", "to_account": "ACC-002", "amount": "999999"},
    )
    assert resp.status_code == 200
    assert b"Insufficient funds" in resp.data


def test_valid_transfer_redirects_to_confirmation():
    c = client()
    resp = c.post(
        "/member/12345/transfer",
        data={"from_account": "ACC-001", "to_account": "ACC-002", "amount": "100"},
    )
    assert resp.status_code == 302
    assert "/confirm" in resp.headers["Location"]


def test_transfer_confirmation_shows_txn_id():
    c = client()
    resp = c.post(
        "/member/12345/transfer",
        data={"from_account": "ACC-001", "to_account": "ACC-002", "amount": "100"},
        follow_redirects=True,
    )
    assert b"Transaction ID" in resp.data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mock_app_transfer.py -v`
Expected: FAIL with `404 NOT FOUND` assertion errors

- [ ] **Step 3: Write the implementation**

```python
# mock_app/data.py — append to existing file
_txn_counter = {"n": 0}


def next_txn_id() -> str:
    _txn_counter["n"] += 1
    return f"TXN-{_txn_counter['n']:06d}"
```

```html
<!-- mock_app/templates/transfer.html -->
{% extends "base.html" %}
{% block content %}
<h1>Transfer Funds</h1>
<form method="post" action="/member/{{ member_id }}/transfer">
<table>
<tr><td>From Account</td><td><input type="text" name="from_account"></td></tr>
<tr><td>To Account</td><td><input type="text" name="to_account"></td></tr>
<tr><td>Amount</td><td><input type="text" name="amount"></td></tr>
<tr><td colspan="2"><input type="submit" value="Transfer"></td></tr>
</table>
</form>
{% if error %}<p>{{ error }}</p>{% endif %}
{% if insufficient_funds %}<p>Insufficient funds for this transfer.</p>{% endif %}
{% endblock %}
```

```html
<!-- mock_app/templates/transfer_confirmation.html -->
{% extends "base.html" %}
{% block content %}
<h1>Confirmation</h1>
<table>
<tr><td>Transaction ID</td><td>{{ txn_id }}</td></tr>
<tr><td>Amount</td><td>{{ "%.2f"|format(amount) }}</td></tr>
</table>
{% endblock %}
```

```python
# mock_app/app.py — add inside create_app(), before "return app"
from mock_app.data import next_txn_id

_transfers: dict[str, dict] = {}

def _find_account(member_id: str, account_id: str) -> dict | None:
    for acc in MEMBERS[member_id]["accounts"]:
        if acc["id"] == account_id:
            return acc
    return None

@app.get("/member/<member_id>/transfer")
def transfer_form(member_id: str):
    return render_template("transfer.html", member_id=member_id)

@app.post("/member/<member_id>/transfer")
def transfer_submit(member_id: str):
    from_account_id = request.form.get("from_account", "")
    to_account_id = request.form.get("to_account", "")
    try:
        amount = float(request.form.get("amount", "0"))
    except ValueError:
        amount = -1

    from_account = _find_account(member_id, from_account_id)
    if amount <= 0 or from_account is None:
        return render_template(
            "transfer.html",
            member_id=member_id,
            error="Amount must be greater than zero and accounts must be valid.",
        )
    if amount > from_account["balance"]:
        return render_template(
            "transfer.html", member_id=member_id, insufficient_funds=True
        )

    from_account["balance"] -= amount
    to_account = _find_account(member_id, to_account_id)
    if to_account is not None:
        to_account["balance"] += amount

    txn_id = next_txn_id()
    _transfers[txn_id] = {"amount": amount}
    return redirect(url_for("transfer_confirmation", member_id=member_id, txn_id=txn_id))

@app.get("/member/<member_id>/transfer/<txn_id>/confirm")
def transfer_confirmation(member_id: str, txn_id: str):
    info = _transfers[txn_id]
    return render_template("transfer_confirmation.html", txn_id=txn_id, amount=info["amount"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mock_app_transfer.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add mock_app/ tests/test_mock_app_transfer.py
git commit -m "Add mock bank app: funds transfer flow"
```

---

## Task 6: Guardrail layer

**Files:**
- Create: `comp_use/guardrail.py`
- Test: `tests/test_guardrail.py`

**Interfaces:**
- Consumes: `comp_use.config.Settings` (Task 1), `comp_use.schemas.Step`, `RiskTier`
  (Task 2).
- Produces: `comp_use.guardrail.AllowlistViolation(Exception)`,
  `comp_use.guardrail.Guardrail(settings: Settings)` with methods
  `check_allowlist(self, url: str, action_type: str) -> None` (raises on violation),
  `requires_confirmation(self, step: Step, confirm_risky: bool) -> bool`,
  `redact(self, text: str) -> str`. Used by Surface (Task 8), discovery agent (Task
  10), and replay engine (Task 11).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_guardrail.py
import pytest

from comp_use.config import load_settings
from comp_use.guardrail import AllowlistViolation, Guardrail
from comp_use.schemas import Locator, LocatorStrategy, RiskTier, Step


def make_guardrail() -> Guardrail:
    return Guardrail(load_settings())


def test_allowed_url_and_action_pass():
    g = make_guardrail()
    g.check_allowlist("http://localhost:5000/member/12345", "navigate")


def test_disallowed_domain_raises():
    g = make_guardrail()
    with pytest.raises(AllowlistViolation):
        g.check_allowlist("http://evil.example.com/x", "navigate")


def test_disallowed_action_type_raises():
    g = make_guardrail()
    with pytest.raises(AllowlistViolation):
        g.check_allowlist("http://localhost:5000/x", "delete_everything")


def test_risky_step_requires_confirmation_by_default():
    g = make_guardrail()
    step = Step(
        action="click",
        locator=Locator(strategy=LocatorStrategy.TEXT, value={"text": "Transfer"}),
        risk_tier=RiskTier.RISKY,
    )
    assert g.requires_confirmation(step, confirm_risky=False) is True
    assert g.requires_confirmation(step, confirm_risky=True) is False


def test_safe_step_never_requires_confirmation():
    g = make_guardrail()
    step = Step(
        action="click",
        locator=Locator(strategy=LocatorStrategy.TEXT, value={"text": "Search"}),
        risk_tier=RiskTier.SAFE,
    )
    assert g.requires_confirmation(step, confirm_risky=False) is False


def test_redact_masks_account_numbers_and_amounts():
    g = make_guardrail()
    redacted = g.redact("Member 123456789 has balance $1,500.00 today")
    assert "123456789" not in redacted
    assert "$1,500.00" not in redacted
    assert "[REDACTED]" in redacted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_guardrail.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'comp_use.guardrail'`

- [ ] **Step 3: Write the implementation**

```python
# comp_use/guardrail.py
import re

from comp_use.config import Settings
from comp_use.schemas import RiskTier, Step


class AllowlistViolation(Exception):
    pass


class Guardrail:
    def __init__(self, settings: Settings):
        self.settings = settings

    def check_allowlist(self, url: str, action_type: str) -> None:
        if action_type not in self.settings.allowed_action_types:
            raise AllowlistViolation(f"action type '{action_type}' not in allowlist")
        if not any(
            url.startswith(prefix) for prefix in self.settings.allowed_url_prefixes
        ):
            raise AllowlistViolation(f"url '{url}' not in allowlist")

    def requires_confirmation(self, step: Step, confirm_risky: bool) -> bool:
        return step.risk_tier == RiskTier.RISKY and not confirm_risky

    def redact(self, text: str) -> str:
        redacted = text
        for pattern in self.settings.redaction_patterns:
            redacted = re.sub(pattern, "[REDACTED]", redacted)
        return redacted
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_guardrail.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add comp_use/guardrail.py tests/test_guardrail.py
git commit -m "Add guardrail layer: allowlist, risk confirmation, redaction"
```

---

## Task 7: Evidence logger

**Files:**
- Create: `comp_use/evidence.py`
- Test: `tests/test_evidence.py`

**Interfaces:**
- Consumes: `comp_use.config.Settings` (Task 1), `comp_use.guardrail.Guardrail`
  (Task 6).
- Produces: `comp_use.evidence.EvidenceLogger(settings: Settings, guardrail:
  Guardrail, run_id: str)` with `log_event(self, event_type: str, data: dict) -> None`
  (redacts string values in `data` before appending a JSON line to
  `evidence/<run_id>/log.jsonl`), `save_screenshot(self, png_bytes: bytes, label:
  str) -> str` (writes to `evidence/<run_id>/<label>.png`, returns the path as `str`).
  Used by discovery agent (Task 10), replay engine (Task 11), escalation (Task 12).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_evidence.py
import json

from comp_use.config import load_settings
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail


def test_log_event_writes_redacted_jsonl(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    logger = EvidenceLogger(settings, Guardrail(settings), run_id="run_001")

    logger.log_event("action", {"detail": "typed $1,500.00 into field"})

    log_path = tmp_path / "evidence" / "run_001" / "log.jsonl"
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event_type"] == "action"
    assert "$1,500.00" not in record["data"]["detail"]
    assert "[REDACTED]" in record["data"]["detail"]


def test_save_screenshot_writes_file_and_returns_path(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    logger = EvidenceLogger(settings, Guardrail(settings), run_id="run_002")

    path = logger.save_screenshot(b"\x89PNG\r\n", label="step_1")

    assert path.endswith("step_1.png")
    assert (tmp_path / "evidence" / "run_002" / "step_1.png").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_evidence.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'comp_use.evidence'`

- [ ] **Step 3: Write the implementation**

```python
# comp_use/evidence.py
import json
from pathlib import Path

from comp_use.config import Settings
from comp_use.guardrail import Guardrail


def _redact_value(guardrail: Guardrail, value):
    if isinstance(value, str):
        return guardrail.redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(guardrail, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(guardrail, v) for v in value]
    return value


class EvidenceLogger:
    def __init__(self, settings: Settings, guardrail: Guardrail, run_id: str):
        self.settings = settings
        self.guardrail = guardrail
        self.run_id = run_id
        self.run_dir = Path(settings.evidence_dir) / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def log_event(self, event_type: str, data: dict) -> None:
        record = {"event_type": event_type, "data": _redact_value(self.guardrail, data)}
        log_path = self.run_dir / "log.jsonl"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def save_screenshot(self, png_bytes: bytes, label: str) -> str:
        path = self.run_dir / f"{label}.png"
        path.write_bytes(png_bytes)
        return str(path)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_evidence.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add comp_use/evidence.py tests/test_evidence.py
git commit -m "Add evidence logger with redacted JSONL and screenshots"
```

---

## Task 8: Surface abstraction (Playwright wrapper)

**Files:**
- Create: `comp_use/surface.py`
- Test: `tests/test_surface.py`

**Interfaces:**
- Consumes: `comp_use.schemas.Locator`, `LocatorStrategy`, `Checkpoint`,
  `CheckpointType`, `ActionType` (Task 2), a running `mock_app` (Task 3-5) served over
  HTTP for tests.
- Produces: `comp_use.surface.Surface` (abstract base) with `observe(self) ->
  ObservedState` and `act(self, action: ActionType, locator: Locator | None, target:
  str | None, text: str | None) -> None` and `check_checkpoint(self, checkpoint:
  Checkpoint) -> bool` and `screenshot(self) -> bytes` and `current_url(self) -> str`.
  `comp_use.surface.ObservedState` (dataclass: `accessibility_tree: str`,
  `url: str`). `comp_use.surface.PlaywrightSurface(page)` implementing `Surface`
  against a Playwright `Page`. Used by discovery agent (Task 10) and replay engine
  (Task 11).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_surface.py
import threading
import time

import pytest
from playwright.sync_api import sync_playwright

from comp_use.schemas import ActionType, Checkpoint, CheckpointType, Locator, LocatorStrategy
from comp_use.surface import PlaywrightSurface
from mock_app.app import create_app


@pytest.fixture(scope="module")
def live_server():
    app = create_app()
    server = threading.Thread(
        target=lambda: app.run(port=5099, use_reloader=False), daemon=True
    )
    server.start()
    time.sleep(0.5)
    yield "http://localhost:5099"


@pytest.fixture
def surface(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        yield PlaywrightSurface(page)
        browser.close()


def test_observe_returns_accessibility_tree_text(surface, live_server):
    surface.act(ActionType.NAVIGATE, locator=None, target=f"{live_server}/member/search", text=None)
    state = surface.observe()
    assert "textbox" in state.accessibility_tree.lower()
    assert state.url.endswith("/member/search")


def test_act_type_text_and_click_navigates_to_detail(surface, live_server):
    surface.act(ActionType.NAVIGATE, locator=None, target=f"{live_server}/member/search", text=None)
    surface.act(
        ActionType.TYPE_TEXT,
        locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "Member ID"}),
        target=None,
        text="12345",
    )
    surface.act(
        ActionType.CLICK,
        locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Search"}),
        target=None,
        text=None,
    )
    assert "/member/12345" in surface.current_url()


def test_check_checkpoint_element_visible(surface, live_server):
    surface.act(ActionType.NAVIGATE, locator=None, target=f"{live_server}/member/12345", text=None)
    checkpoint = Checkpoint(
        type=CheckpointType.ELEMENT_VISIBLE,
        locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Member Detail"}),
    )
    assert surface.check_checkpoint(checkpoint) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_surface.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'comp_use.surface'`

- [ ] **Step 3: Write the implementation**

```python
# comp_use/surface.py
from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

from comp_use.schemas import ActionType, Checkpoint, CheckpointType, Locator, LocatorStrategy


@dataclass
class ObservedState:
    accessibility_tree: str
    url: str


def _resolve(page: Page, locator: Locator):
    if locator.strategy == LocatorStrategy.ROLE:
        role = locator.value["role"]
        name = locator.value.get("name")
        if name:
            return page.get_by_role(role, name=name)
        return page.get_by_role(role)
    if locator.strategy == LocatorStrategy.TEXT:
        return page.get_by_text(locator.value["text"])
    if locator.strategy == LocatorStrategy.CSS:
        return page.locator(locator.value["css"])
    raise ValueError(f"unknown locator strategy: {locator.strategy}")


class Surface:
    def observe(self) -> ObservedState:
        raise NotImplementedError

    def act(self, action: ActionType, locator: Locator | None, target: str | None, text: str | None) -> None:
        raise NotImplementedError

    def check_checkpoint(self, checkpoint: Checkpoint) -> bool:
        raise NotImplementedError

    def screenshot(self) -> bytes:
        raise NotImplementedError

    def current_url(self) -> str:
        raise NotImplementedError


class PlaywrightSurface(Surface):
    def __init__(self, page: Page):
        self.page = page

    def observe(self) -> ObservedState:
        snapshot = self.page.accessibility.snapshot()
        return ObservedState(accessibility_tree=str(snapshot), url=self.page.url)

    def act(self, action: ActionType, locator: Locator | None, target: str | None, text: str | None) -> None:
        if action == ActionType.NAVIGATE:
            self.page.goto(target)
        elif action == ActionType.CLICK:
            _resolve(self.page, locator).click()
        elif action == ActionType.TYPE_TEXT:
            _resolve(self.page, locator).fill(text)
        elif action == ActionType.SELECT_OPTION:
            _resolve(self.page, locator).select_option(text)
        elif action == ActionType.EXTRACT:
            pass
        else:
            raise ValueError(f"unknown action: {action}")

    def check_checkpoint(self, checkpoint: Checkpoint) -> bool:
        if checkpoint.type == CheckpointType.ELEMENT_VISIBLE:
            return _resolve(self.page, checkpoint.locator).is_visible()
        if checkpoint.type == CheckpointType.TEXT_PRESENT:
            return checkpoint.text in self.page.content()
        if checkpoint.type == CheckpointType.URL_MATCHES:
            return checkpoint.url_pattern in self.page.url
        raise ValueError(f"unknown checkpoint type: {checkpoint.type}")

    def screenshot(self) -> bytes:
        return self.page.screenshot()

    def current_url(self) -> str:
        return self.page.url
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_surface.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add comp_use/surface.py tests/test_surface.py
git commit -m "Add Playwright Surface: accessibility-tree observe, role-based act"
```

---

## Task 9: LLM client abstraction

**Files:**
- Create: `comp_use/llm_client.py`
- Test: `tests/test_llm_client.py`

**Interfaces:**
- Consumes: `comp_use.config.Settings` (Task 1).
- Produces: `comp_use.llm_client.LLMClient` (abstract) with `decide_next_action(self,
  goal: str, observed_tree: str, screenshot_b64: str | None, history: list[dict]) ->
  dict` (returns a tool-call dict: `{"action": str, "locator": dict | None, "target":
  str | None, "text": str | None, "value_source": dict | None, "done": bool}`).
  `comp_use.llm_client.OpenRouterClient(settings: Settings)` implementing `LLMClient`
  via HTTP POST to OpenRouter's chat-completions endpoint with a tool-call JSON
  schema. `comp_use.llm_client.FakeLLMClient(scripted_actions: list[dict])`
  implementing `LLMClient` by returning `scripted_actions` in order — used by
  discovery agent tests (Task 10) so they don't require a live API key.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_client.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_llm_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'comp_use.llm_client'`

- [ ] **Step 3: Write the implementation**

```python
# comp_use/llm_client.py
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
            {"role": "system", "content": "You control a web browser to accomplish a goal. Call decide_next_action with the next single action."},
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_llm_client.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add comp_use/llm_client.py tests/test_llm_client.py
git commit -m "Add LLM client abstraction: OpenRouter and fake for tests"
```

---

## Task 10: Discovery agent + artifact compiler

**Files:**
- Create: `comp_use/discovery/__init__.py`
- Create: `comp_use/discovery/agent.py`
- Create: `comp_use/discovery/compiler.py`
- Test: `tests/test_discovery_agent.py`
- Test: `tests/test_compiler.py`

**Interfaces:**
- Consumes: `comp_use.surface.Surface`, `ObservedState` (Task 8),
  `comp_use.llm_client.LLMClient` (Task 9), `comp_use.guardrail.Guardrail` (Task 6),
  `comp_use.evidence.EvidenceLogger` (Task 7), `comp_use.schemas.ActionType, Step,
  Locator, ValueSource, RiskTier` (Task 2).
- Produces: `comp_use.discovery.agent.RunTrace` (dataclass: `run_id: str`,
  `goal: str`, `steps: list[Step]`, `final_url: str`, `succeeded: bool`).
  `comp_use.discovery.agent.DiscoveryAgent(surface, llm_client, guardrail,
  evidence_logger, max_steps: int)` with `run(self, goal: str, start_url: str) ->
  RunTrace`. `comp_use.discovery.compiler.compile_artifact(trace: RunTrace,
  capability_name: str, target: dict, success_checkpoint: Checkpoint,
  output_schema: list[OutputParam]) -> Artifact` (Task 11 consumes `Artifact`).

- [ ] **Step 1: Write the failing test for the agent loop**

```python
# tests/test_discovery_agent.py
from comp_use.config import load_settings
from comp_use.discovery.agent import DiscoveryAgent
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.llm_client import FakeLLMClient
from comp_use.schemas import ActionType


class FakeSurface:
    def __init__(self):
        self.actions = []
        self.url = "http://localhost:5000/member/search"

    def act(self, action, locator, target, text):
        self.actions.append((action, locator, target, text))
        if action == ActionType.NAVIGATE:
            self.url = target

    def observe(self):
        from comp_use.surface import ObservedState
        return ObservedState(accessibility_tree="fake tree", url=self.url)

    def check_checkpoint(self, checkpoint):
        return True

    def screenshot(self):
        return b"fakepng"

    def current_url(self):
        return self.url


def test_agent_runs_until_finish_and_records_steps(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    surface = FakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "text", "value": {"text": "Search"}},
             "target": None, "text": None, "value_source": None, "done": False},
            {"action": "type_text", "locator": {"strategy": "role", "value": {"role": "textbox", "name": "Member ID"}},
             "target": None, "text": "12345", "value_source": {"type": "goal_parameter", "param_name": "member_id", "param_type": "string"}, "done": False},
            {"action": "finish", "locator": None, "target": None, "text": None, "value_source": None, "done": True},
        ]
    )
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="run_test")
    agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=10)

    trace = agent.run(goal="Look up member 12345", start_url="http://localhost:5000/member/search")

    assert trace.succeeded is True
    assert len(trace.steps) == 2
    assert trace.steps[1].value_source.param_name == "member_id"


def test_agent_stops_at_max_steps_without_finish():
    settings = load_settings()
    surface = FakeSurface()
    llm = FakeLLMClient(
        scripted_actions=[
            {"action": "click", "locator": {"strategy": "text", "value": {"text": "x"}},
             "target": None, "text": None, "value_source": None, "done": False},
        ] * 5
    )
    guardrail = Guardrail(settings)
    from comp_use.evidence import EvidenceLogger
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        settings.evidence_dir = d
        evidence = EvidenceLogger(settings, guardrail, run_id="run_test2")
        agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=3)
        trace = agent.run(goal="do something", start_url="http://localhost:5000/member/search")
        assert trace.succeeded is False
        assert len(trace.steps) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_discovery_agent.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'comp_use.discovery'`

- [ ] **Step 3: Write the agent implementation**

```python
# comp_use/discovery/__init__.py
```

```python
# comp_use/discovery/agent.py
from __future__ import annotations

from dataclasses import dataclass, field

from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.llm_client import LLMClient
from comp_use.schemas import ActionType, Locator, RiskTier, Step, ValueSource

_RISKY_ACTIONS = {ActionType.TYPE_TEXT: False}
_RISKY_TARGET_HINTS = ("transfer", "sub-account", "delete")


@dataclass
class RunTrace:
    run_id: str
    goal: str
    steps: list[Step] = field(default_factory=list)
    final_url: str = ""
    succeeded: bool = False


def _classify_risk(action: ActionType, target: str | None, locator) -> RiskTier:
    haystack = " ".join(filter(None, [target, str(locator.value) if locator else ""])).lower()
    if action in (ActionType.CLICK, ActionType.NAVIGATE) and any(h in haystack for h in _RISKY_TARGET_HINTS):
        return RiskTier.RISKY
    return RiskTier.SAFE


class DiscoveryAgent:
    def __init__(self, surface, llm_client: LLMClient, guardrail: Guardrail, evidence_logger: EvidenceLogger, max_steps: int):
        self.surface = surface
        self.llm_client = llm_client
        self.guardrail = guardrail
        self.evidence_logger = evidence_logger
        self.max_steps = max_steps

    def run(self, goal: str, start_url: str) -> RunTrace:
        trace = RunTrace(run_id=self.evidence_logger.run_id, goal=goal)
        self.surface.act(ActionType.NAVIGATE, locator=None, target=start_url, text=None)
        history: list[dict] = []

        for _ in range(self.max_steps):
            observed = self.surface.observe()
            decision = self.llm_client.decide_next_action(
                goal=goal, observed_tree=observed.accessibility_tree, screenshot_b64=None, history=history
            )
            self.evidence_logger.log_event("decision", decision)

            if decision.get("done"):
                trace.succeeded = True
                break

            action = ActionType(decision["action"])
            locator = Locator.model_validate(decision["locator"]) if decision.get("locator") else None
            target = decision.get("target")
            text = decision.get("text")
            value_source = ValueSource.model_validate(decision["value_source"]) if decision.get("value_source") else None

            self.guardrail.check_allowlist(target or self.surface.current_url(), action.value)
            risk_tier = _classify_risk(action, target, locator)

            self.surface.act(action, locator=locator, target=target, text=text)
            trace.steps.append(
                Step(action=action, target=target, locator=locator, value_source=value_source, risk_tier=risk_tier)
            )
            history.append(decision)

        trace.final_url = self.surface.current_url()
        return trace
```

- [ ] **Step 4: Run agent test to verify it passes**

Run: `pytest tests/test_discovery_agent.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Write the failing test for the compiler**

```python
# tests/test_compiler.py
from comp_use.discovery.agent import RunTrace
from comp_use.discovery.compiler import compile_artifact
from comp_use.schemas import ActionType, Checkpoint, CheckpointType, Locator, LocatorStrategy, RiskTier, Step, ValueSource


def test_compile_artifact_derives_input_schema_from_value_sources():
    trace = RunTrace(
        run_id="run_abc",
        goal="Open sub-account for member 12345",
        steps=[
            Step(action=ActionType.NAVIGATE, target="/member/search", risk_tier=RiskTier.SAFE),
            Step(
                action=ActionType.TYPE_TEXT,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "Member ID"}),
                value_source=ValueSource(type="goal_parameter", param_name="member_id", param_type="string"),
                risk_tier=RiskTier.SAFE,
            ),
            Step(
                action=ActionType.CLICK,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Search"}),
                risk_tier=RiskTier.SAFE,
            ),
        ],
        final_url="http://localhost:5000/member/12345",
        succeeded=True,
    )
    success_checkpoint = Checkpoint(
        type=CheckpointType.ELEMENT_VISIBLE,
        locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Member Detail"}),
    )

    artifact = compile_artifact(
        trace,
        capability_name="lookup_member",
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        success_checkpoint=success_checkpoint,
        output_schema=[],
    )

    assert artifact.capability_name == "lookup_member"
    assert len(artifact.input_schema) == 1
    assert artifact.input_schema[0].name == "member_id"
    assert artifact.input_schema[0].type == "string"
    assert artifact.created_from_run_id == "run_abc"
    assert len(artifact.steps) == 3
```

- [ ] **Step 6: Run compiler test to verify it fails**

Run: `pytest tests/test_compiler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'comp_use.discovery.compiler'`

- [ ] **Step 7: Write the compiler implementation**

```python
# comp_use/discovery/compiler.py
from comp_use.discovery.agent import RunTrace
from comp_use.schemas import Artifact, Checkpoint, InputParam, OutputParam


def compile_artifact(
    trace: RunTrace,
    capability_name: str,
    target: dict,
    success_checkpoint: Checkpoint,
    output_schema: list[OutputParam],
) -> Artifact:
    input_schema: list[InputParam] = []
    seen = set()
    for step in trace.steps:
        vs = step.value_source
        if vs is not None and vs.type == "goal_parameter" and vs.param_name not in seen:
            seen.add(vs.param_name)
            input_schema.append(
                InputParam(name=vs.param_name, type=vs.param_type or "string", required=True)
            )

    return Artifact(
        capability_name=capability_name,
        target=target,
        description=trace.goal,
        input_schema=input_schema,
        output_schema=output_schema,
        steps=trace.steps,
        success_checkpoint=success_checkpoint,
        created_from_run_id=trace.run_id,
    )
```

- [ ] **Step 8: Run compiler test to verify it passes**

Run: `pytest tests/test_compiler.py -v`
Expected: PASS (1 test)

- [ ] **Step 9: Commit**

```bash
git add comp_use/discovery/ tests/test_discovery_agent.py tests/test_compiler.py
git commit -m "Add discovery agent loop and artifact compiler"
```

---

## Task 11: Replay engine

**Files:**
- Create: `comp_use/replay/__init__.py`
- Create: `comp_use/replay/engine.py`
- Test: `tests/test_replay_engine.py`

**Interfaces:**
- Consumes: `comp_use.schemas.Artifact, Step, ActionType, OutcomeType, ReplayResult,
  RiskTier` (Task 2), `comp_use.surface.Surface` (Task 8),
  `comp_use.guardrail.Guardrail` (Task 6), `comp_use.evidence.EvidenceLogger`
  (Task 7).
- Produces: `comp_use.replay.engine.ReplayEngine(surface, guardrail, evidence_logger)`
  with `run(self, artifact: Artifact, params: dict, confirm_risky: bool = False) ->
  ReplayResult`. Must not import `comp_use.llm_client` (Global Constraint).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_replay_engine.py
import pytest

from comp_use.config import load_settings
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.replay.engine import ReplayEngine
from comp_use.schemas import (
    ActionType, Artifact, Checkpoint, CheckpointType, InputParam, Locator,
    LocatorStrategy, OutcomeType, RiskTier, Step, ValueSource,
)


class FakeSurface:
    def __init__(self, checkpoint_result=True, fail_on_step=None):
        self.acted = []
        self.checkpoint_result = checkpoint_result
        self.fail_on_step = fail_on_step
        self.url = "http://localhost:5000/member/search"

    def act(self, action, locator, target, text):
        self.acted.append((action, target, text))
        if action == ActionType.NAVIGATE:
            self.url = target

    def observe(self):
        from comp_use.surface import ObservedState
        return ObservedState(accessibility_tree="tree", url=self.url)

    def check_checkpoint(self, checkpoint):
        if self.fail_on_step is not None and len(self.acted) - 1 == self.fail_on_step:
            return False
        return self.checkpoint_result

    def screenshot(self):
        return b"png"

    def current_url(self):
        return self.url


def _make_artifact():
    return Artifact(
        capability_name="lookup_member",
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        input_schema=[InputParam(name="member_id", type="string", required=True)],
        steps=[
            Step(action=ActionType.NAVIGATE, target="http://localhost:5000/member/search", risk_tier=RiskTier.SAFE),
            Step(
                action=ActionType.TYPE_TEXT,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "Member ID"}),
                value_source=ValueSource(type="goal_parameter", param_name="member_id", param_type="string"),
                risk_tier=RiskTier.SAFE,
            ),
        ],
        success_checkpoint=Checkpoint(
            type=CheckpointType.ELEMENT_VISIBLE,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Member Detail"}),
        ),
        created_from_run_id="run_x",
    )


def _make_engine(surface, tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="replay_run")
    return ReplayEngine(surface, guardrail, evidence)


def test_missing_required_param_is_validation_error(tmp_path):
    engine = _make_engine(FakeSurface(), tmp_path)
    result = engine.run(_make_artifact(), params={})
    assert result.outcome == OutcomeType.VALIDATION_ERROR
    assert not FakeSurface().acted  # no browser interaction on validation failure


def test_successful_replay_returns_success(tmp_path):
    engine = _make_engine(FakeSurface(checkpoint_result=True), tmp_path)
    result = engine.run(_make_artifact(), params={"member_id": "12345"})
    assert result.outcome == OutcomeType.SUCCESS


def test_checkpoint_failure_returns_hard_failure_with_step_detail(tmp_path):
    surface = FakeSurface(fail_on_step=1)
    engine = _make_engine(surface, tmp_path)
    result = engine.run(_make_artifact(), params={"member_id": "12345"})
    assert result.outcome == OutcomeType.HARD_FAILURE
    assert result.step_index == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_replay_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'comp_use.replay'`

- [ ] **Step 3: Write the implementation**

```python
# comp_use/replay/__init__.py
```

```python
# comp_use/replay/engine.py
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.schemas import Artifact, OutcomeType, ReplayResult


class ReplayEngine:
    def __init__(self, surface, guardrail: Guardrail, evidence_logger: EvidenceLogger):
        self.surface = surface
        self.guardrail = guardrail
        self.evidence_logger = evidence_logger

    def _validate_params(self, artifact: Artifact, params: dict) -> str | None:
        for input_param in artifact.input_schema:
            if input_param.required and input_param.name not in params:
                return f"missing required param '{input_param.name}'"
        return None

    def run(self, artifact: Artifact, params: dict, confirm_risky: bool = False) -> ReplayResult:
        validation_error = self._validate_params(artifact, params)
        if validation_error:
            self.evidence_logger.log_event("validation_error", {"detail": validation_error})
            return ReplayResult(outcome=OutcomeType.VALIDATION_ERROR, detail=validation_error)

        for index, step in enumerate(artifact.steps):
            text = None
            if step.value_source is not None and step.value_source.type == "goal_parameter":
                text = str(params[step.value_source.param_name])

            target_url = step.target
            self.guardrail.check_allowlist(target_url or self.surface.current_url(), step.action.value)

            self.surface.act(step.action, locator=step.locator, target=target_url, text=text)
            self.evidence_logger.log_event("replay_step", {"index": index, "action": step.action.value})

            if step.checkpoint is not None and not self.surface.check_checkpoint(step.checkpoint):
                return ReplayResult(
                    outcome=OutcomeType.HARD_FAILURE,
                    step_index=index,
                    expected=str(step.checkpoint),
                    observed=self.surface.current_url(),
                )

        if not self.surface.check_checkpoint(artifact.success_checkpoint):
            return ReplayResult(
                outcome=OutcomeType.HARD_FAILURE,
                step_index=len(artifact.steps) - 1,
                expected=str(artifact.success_checkpoint),
                observed=self.surface.current_url(),
            )

        return ReplayResult(outcome=OutcomeType.SUCCESS, outputs={})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_replay_engine.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add comp_use/replay/ tests/test_replay_engine.py
git commit -m "Add deterministic replay engine with 4-bucket outcome taxonomy"
```

---

## Task 12: Escalation controller + local transport

**Files:**
- Create: `comp_use/escalation/__init__.py`
- Create: `comp_use/escalation/controller.py`
- Create: `comp_use/escalation/transport.py`
- Modify: `comp_use/replay/engine.py`
- Test: `tests/test_escalation.py`

**Interfaces:**
- Consumes: `comp_use.schemas.InterventionRequest` (Task 2),
  `comp_use.evidence.EvidenceLogger` (Task 7), `comp_use.surface.Surface` (Task 8).
- Produces: `comp_use.escalation.controller.ControlState` (Enum: `AGENT`, `HUMAN`,
  `NONE`). `comp_use.escalation.controller.EscalationController(evidence_logger,
  transport)` with `control: ControlState` attribute, `escalate(self, request:
  InterventionRequest) -> None` (sets `control = HUMAN`, logs the request, calls
  `transport.notify(request)`, blocks on `transport.wait_for_resume()`, then sets
  `control = AGENT`). `comp_use.escalation.transport.ControlTransport` (abstract)
  with `notify(self, request: InterventionRequest) -> None` and
  `wait_for_resume(self) -> None`. `comp_use.escalation.transport.
  LocalSharedBrowserTransport()` implementing `ControlTransport` — `notify` prints the
  request to stdout, `wait_for_resume` blocks on `input()` until the operator types
  `resume`.
- Modify `ReplayEngine.__init__` to accept an optional `escalation: EscalationController
  | None = None`; when a step's `risk_tier` is `RiskTier.RISKY` and `confirm_risky` is
  `False`, call `escalation.escalate(...)` before executing that step instead of
  proceeding automatically (only if `escalation` is provided — keep the constructor
  arg optional so Task 11's existing tests, which pass no escalation controller, keep
  passing unchanged).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_escalation.py
from comp_use.escalation.controller import ControlState, EscalationController
from comp_use.escalation.transport import ControlTransport
from comp_use.evidence import EvidenceLogger
from comp_use.config import load_settings
from comp_use.guardrail import Guardrail
from comp_use.schemas import InterventionRequest


class FakeTransport(ControlTransport):
    def __init__(self):
        self.notified = []
        self.resumed = False

    def notify(self, request):
        self.notified.append(request)

    def wait_for_resume(self):
        self.resumed = True


def test_escalate_transitions_control_and_resumes(tmp_path):
    settings = load_settings()
    settings.evidence_dir = tmp_path / "evidence"
    guardrail = Guardrail(settings)
    evidence = EvidenceLogger(settings, guardrail, run_id="esc_run")
    transport = FakeTransport()
    controller = EscalationController(evidence, transport)

    assert controller.control == ControlState.AGENT

    request = InterventionRequest(
        run_id="esc_run", capability_or_goal="transfer funds",
        current_step=2, screenshot_path=None, reason="risky action needs confirmation",
    )
    controller.escalate(request)

    assert transport.notified == [request]
    assert transport.resumed is True
    assert controller.control == ControlState.AGENT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_escalation.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'comp_use.escalation'`

- [ ] **Step 3: Write the implementation**

```python
# comp_use/escalation/__init__.py
```

```python
# comp_use/escalation/transport.py
from comp_use.schemas import InterventionRequest


class ControlTransport:
    def notify(self, request: InterventionRequest) -> None:
        raise NotImplementedError

    def wait_for_resume(self) -> None:
        raise NotImplementedError


class LocalSharedBrowserTransport(ControlTransport):
    def notify(self, request: InterventionRequest) -> None:
        print(f"[ESCALATION] {request.reason} (step {request.current_step}) — "
              f"take over the browser window, then type 'resume' here.")

    def wait_for_resume(self) -> None:
        while True:
            typed = input("> ").strip().lower()
            if typed == "resume":
                return
```

```python
# comp_use/escalation/controller.py
from enum import Enum

from comp_use.evidence import EvidenceLogger
from comp_use.escalation.transport import ControlTransport
from comp_use.schemas import InterventionRequest


class ControlState(str, Enum):
    AGENT = "agent"
    HUMAN = "human"
    NONE = "none"


class EscalationController:
    def __init__(self, evidence_logger: EvidenceLogger, transport: ControlTransport):
        self.evidence_logger = evidence_logger
        self.transport = transport
        self.control = ControlState.AGENT

    def escalate(self, request: InterventionRequest) -> None:
        self.control = ControlState.HUMAN
        self.evidence_logger.log_event("escalation_requested", request.model_dump())
        self.transport.notify(request)
        self.transport.wait_for_resume()
        self.evidence_logger.log_event("escalation_resumed", {"run_id": request.run_id})
        self.control = ControlState.AGENT
```

```python
# comp_use/replay/engine.py — modify __init__ and run()
from comp_use.escalation.controller import EscalationController
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.schemas import Artifact, InterventionRequest, OutcomeType, ReplayResult, RiskTier


class ReplayEngine:
    def __init__(self, surface, guardrail: Guardrail, evidence_logger: EvidenceLogger, escalation: EscalationController | None = None):
        self.surface = surface
        self.guardrail = guardrail
        self.evidence_logger = evidence_logger
        self.escalation = escalation

    def _validate_params(self, artifact: Artifact, params: dict) -> str | None:
        for input_param in artifact.input_schema:
            if input_param.required and input_param.name not in params:
                return f"missing required param '{input_param.name}'"
        return None

    def run(self, artifact: Artifact, params: dict, confirm_risky: bool = False) -> ReplayResult:
        validation_error = self._validate_params(artifact, params)
        if validation_error:
            self.evidence_logger.log_event("validation_error", {"detail": validation_error})
            return ReplayResult(outcome=OutcomeType.VALIDATION_ERROR, detail=validation_error)

        for index, step in enumerate(artifact.steps):
            if (
                step.risk_tier == RiskTier.RISKY
                and not confirm_risky
                and self.escalation is not None
            ):
                self.escalation.escalate(
                    InterventionRequest(
                        run_id=self.evidence_logger.run_id,
                        capability_or_goal=artifact.capability_name,
                        current_step=index,
                        screenshot_path=None,
                        reason=f"step {index} is risk_tier=risky and confirm_risky is False",
                    )
                )

            text = None
            if step.value_source is not None and step.value_source.type == "goal_parameter":
                text = str(params[step.value_source.param_name])

            target_url = step.target
            self.guardrail.check_allowlist(target_url or self.surface.current_url(), step.action.value)

            self.surface.act(step.action, locator=step.locator, target=target_url, text=text)
            self.evidence_logger.log_event("replay_step", {"index": index, "action": step.action.value})

            if step.checkpoint is not None and not self.surface.check_checkpoint(step.checkpoint):
                return ReplayResult(
                    outcome=OutcomeType.HARD_FAILURE,
                    step_index=index,
                    expected=str(step.checkpoint),
                    observed=self.surface.current_url(),
                )

        if not self.surface.check_checkpoint(artifact.success_checkpoint):
            return ReplayResult(
                outcome=OutcomeType.HARD_FAILURE,
                step_index=len(artifact.steps) - 1,
                expected=str(artifact.success_checkpoint),
                observed=self.surface.current_url(),
            )

        return ReplayResult(outcome=OutcomeType.SUCCESS, outputs={})
```

- [ ] **Step 4: Run tests to verify everything passes**

Run: `pytest tests/test_escalation.py tests/test_replay_engine.py -v`
Expected: PASS (all tests — Task 11's tests keep passing since `escalation` defaults to
`None` and none of those artifacts use `RiskTier.RISKY` steps)

- [ ] **Step 5: Commit**

```bash
git add comp_use/escalation/ comp_use/replay/engine.py tests/test_escalation.py
git commit -m "Add escalation controller and local shared-browser transport"
```

---

## Task 13: CLI entrypoints

**Files:**
- Create: `comp_use/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 1–12: `load_settings`, `Guardrail`, `EvidenceLogger`,
  `PlaywrightSurface`, `OpenRouterClient`, `DiscoveryAgent`, `compile_artifact`,
  `ReplayEngine`, `EscalationController`, `LocalSharedBrowserTransport`, `Artifact`.
- Produces: `comp_use.cli.save_artifact(artifact: Artifact, artifacts_dir: Path) ->
  Path` (writes to `artifacts_dir/<capability_name>/v<version>.json`, creating dirs as
  needed). `comp_use.cli.load_artifact(capability_name: str, artifacts_dir: Path,
  version: int | None = None) -> Artifact` (loads latest version if `version` is
  `None`). `comp_use.cli.main()` — argparse CLI with two subcommands: `discover
  --goal TEXT --start-url URL --capability-name NAME` and `replay --capability-name
  NAME --params JSON`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
import json

from comp_use.cli import load_artifact, save_artifact
from comp_use.schemas import ActionType, Artifact, Checkpoint, CheckpointType, Locator, LocatorStrategy, RiskTier, Step


def _artifact(version=1):
    return Artifact(
        capability_name="lookup_member",
        version=version,
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        steps=[Step(action=ActionType.NAVIGATE, target="/member/search", risk_tier=RiskTier.SAFE)],
        success_checkpoint=Checkpoint(
            type=CheckpointType.ELEMENT_VISIBLE,
            locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Member Detail"}),
        ),
        created_from_run_id="run_1",
    )


def test_save_artifact_writes_versioned_json(tmp_path):
    path = save_artifact(_artifact(version=1), tmp_path)
    assert path == tmp_path / "lookup_member" / "v1.json"
    assert json.loads(path.read_text())["capability_name"] == "lookup_member"


def test_load_artifact_loads_latest_version_when_unspecified(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)
    save_artifact(_artifact(version=2), tmp_path)
    loaded = load_artifact("lookup_member", tmp_path)
    assert loaded.version == 2


def test_load_artifact_loads_specific_version(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)
    save_artifact(_artifact(version=2), tmp_path)
    loaded = load_artifact("lookup_member", tmp_path, version=1)
    assert loaded.version == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'comp_use.cli'`

- [ ] **Step 3: Write the implementation**

```python
# comp_use/cli.py
import argparse
import json
from pathlib import Path

from comp_use.config import load_settings
from comp_use.discovery.agent import DiscoveryAgent
from comp_use.discovery.compiler import compile_artifact
from comp_use.escalation.controller import EscalationController
from comp_use.escalation.transport import LocalSharedBrowserTransport
from comp_use.evidence import EvidenceLogger
from comp_use.guardrail import Guardrail
from comp_use.llm_client import OpenRouterClient
from comp_use.replay.engine import ReplayEngine
from comp_use.schemas import Artifact, Checkpoint, CheckpointType, Locator, LocatorStrategy
from comp_use.surface import PlaywrightSurface


def save_artifact(artifact: Artifact, artifacts_dir: Path) -> Path:
    capability_dir = Path(artifacts_dir) / artifact.capability_name
    capability_dir.mkdir(parents=True, exist_ok=True)
    path = capability_dir / f"v{artifact.version}.json"
    path.write_text(artifact.model_dump_json(indent=2))
    return path


def load_artifact(capability_name: str, artifacts_dir: Path, version: int | None = None) -> Artifact:
    capability_dir = Path(artifacts_dir) / capability_name
    if version is None:
        versions = sorted(
            int(p.stem[1:]) for p in capability_dir.glob("v*.json")
        )
        version = versions[-1]
    path = capability_dir / f"v{version}.json"
    return Artifact.model_validate_json(path.read_text())


def _run_discover(args) -> None:
    import time
    from playwright.sync_api import sync_playwright

    settings = load_settings()
    guardrail = Guardrail(settings)
    run_id = f"discover_{int(time.time())}"
    evidence = EvidenceLogger(settings, guardrail, run_id=run_id)
    llm = OpenRouterClient(settings)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        surface = PlaywrightSurface(page)
        agent = DiscoveryAgent(surface, llm, guardrail, evidence, max_steps=settings.max_discovery_steps)
        trace = agent.run(goal=args.goal, start_url=args.start_url)
        browser.close()

    if not trace.succeeded:
        print(f"Discovery did not reach 'finish' within {settings.max_discovery_steps} steps.")
        return

    success_checkpoint = Checkpoint(
        type=CheckpointType.URL_MATCHES,
        url_pattern=trace.final_url.split("://", 1)[-1].split("/", 1)[-1],
    )
    artifact = compile_artifact(
        trace,
        capability_name=args.capability_name,
        target={"app": "mock_bank", "base_url": args.start_url.split("/member")[0]},
        success_checkpoint=success_checkpoint,
        output_schema=[],
    )
    path = save_artifact(artifact, settings.artifacts_dir)
    print(f"Saved artifact to {path}")


def _run_replay(args) -> None:
    import time
    from playwright.sync_api import sync_playwright

    settings = load_settings()
    guardrail = Guardrail(settings)
    run_id = f"replay_{int(time.time())}"
    evidence = EvidenceLogger(settings, guardrail, run_id=run_id)
    transport = LocalSharedBrowserTransport()
    escalation = EscalationController(evidence, transport)

    artifact = load_artifact(args.capability_name, settings.artifacts_dir)
    params = json.loads(args.params) if args.params else {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        surface = PlaywrightSurface(page)
        engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)
        result = engine.run(artifact, params, confirm_risky=args.confirm_risky)
        browser.close()

    print(result.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="comp-use")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser("discover")
    discover_parser.add_argument("--goal", required=True)
    discover_parser.add_argument("--start-url", required=True)
    discover_parser.add_argument("--capability-name", required=True)
    discover_parser.set_defaults(func=_run_discover)

    replay_parser = subparsers.add_parser("replay")
    replay_parser.add_argument("--capability-name", required=True)
    replay_parser.add_argument("--params", default="{}")
    replay_parser.add_argument("--confirm-risky", action="store_true")
    replay_parser.set_defaults(func=_run_replay)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add comp_use/cli.py tests/test_cli.py
git commit -m "Add CLI: discover and replay subcommands"
```

---

## Task 14: End-to-end evidence capture + README/REPORT fill-in

**Files:**
- Modify: `README.md`
- Create: `REPORT.md`
- Create: `run_mock_app.py`

**Interfaces:**
- Consumes: `mock_app.app.create_app` (Task 3), `comp_use.cli.main` (Task 13).
- No new library interfaces — this task runs the built system end-to-end and writes up
  the results.

- [ ] **Step 1: Add a runnable entrypoint for the mock app**

```python
# run_mock_app.py
from mock_app.app import create_app

if __name__ == "__main__":
    create_app().run(port=5000)
```

- [ ] **Step 2: Run the full test suite**

Run: `pytest -v`
Expected: PASS (all tests from Tasks 1–13)

- [ ] **Step 3: Start the mock app in one terminal**

Run: `python run_mock_app.py`
Expected: server listening on `http://localhost:5000`

- [ ] **Step 4: Run a real discovery capability against it**

Set `OPENROUTER_API_KEY` in the environment (see `.env.example`), then run:

```bash
python -m comp_use.cli discover \
  --goal "Look up member 12345 and view their account balances" \
  --start-url "http://localhost:5000/member/search" \
  --capability-name lookup_member
```

Expected: prints `Saved artifact to artifacts/lookup_member/v1.json`; a
`discover_<timestamp>/` directory appears under `evidence/` with `log.jsonl`.

- [ ] **Step 5: Replay the saved artifact successfully**

```bash
python -m comp_use.cli replay --capability-name lookup_member --params "{\"member_id\": \"12345\"}"
```

Expected: prints a `ReplayResult` JSON with `"outcome": "success"`; a new
`replay_<timestamp>/` directory appears under `evidence/`.

- [ ] **Step 6: Replay with a bad param to capture the validation_error bucket**

```bash
python -m comp_use.cli replay --capability-name lookup_member --params "{}"
```

Expected: prints a `ReplayResult` JSON with `"outcome": "validation_error"` — no
browser window opens.

- [ ] **Step 7: Fill in README's Setup and Demo path sections**

Replace the `_TBD once implementation begins._` placeholders under `## Setup` and
`## Demo path` in `README.md` with the exact commands used in Steps 3–6 above
(`pip install -r requirements.txt && playwright install chromium`, `python
run_mock_app.py`, the two `comp-use` CLI invocations), plus the note that
`OPENROUTER_API_KEY` must be set for `discover` (replay never calls the LLM).

- [ ] **Step 8: Write REPORT.md**

Create `REPORT.md` with the seven headings from the assignment (Architecture,
Artifact schema, Determinism & error handling, Heterogeneity & multi-tenant,
Escalation & handoff, Safety, Cuts), each summarizing the corresponding section of
`docs/superpowers/specs/2026-08-17-computer-use-automation-design.md` and pointing at
the concrete files that implement it (e.g. "Artifact schema — see
`comp_use/schemas.py`; example at `artifacts/lookup_member/v1.json`"), plus the two
evidence runs captured in Steps 4–6 as a walkthrough with actual outcomes observed.

- [ ] **Step 9: Commit**

```bash
git add run_mock_app.py README.md REPORT.md artifacts/ evidence/
git commit -m "Add end-to-end demo entrypoint, evidence capture, and REPORT.md"
```

---

## Self-Review Notes

- **Spec coverage:** §2 target app → Tasks 3–5. §3 tech choices → Task 1, 8, 9. §4
  architecture → Tasks 8–13 (component boundaries). §5 artifact schema → Task 2, 10.
  §6 determinism/error handling → Task 11. §7 escalation → Task 12. §8 safety → Task 6,
  7. §9 heterogeneity (design only) → covered by the `Surface`/`ControlTransport`
  interfaces already in Tasks 8, 12 — no additional build task, consistent with spec's
  "designed only" scoping. §10 deliverables mapping → Task 14. §11 cuts → carried
  forward unchanged from the spec; nothing in this plan builds them.
- **Type consistency:** `Step`, `Locator`, `ValueSource`, `Checkpoint`, `Artifact`,
  `ReplayResult`, `InterventionRequest` are defined once in Task 2 and imported
  identically (same field names) by every later task — verified against each task's
  Interfaces block above.
- **No placeholders:** every step has runnable code or an exact command; Task 14's
  README/REPORT steps reference the actual artifacts produced by Steps 4–6 rather than
  describing them abstractly.
