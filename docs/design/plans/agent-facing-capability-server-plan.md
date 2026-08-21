# Agent-Facing Capability Server Implementation Plan

> **Status: complete.** All 10 tasks below were implemented test-first (write the
> failing test, watch it fail, implement, watch it pass, commit), individually
> reviewed, and re-verified together in a final whole-branch review before merge —
> including one real bug (a concurrent-`discover` version-clobbering race) the final
> review caught and a follow-up fix closed. Kept as a record of how the server was
> designed and built. See REPORT.md for the as-built write-up and the
> [task index](#task-index) below to jump to any task.

**Goal:** Add a REST API over the existing `discover`/`invoke` operations (the assignment's §8 "agent-facing capability interface" stretch goal), plus a minimal `draft`/`approved`/`rejected` artifact status that closes a real gap: an unreviewed artifact version must never silently become what unattended replay picks up.

**Architecture:** Reuse all existing core logic unchanged (`ReplayEngine`, `DiscoveryAgent`, `Guardrail`, `EscalationController`) by extracting callable cores (`run_discover`/`run_replay`) from the CLI's argparse handlers, running them on background threads via a new `RunManager`, and signaling human escalation over HTTP via a new `QueueTransport` (a second `ControlTransport` implementation alongside the existing `LocalSharedBrowserTransport`) instead of blocking on `input()`.

**Tech Stack:** FastAPI + uvicorn (new deps), Python `threading`/`queue` (stdlib), existing Pydantic schemas, existing pytest conventions (fakes over real browsers in unit tests; one live pass for `/evidence/`).

**Spec:** `docs/design/specs/agent-facing-capability-server-design.md`

## Global Constraints

- `ReplayEngine` and `DiscoveryAgent` are never modified — the "deterministic replay, no further LLM calls" guarantee and the existing escalation model stay exactly as they are. All new behavior lives in `cli.py`, `escalation/transport.py`, and the new `server/` package.
- No artifact file is ever deleted by any new code path (`approve`/`reject`/`retire` only flip `status` and re-save). No `DELETE` endpoint exists in this pass — see spec §7.
- `Artifact.status` defaults to `"approved"` so all 7 already-committed artifact files parse unchanged with zero migration.
- Existing tests in `tests/test_cli.py`, `tests/test_drift.py`, `tests/test_transport.py` must keep passing unmodified in their assertions about current CLI behavior (only new tests are added, alongside minimal fixture adjustments if a test's own fixtures now need `status` set explicitly).
- New server-layer unit tests use fakes (no real Chromium, no real LLM) — consistent with `test_replay_engine.py`/`test_discovery_agent.py`. Exactly one live, real end-to-end pass (Task 10) produces new `/evidence/`.
- Every route that takes a `capability_name`/`{name}` path parameter validates it against `^[a-z0-9_]+$` (`_validate_capability_name`, added in Task 6, called from every route added in Tasks 6-8) before it's used to build any filesystem path — closes a path-traversal gap (e.g. `name=".."`) found during design review, on a system that stores regulated financial artifacts on disk.

## Task Index

| # | Task | What it builds |
|---|---|---|
| 1 | [`Artifact.status` field](#task-1-artifactstatus-field) | Adds `draft`/`approved`/`rejected` to the artifact schema |
| 2 | [Artifact lifecycle helpers](#task-2-artifact-lifecycle-helpers--approved-only-load_artifact-approve_artifact-reject_artifact-retire_artifact) | `load_artifact` becomes approved-only by default; `approve_artifact`/`reject_artifact`/`retire_artifact` |
| 3 | [`QueueTransport`](#task-3-queuetransport) | Signals an escalation's resume over a thread-safe queue instead of a terminal prompt |
| 4 | [Extract `run_discover`/`run_replay` cores](#task-4-extract-run_discoverrun_replay-cores--cli-approval-prompt) | Callable, transport-injectable core functions + a CLI approval prompt |
| 5 | [`RunManager`](#task-5-runmanager) | Background-thread run tracking (`running`/`escalated`/`done`/`error`) |
| 6 | [FastAPI skeleton + catalog endpoints](#task-6-fastapi-app-skeleton--read-only-catalog-endpoints) | Read-only capability catalog + a path-traversal guard |
| 7 | [`invoke`/`discover`/`resume` endpoints](#task-7-invokediscoverresume-endpoints) | The routes that actually run a capability or start a discovery |
| 8 | [`approve`/`reject`/`retire` endpoints](#task-8-approverejectretire-endpoints) | Version lifecycle management over HTTP |
| 9 | [CLI subcommands](#task-9-comp-use-serve-and-comp-use-approve-cli-subcommands) | `comp-use serve` and `comp-use approve` |
| 10 | [Live evidence + docs](#task-10-live-evidence--readmereport-updates) | A real discover → approve → invoke and escalate → resume cycle over HTTP |

---

### Task 1: `Artifact.status` field

**Files:**
- Modify: `comp_use/schemas.py:97-108` (`Artifact` class)
- Test: `tests/test_schemas.py`

**Interfaces:**
- Produces: `Artifact.status: Literal["draft", "approved", "rejected"]`, default `"approved"`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_schemas.py`:

```python
def test_artifact_status_defaults_to_approved():
    artifact = Artifact(
        capability_name="lookup_member",
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        steps=[],
        success_checkpoint=Checkpoint(type=CheckpointType.URL_MATCHES, url_pattern="member"),
        created_from_run_id="run_1",
    )
    assert artifact.status == "approved"


def test_artifact_status_rejects_invalid_value():
    with pytest.raises(ValidationError):
        Artifact(
            capability_name="lookup_member",
            target={"app": "mock_bank", "base_url": "http://localhost:5000"},
            steps=[],
            success_checkpoint=Checkpoint(type=CheckpointType.URL_MATCHES, url_pattern="member"),
            created_from_run_id="run_1",
            status="not_a_real_status",
        )


def test_old_artifact_json_without_status_field_parses_as_approved():
    # Simulates the 7 already-committed artifact files, none of which have a
    # "status" key - they must keep loading exactly as before.
    old_json = Artifact(
        capability_name="lookup_member",
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        steps=[],
        success_checkpoint=Checkpoint(type=CheckpointType.URL_MATCHES, url_pattern="member"),
        created_from_run_id="run_1",
    ).model_dump_json(exclude={"status"})
    restored = Artifact.model_validate_json(old_json)
    assert restored.status == "approved"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_schemas.py -v`
Expected: `test_artifact_status_defaults_to_approved` and the "old JSON" test FAIL with `AttributeError: 'Artifact' object has no attribute 'status'` (or a `KeyError`-style pydantic error); `test_artifact_status_rejects_invalid_value` FAILs because no error is raised (status field doesn't exist yet, so the kwarg is silently ignored by pydantic's default `extra="ignore"`... actually pydantic v2 default is `extra="ignore"` unless configured — if it errors instead that's also an acceptable "fails for the right reason").

- [ ] **Step 3: Add the field**

In `comp_use/schemas.py`, in the `Artifact` class (after `outcome_patterns`, before `created_from_run_id`):

```python
class Artifact(BaseModel):
    capability_name: str
    version: int = 1
    target: dict[str, str]
    description: str = ""
    input_schema: list[InputParam] = Field(default_factory=list)
    output_schema: list[OutputParam] = Field(default_factory=list)
    steps: list[Step]
    success_checkpoint: Checkpoint
    outcome_patterns: list[OutcomePattern] = Field(default_factory=list)
    status: Literal["draft", "approved", "rejected"] = "approved"
    created_from_run_id: str
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_schemas.py -v`
Expected: PASS, all tests including pre-existing ones (`test_artifact_round_trips_through_json` etc. — unaffected since they don't reference `status`).

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `python -m pytest -q`
Expected: same pass count as before plus 3 new passes; no failures (every existing `Artifact(...)` construction across the test suite omits `status`, which is fine since it's optional with a default).

- [ ] **Step 6: Commit**

```bash
git add comp_use/schemas.py tests/test_schemas.py
git commit -m "feat: add draft/approved/rejected status to Artifact"
```

---

### Task 2: Artifact lifecycle helpers — approved-only `load_artifact`, `approve_artifact`, `reject_artifact`, `retire_artifact`

**Files:**
- Modify: `comp_use/cli.py:46-59` (`load_artifact`)
- Modify: `comp_use/drift.py:56-57` (`propose_drift_patch`'s returned artifact)
- Test: `tests/test_cli.py`
- Test: `tests/test_drift.py`

**Interfaces:**
- Consumes: `Artifact.status` (Task 1).
- Produces: `load_artifact(capability_name, artifacts_dir, version=None) -> Artifact` (same signature, new default-selection behavior), `approve_artifact(capability_name, version, artifacts_dir) -> Artifact`, `reject_artifact(capability_name, version, artifacts_dir) -> Artifact`, `retire_artifact(capability_name, version, artifacts_dir) -> Artifact` — all raise `FileNotFoundError` for an unknown capability/version and `ValueError` for an invalid state transition. Later tasks (5, 8) import all four from `comp_use.cli`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py` (uses the existing `_artifact(version=...)` helper already in that file — extend it to accept `status`):

```python
def test_load_artifact_skips_draft_versions_when_unspecified(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)
    save_artifact(_artifact(version=2, status="draft"), tmp_path)
    loaded = load_artifact("lookup_member", tmp_path)
    assert loaded.version == 1


def test_load_artifact_raises_a_clear_error_when_all_versions_are_drafts(tmp_path):
    save_artifact(_artifact(version=1, status="draft"), tmp_path)
    with pytest.raises(FileNotFoundError, match="none are approved"):
        load_artifact("lookup_member", tmp_path)


def test_load_artifact_with_explicit_version_bypasses_the_draft_filter(tmp_path):
    save_artifact(_artifact(version=1, status="draft"), tmp_path)
    loaded = load_artifact("lookup_member", tmp_path, version=1)
    assert loaded.version == 1
    assert loaded.status == "draft"


def test_approve_artifact_flips_draft_to_approved(tmp_path):
    save_artifact(_artifact(version=1, status="draft"), tmp_path)
    approved = approve_artifact("lookup_member", 1, tmp_path)
    assert approved.status == "approved"
    reloaded = load_artifact("lookup_member", tmp_path, version=1)
    assert reloaded.status == "approved"


def test_approve_artifact_rejects_a_version_that_is_not_a_draft(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)  # default status="approved"
    with pytest.raises(ValueError, match="not 'draft'"):
        approve_artifact("lookup_member", 1, tmp_path)


def test_reject_artifact_flips_draft_to_rejected_without_deleting_the_file(tmp_path):
    path = save_artifact(_artifact(version=1, status="draft"), tmp_path)
    rejected = reject_artifact("lookup_member", 1, tmp_path)
    assert rejected.status == "rejected"
    assert path.exists()


def test_reject_artifact_rejects_a_version_that_is_not_a_draft(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)
    with pytest.raises(ValueError, match="not 'draft'"):
        reject_artifact("lookup_member", 1, tmp_path)


def test_retire_artifact_flips_approved_to_rejected(tmp_path):
    save_artifact(_artifact(version=1), tmp_path)  # approved
    retired = retire_artifact("lookup_member", 1, tmp_path)
    assert retired.status == "rejected"


def test_retire_artifact_rejects_a_version_that_is_not_approved(tmp_path):
    save_artifact(_artifact(version=1, status="draft"), tmp_path)
    with pytest.raises(ValueError, match="not 'approved'"):
        retire_artifact("lookup_member", 1, tmp_path)
```

Find the existing `_artifact(...)` test helper near the top of `tests/test_cli.py` and add a `status` parameter (default `"approved"` to match the schema default, passed through to `Artifact(...)`):

```python
def _artifact(version=1, status="approved"):
    return Artifact(
        capability_name="lookup_member",
        version=version,
        status=status,
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        steps=[],
        success_checkpoint=Checkpoint(type=CheckpointType.URL_MATCHES, url_pattern="member"),
        created_from_run_id="run_1",
    )
```

(If the existing helper already has a different shape, add the `status` parameter to it in place rather than duplicating a second helper — check the current definition first.)

Add to `tests/test_drift.py`:

```python
def test_propose_drift_patch_marks_the_patched_artifact_as_draft():
    from comp_use.llm_client import FakeLLMClient
    artifact = _make_artifact()
    assert artifact.status == "approved"  # base artifact is a normal approved one
    llm = FakeLLMClient(scripted_drift_diagnoses=[
        {"found": True, "locator": {"strategy": "role", "value": {"role": "button", "name": "Confirm Transfer"}}, "reasoning": "renamed"},
    ])
    diagnosis = propose_drift_patch(llm, FakeSurfaceForDrift(), artifact, failed_step_index=1)
    assert diagnosis.patched_artifact.status == "draft"
```

(Check `FakeLLMClient`'s actual constructor kwarg name for scripted drift diagnoses in `comp_use/llm_client.py` / existing `test_drift.py` usage before writing this — match whatever `test_drift.py`'s other drift-diagnosis tests already use rather than guessing a new kwarg name.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py tests/test_drift.py -v -k "draft or approve or reject or retire"`
Expected: FAIL — `load_artifact` doesn't filter by status yet; `approve_artifact`/`reject_artifact`/`retire_artifact` don't exist (`ImportError`/`AttributeError`); the drift test fails because `patched_artifact.status` is `"approved"` (inherited from the base via `model_copy`), not `"draft"`.

- [ ] **Step 3: Implement**

In `comp_use/cli.py`, replace `load_artifact`:

```python
def load_artifact(capability_name: str, artifacts_dir: Path, version: int | None = None) -> Artifact:
    capability_dir = Path(artifacts_dir) / capability_name
    if version is None:
        versions = sorted(
            (int(p.stem[1:]) for p in capability_dir.glob("v*.json")), reverse=True
        )
        if not versions:
            raise FileNotFoundError(
                f"no artifact found for capability '{capability_name}' in {capability_dir} "
                "(check --capability-name for a typo, or run 'discover' first)"
            )
        for candidate_version in versions:
            candidate = Artifact.model_validate_json(
                (capability_dir / f"v{candidate_version}.json").read_text()
            )
            if candidate.status == "approved":
                return candidate
        raise FileNotFoundError(
            f"capability '{capability_name}' has {len(versions)} version(s) in {capability_dir} "
            "but none are approved (approve one via `comp-use approve --capability-name "
            f"{capability_name} --version N`, or pass --version explicitly to load a draft)"
        )
    path = capability_dir / f"v{version}.json"
    return Artifact.model_validate_json(path.read_text())


def approve_artifact(capability_name: str, version: int, artifacts_dir: Path) -> Artifact:
    artifact = load_artifact(capability_name, artifacts_dir, version=version)
    if artifact.status != "draft":
        raise ValueError(f"version {version} is '{artifact.status}', not 'draft' - only a draft can be approved")
    artifact.status = "approved"
    save_artifact(artifact, artifacts_dir)
    return artifact


def reject_artifact(capability_name: str, version: int, artifacts_dir: Path) -> Artifact:
    artifact = load_artifact(capability_name, artifacts_dir, version=version)
    if artifact.status != "draft":
        raise ValueError(f"version {version} is '{artifact.status}', not 'draft' - only a draft can be rejected")
    artifact.status = "rejected"
    save_artifact(artifact, artifacts_dir)
    return artifact


def retire_artifact(capability_name: str, version: int, artifacts_dir: Path) -> Artifact:
    artifact = load_artifact(capability_name, artifacts_dir, version=version)
    if artifact.status != "approved":
        raise ValueError(f"version {version} is '{artifact.status}', not 'approved' - only an approved version can be retired")
    artifact.status = "rejected"
    save_artifact(artifact, artifacts_dir)
    return artifact
```

In `comp_use/drift.py`'s `propose_drift_patch`, after `patched = artifact.model_copy(deep=True)`:

```python
    patched = artifact.model_copy(deep=True)
    patched.status = "draft"
    patched.steps[failed_step_index].locator = new_locator
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py tests/test_drift.py -v`
Expected: PASS, including every pre-existing test in both files (in particular `test_load_artifact_loads_latest_version_when_unspecified` and the drift end-to-end test `test_...drift...` in `test_cli.py`, both of which use approved-by-default artifacts and so are unaffected by the new filter).

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: no regressions; count increases by the new tests added in this task.

- [ ] **Step 6: Commit**

```bash
git add comp_use/cli.py comp_use/drift.py tests/test_cli.py tests/test_drift.py
git commit -m "feat: approved-only load_artifact + approve/reject/retire lifecycle"
```

---

### Task 3: `QueueTransport`

**Files:**
- Modify: `comp_use/escalation/transport.py` (add class, alongside existing `ControlTransport`/`LocalSharedBrowserTransport`)
- Test: `tests/test_transport.py`

**Interfaces:**
- Consumes: `ControlTransport` (existing base class), `InterventionRequest` (existing schema).
- Produces: `QueueTransport(on_notify: Callable[[InterventionRequest], None])` with `.notify(request)`, `.wait_for_resume() -> str` (blocks), `.resume(note: str) -> None`. Task 5 (`RunManager`) and Task 7 (FastAPI routes) depend on this exact interface.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_transport.py`:

```python
import threading
import time

from comp_use.escalation.transport import QueueTransport
from comp_use.schemas import InterventionRequest


def test_queue_transport_notify_invokes_the_callback_with_the_exact_request():
    received = []
    transport = QueueTransport(on_notify=received.append)
    request = InterventionRequest(run_id="r1", capability_or_goal="lookup_member", reason="stuck")

    transport.notify(request)

    assert received == [request]


def test_queue_transport_wait_for_resume_blocks_until_resume_is_called():
    transport = QueueTransport(on_notify=lambda r: None)
    result = {}

    def waiter():
        result["note"] = transport.wait_for_resume()

    waiter_thread = threading.Thread(target=waiter)
    waiter_thread.start()
    time.sleep(0.05)
    assert "note" not in result  # still blocked - nobody has called resume() yet

    transport.resume("handled it via the API")
    waiter_thread.join(timeout=1)

    assert result["note"] == "handled it via the API"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_transport.py -v -k queue_transport`
Expected: FAIL with `ImportError: cannot import name 'QueueTransport'`.

- [ ] **Step 3: Implement**

In `comp_use/escalation/transport.py`, add at the top and at the end:

```python
import queue
from typing import Callable

from comp_use.schemas import InterventionRequest


class ControlTransport:
    def notify(self, request: InterventionRequest) -> None:
        raise NotImplementedError

    def wait_for_resume(self) -> str:
        raise NotImplementedError


class LocalSharedBrowserTransport(ControlTransport):
    # ... unchanged ...


class QueueTransport(ControlTransport):
    """A ControlTransport for the capability server: `notify()` reports the
    escalation to a caller-supplied callback (how GET /runs/{run_id} learns
    about it) instead of printing, and `wait_for_resume()` blocks on a
    thread-safe queue instead of stdin - unblocked by POST /runs/{run_id}/resume
    calling .resume(note) from a different thread (the HTTP request thread)."""

    def __init__(self, on_notify: Callable[[InterventionRequest], None]):
        self._on_notify = on_notify
        self._resume_queue: "queue.Queue[str]" = queue.Queue()

    def notify(self, request: InterventionRequest) -> None:
        self._on_notify(request)

    def wait_for_resume(self) -> str:
        return self._resume_queue.get()

    def resume(self, note: str) -> None:
        self._resume_queue.put(note)
```

(Keep the existing `LocalSharedBrowserTransport` class body exactly as-is — only add the `import queue`, `from typing import Callable`, and the new `QueueTransport` class.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_transport.py -v`
Expected: PASS, all tests including the 3 pre-existing `wait_for_resume`/EOFError tests.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add comp_use/escalation/transport.py tests/test_transport.py
git commit -m "feat: add QueueTransport for HTTP-signaled escalation resume"
```

---

### Task 4: Extract `run_discover`/`run_replay` cores + CLI approval prompt

**Files:**
- Modify: `comp_use/cli.py:115-291` (`_run_discover`, `_run_replay`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `QueueTransport`/`ControlTransport` (Task 3), `approve_artifact`/`load_artifact` (Task 2).
- Produces: `run_discover(goal: str, start_url: str, capability_name: str, confirm_risky: bool, transport: ControlTransport, interactive: bool = True) -> Artifact | None` and `run_replay(capability_name: str, params: dict, confirm_risky: bool, diagnose_drift_on_failure: bool, transport: ControlTransport) -> ReplayResult`. Task 6/7 (FastAPI routes) call these directly.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py` (these exercise the *interactive prompt* behavior specifically; the existing live-server integration tests already exercise `_run_discover`/`_run_replay` end-to-end and must keep passing unmodified — they're the regression check for this task, not new tests):

```python
def test_run_discover_produces_a_draft_artifact_by_default(tmp_path, live_server, monkeypatch):
    monkeypatch.setenv("COMP_USE_HEADLESS", "1")
    settings = _settings_for(tmp_path, live_server)
    transport = FakeTransport()  # existing fake from test_escalation.py-style tests; reuse comp_use.escalation.transport.ControlTransport
    with patch.object(cli, "load_settings", return_value=settings), \
         patch.object(cli, "OpenRouterClient", return_value=_scripted_lookup_member()), \
         patch("builtins.input", side_effect=EOFError):  # non-interactive stdin: must NOT auto-approve
        artifact = cli.run_discover(
            goal="look up member 12345",
            start_url=f"{live_server}/member/search",
            capability_name="lookup_member_draft_test",
            confirm_risky=False,
            transport=transport,
            interactive=True,
        )
    assert artifact.status == "draft"


def test_run_discover_approves_when_the_operator_confirms_interactively(tmp_path, live_server, monkeypatch):
    monkeypatch.setenv("COMP_USE_HEADLESS", "1")
    settings = _settings_for(tmp_path, live_server)
    transport = FakeTransport()
    with patch.object(cli, "load_settings", return_value=settings), \
         patch.object(cli, "OpenRouterClient", return_value=_scripted_lookup_member()), \
         patch("builtins.input", return_value="y"):
        artifact = cli.run_discover(
            goal="look up member 12345",
            start_url=f"{live_server}/member/search",
            capability_name="lookup_member_approve_test",
            confirm_risky=False,
            transport=transport,
            interactive=True,
        )
    assert artifact.status == "approved"


def test_run_discover_non_interactive_never_prompts_and_stays_draft(tmp_path, live_server, monkeypatch):
    monkeypatch.setenv("COMP_USE_HEADLESS", "1")
    settings = _settings_for(tmp_path, live_server)
    transport = FakeTransport()
    with patch.object(cli, "load_settings", return_value=settings), \
         patch.object(cli, "OpenRouterClient", return_value=_scripted_lookup_member()), \
         patch("builtins.input", side_effect=AssertionError("must not prompt when interactive=False")):
        artifact = cli.run_discover(
            goal="look up member 12345",
            start_url=f"{live_server}/member/search",
            capability_name="lookup_member_api_test",
            confirm_risky=False,
            transport=transport,
            interactive=False,
        )
    assert artifact.status == "draft"
```

(`_scripted_lookup_member`, `_settings_for`, `live_server` are the existing fixtures/helpers already used by `test_run_discover_produces_a_reusable_checkpoint_end_to_end` earlier in this file — reuse them, don't redefine. If `FakeTransport` isn't already defined/importable in `tests/test_cli.py`, add a minimal one matching the pattern in `tests/test_replay_engine.py`:
```python
class FakeTransport(ControlTransport):
    def notify(self, request):
        pass
    def wait_for_resume(self):
        return "handled it"
```
importing `from comp_use.escalation.transport import ControlTransport` at the top of the file if not already imported.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -v -k "run_discover_produces_a_draft or approves_when_the_operator or non_interactive"`
Expected: FAIL with `AttributeError: module 'comp_use.cli' has no attribute 'run_discover'`.

- [ ] **Step 3: Implement**

Replace `_run_discover` in `comp_use/cli.py` with a core function plus a thin wrapper. The core is today's `_run_discover` body, changed to take explicit parameters instead of `args`, take `transport`/`interactive` instead of hardcoding `LocalSharedBrowserTransport()`, set `artifact.status = "draft"` after compiling, add the interactive-approval prompt, and `return artifact` (or `None`) instead of only printing:

```python
def run_discover(
    goal: str, start_url: str, capability_name: str, confirm_risky: bool,
    transport, interactive: bool = True,
) -> Artifact | None:
    import time
    from playwright.sync_api import sync_playwright

    settings = load_settings()
    guardrail = Guardrail(settings)
    run_id = f"discover_{int(time.time())}"
    evidence = EvidenceLogger(settings, guardrail, run_id=run_id)
    llm = _build_llm_client(settings)
    if settings.model_provider == "nvidia" and settings.nvidia_api_key:
        primary_label, primary_model = "NVIDIA NIM", settings.nvidia_model
        fallback_label, fallback_model, fallback_key = "OpenRouter", settings.openrouter_model, settings.openrouter_api_key
    else:
        primary_label, primary_model = "OpenRouter", settings.openrouter_model
        fallback_label, fallback_model, fallback_key = "NVIDIA NIM", settings.nvidia_model, settings.nvidia_api_key
    fallback_note = f" (falls back to {fallback_label}:{fallback_model} on failure)" if fallback_key else ""
    print(
        f"Discovering with {primary_label}:{primary_model}{fallback_note} (max {settings.max_discovery_steps} steps)",
        flush=True,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=_headless())
        page = browser.new_page()
        surface = PlaywrightSurface(page)
        escalation = EscalationController(evidence, transport, surface=surface)
        agent = DiscoveryAgent(
            surface, llm, guardrail, evidence, max_steps=settings.max_discovery_steps,
            escalation=escalation, confirm_risky=confirm_risky,
        )
        trace = agent.run(goal=goal, start_url=start_url)
        if not trace.succeeded:
            evidence.save_screenshot(safe_screenshot(surface), "final")
            browser.close()
        else:
            success_checkpoint = _derive_success_checkpoint(surface, fallback_url=trace.final_url)
            browser.close()

    if not trace.succeeded:
        print(f"Discovery did not reach 'finish' within {settings.max_discovery_steps} steps.")
        return None

    artifact = compile_artifact(
        trace,
        capability_name=capability_name,
        target={"app": "mock_bank", "base_url": start_url.split("/member")[0]},
        success_checkpoint=success_checkpoint,
        output_schema=[],
    )
    if not any(step.action == ActionType.NAVIGATE for step in artifact.steps):
        artifact.steps.insert(
            0,
            Step(action=ActionType.NAVIGATE, target=start_url, risk_tier=RiskTier.SAFE),
        )
    artifact.status = "draft"
    artifact.version = next_artifact_version(capability_name, settings.artifacts_dir)
    if artifact.version > 1:
        try:
            prev_artifact = load_artifact(capability_name, settings.artifacts_dir, version=artifact.version - 1)
            if prev_artifact.outcome_patterns:
                artifact.outcome_patterns = prev_artifact.outcome_patterns
                print(
                    f"[discover] carried forward {len(prev_artifact.outcome_patterns)} "
                    f"outcome_patterns(s) from v{prev_artifact.version}",
                    flush=True,
                )
        except (FileNotFoundError, ValueError):
            pass

    if interactive:
        try:
            answer = input("Approve as new default? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer == "y":
            artifact.status = "approved"

    path = save_artifact(artifact, settings.artifacts_dir)
    print(f"Saved artifact to {path} (status={artifact.status})")
    return artifact


def _run_discover(args) -> None:
    transport = LocalSharedBrowserTransport()
    run_discover(
        goal=args.goal, start_url=args.start_url, capability_name=args.capability_name,
        confirm_risky=args.confirm_risky, transport=transport, interactive=True,
    )
```

Replace `_run_replay` the same way:

```python
def run_replay(
    capability_name: str, params: dict, confirm_risky: bool,
    diagnose_drift_on_failure: bool, transport,
) -> ReplayResult:
    import time
    from playwright.sync_api import sync_playwright

    settings = load_settings()
    guardrail = Guardrail(settings)
    run_id = f"replay_{int(time.time())}"
    evidence = EvidenceLogger(settings, guardrail, run_id=run_id)

    artifact = load_artifact(capability_name, settings.artifacts_dir)

    validation_error = validate_required_params(artifact, params)
    if validation_error:
        evidence.log_event("validation_error", {"detail": validation_error})
        return ReplayResult(outcome=OutcomeType.VALIDATION_ERROR, detail=validation_error)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=_headless())
        page = browser.new_page()
        surface = PlaywrightSurface(page)
        escalation = EscalationController(evidence, transport, surface=surface)
        engine = ReplayEngine(surface, guardrail, evidence, escalation=escalation)
        result = engine.run(artifact, params, confirm_risky=confirm_risky)
        if result.outcome != OutcomeType.SUCCESS:
            evidence.save_screenshot(safe_screenshot(surface), "final")
        if diagnose_drift_on_failure and result.outcome == OutcomeType.HARD_FAILURE:
            _diagnose_and_propose_patch(settings, evidence, escalation, surface, artifact, result)
        browser.close()

    return result


def _run_replay(args) -> None:
    transport = LocalSharedBrowserTransport()
    params = json.loads(args.params) if args.params else {}
    result = run_replay(
        capability_name=args.capability_name, params=params, confirm_risky=args.confirm_risky,
        diagnose_drift_on_failure=args.diagnose_drift_on_failure, transport=transport,
    )
    print(result.model_dump_json(indent=2))
```

Note: `run_replay` no longer needs the separate pre-`sync_playwright` `validate_required_params` short-circuit duplicated in the thin wrapper — it's already inside `run_replay` itself (moved verbatim from the original `_run_replay`), so the "skip launching Chromium on validation_error" behavior is preserved exactly, just one layer down.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS — both the 3 new tests and every pre-existing test in the file, including the live-server discover/replay integration tests and the drift end-to-end test (which call `cli._run_discover(args)`/`cli._run_replay(args)` exactly as before — those signatures are unchanged).

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add comp_use/cli.py tests/test_cli.py
git commit -m "refactor: extract run_discover/run_replay cores with injectable transport"
```

---

### Task 5: `RunManager`

**Files:**
- Create: `comp_use/server/__init__.py` (empty)
- Create: `comp_use/server/run_manager.py`
- Test: `tests/test_run_manager.py`

**Interfaces:**
- Consumes: `QueueTransport` (Task 3), `InterventionRequest`/`ReplayResult` (existing schemas).
- Produces: `RunRecord` dataclass, `RunManager` with `.start(kind, capability_name, target_fn) -> str`, `.get(run_id) -> RunRecord | None`, `.resume(run_id, note) -> bool`. Task 7 (FastAPI routes) depends on this exact interface.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_run_manager.py`:

```python
import time

from comp_use.schemas import InterventionRequest, OutcomeType, ReplayResult
from comp_use.server.run_manager import RunManager


def _wait_until(predicate, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_run_manager_completes_a_run_and_stores_the_result():
    manager = RunManager()
    run_id = manager.start("invoke", "lookup_member", lambda transport: ReplayResult(outcome=OutcomeType.SUCCESS))

    assert _wait_until(lambda: manager.get(run_id).status == "done")
    record = manager.get(run_id)
    assert record.kind == "invoke"
    assert record.result.outcome == OutcomeType.SUCCESS


def test_run_manager_stores_discover_result_under_discover_result_not_result():
    manager = RunManager()
    run_id = manager.start("discover", "lookup_member", lambda transport: {"succeeded": True, "artifact_version": 3})

    assert _wait_until(lambda: manager.get(run_id).status == "done")
    record = manager.get(run_id)
    assert record.discover_result == {"succeeded": True, "artifact_version": 3}
    assert record.result is None


def test_run_manager_escalates_then_resumes_to_completion():
    manager = RunManager()

    def target(transport):
        transport.notify(InterventionRequest(run_id="ignored", capability_or_goal="lookup_member", reason="risky step"))
        note = transport.wait_for_resume()
        return ReplayResult(outcome=OutcomeType.SUCCESS, detail=note)

    run_id = manager.start("invoke", "lookup_member", target)

    assert _wait_until(lambda: manager.get(run_id).status == "escalated")
    assert manager.get(run_id).escalation.reason == "risky step"

    assert manager.resume(run_id, "handled it via the API") is True

    assert _wait_until(lambda: manager.get(run_id).status == "done")
    assert manager.get(run_id).result.detail == "handled it via the API"


def test_run_manager_records_error_on_unhandled_exception_without_crashing():
    manager = RunManager()

    def target(transport):
        raise RuntimeError("boom")

    run_id = manager.start("invoke", "lookup_member", target)

    assert _wait_until(lambda: manager.get(run_id).status == "error")
    assert "boom" in manager.get(run_id).error


def test_run_manager_resume_returns_false_when_not_currently_escalated():
    manager = RunManager()
    run_id = manager.start("invoke", "lookup_member", lambda transport: ReplayResult(outcome=OutcomeType.SUCCESS))

    assert manager.resume(run_id, "too early") is False


def test_run_manager_get_returns_none_for_unknown_run_id():
    manager = RunManager()
    assert manager.get("does_not_exist") is None


def test_run_manager_concurrent_runs_get_distinct_ids_and_dont_share_state():
    manager = RunManager()
    run_id_a = manager.start("invoke", "lookup_member", lambda t: ReplayResult(outcome=OutcomeType.SUCCESS, detail="a"))
    run_id_b = manager.start("invoke", "transfer_funds", lambda t: ReplayResult(outcome=OutcomeType.SUCCESS, detail="b"))

    assert run_id_a != run_id_b
    assert _wait_until(lambda: manager.get(run_id_a).status == "done" and manager.get(run_id_b).status == "done")
    assert manager.get(run_id_a).result.detail == "a"
    assert manager.get(run_id_b).result.detail == "b"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_run_manager.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'comp_use.server'`.

- [ ] **Step 3: Implement**

Create `comp_use/server/__init__.py` (empty file).

Create `comp_use/server/run_manager.py`:

```python
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from comp_use.escalation.transport import QueueTransport
from comp_use.schemas import InterventionRequest, ReplayResult


@dataclass
class RunRecord:
    run_id: str
    kind: Literal["discover", "invoke"]
    capability_name: str
    status: Literal["running", "escalated", "done", "error"] = "running"
    escalation: InterventionRequest | None = None
    transport: QueueTransport | None = None
    result: ReplayResult | None = None
    discover_result: dict | None = None
    error: str | None = None


class RunManager:
    """Runs `target_fn` on a background thread per capability-server request, so
    an HTTP call can return immediately while the real Playwright browser keeps
    driving the app. Escalation is surfaced via QueueTransport's on_notify
    callback (see .start()) instead of blocking the caller's HTTP thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._runs: dict[str, RunRecord] = {}

    def start(self, kind: str, capability_name: str, target_fn: Callable[[QueueTransport], Any]) -> str:
        with self._lock:
            run_id = f"{kind}_{int(time.time() * 1000)}"
            while run_id in self._runs:
                run_id = f"{kind}_{int(time.time() * 1000)}"
            record = RunRecord(run_id=run_id, kind=kind, capability_name=capability_name)
            self._runs[run_id] = record

        def on_notify(request: InterventionRequest) -> None:
            with self._lock:
                record.status = "escalated"
                record.escalation = request

        transport = QueueTransport(on_notify=on_notify)
        record.transport = transport

        def worker() -> None:
            try:
                outcome = target_fn(transport)
            except Exception as exc:
                with self._lock:
                    record.status = "error"
                    record.error = f"{type(exc).__name__}: {exc}"
                return
            with self._lock:
                if kind == "invoke":
                    record.result = outcome
                else:
                    record.discover_result = outcome
                record.status = "done"

        threading.Thread(target=worker, daemon=True).start()
        return run_id

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def resume(self, run_id: str, note: str) -> bool:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or record.status != "escalated":
                return False
            record.status = "running"
        record.transport.resume(note)
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_run_manager.py -v`
Expected: PASS, all 8 tests.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add comp_use/server/__init__.py comp_use/server/run_manager.py tests/test_run_manager.py
git commit -m "feat: add RunManager for background-thread discover/invoke execution"
```

---

### Task 6: FastAPI app skeleton + read-only catalog endpoints

**Files:**
- Create: `comp_use/server/app.py`
- Modify: `requirements.txt` (add `fastapi`, `uvicorn`, `httpx`)
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `load_artifact` (Task 2), `Settings`/`load_settings` (existing `comp_use.config`).
- Produces: `create_app(settings: Settings | None = None) -> FastAPI` with `GET /capabilities`, `GET /capabilities/{name}`, `GET /capabilities/{name}/versions`. Task 7/8 add routes to the same `app.py`.

- [ ] **Step 1: Install dependencies**

```bash
pip install fastapi uvicorn httpx
```

Add to `requirements.txt`:

```
fastapi==0.119.0
uvicorn==0.37.0
httpx==0.28.1
```

(Pin to whatever versions `pip show fastapi uvicorn httpx` reports after install, so the pinned versions match what's actually installed and tested against.)

- [ ] **Step 2: Write the failing tests**

Create `tests/test_server.py`:

```python
from fastapi.testclient import TestClient

from comp_use.cli import save_artifact
from comp_use.config import Settings
from comp_use.schemas import Artifact, Checkpoint, CheckpointType, InputParam, OutputParam
from comp_use.server.app import create_app


def _artifact(capability_name="lookup_member", version=1, status="approved", description="Looks up a member."):
    return Artifact(
        capability_name=capability_name,
        version=version,
        status=status,
        description=description,
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        input_schema=[InputParam(name="member_id", type="string", required=True)],
        output_schema=[OutputParam(name="balance", type="string")],
        steps=[],
        success_checkpoint=Checkpoint(type=CheckpointType.URL_MATCHES, url_pattern="member"),
        created_from_run_id="run_1",
    )


def _client(tmp_path):
    settings = Settings(artifacts_dir=tmp_path / "artifacts", evidence_dir=tmp_path / "evidence")
    app = create_app(settings)
    return TestClient(app), settings


def test_list_capabilities_returns_the_latest_approved_summary(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(version=1), settings.artifacts_dir)
    save_artifact(_artifact(version=2, status="draft"), settings.artifacts_dir)

    response = client.get("/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["capability_name"] == "lookup_member"
    assert body[0]["version"] == 1  # v2 is draft, skipped
    assert body[0]["has_pending_draft"] is True
    assert body[0]["input_schema"][0]["name"] == "member_id"


def test_list_capabilities_is_empty_when_no_artifacts_dir_exists(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/capabilities")
    assert response.status_code == 200
    assert response.json() == []


def test_get_capability_returns_full_schema(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)

    response = client.get("/capabilities/lookup_member")

    assert response.status_code == 200
    assert response.json()["description"] == "Looks up a member."


def test_get_capability_404s_when_none_approved(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="draft"), settings.artifacts_dir)

    response = client.get("/capabilities/lookup_member")

    assert response.status_code == 404


def test_get_capability_400s_on_a_path_traversal_attempt(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/capabilities/..")
    assert response.status_code == 400


def test_list_versions_reports_status_for_every_version(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(version=1, status="approved"), settings.artifacts_dir)
    save_artifact(_artifact(version=2, status="draft"), settings.artifacts_dir)

    response = client.get("/capabilities/lookup_member/versions")

    assert response.status_code == 200
    versions = {v["version"]: v["status"] for v in response.json()}
    assert versions == {1: "approved", 2: "draft"}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'comp_use.server.app'`.

- [ ] **Step 4: Implement**

Create `comp_use/server/app.py`:

```python
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

import re

from comp_use.cli import load_artifact
from comp_use.config import Settings, load_settings
from comp_use.server.run_manager import RunManager

_CAPABILITY_NAME_RE = re.compile(r"^[a-z0-9_]+$")


def _validate_capability_name(name: str) -> None:
    """`name` is used to build a filesystem path (`artifacts_dir / name`) in
    every route below - reject anything that isn't a plain lowercase/digits/
    underscore token before it ever reaches Path(), so a value like ".." can't
    resolve outside artifacts_dir. Path traversal on a regulated financial
    artifact store is a live vulnerability, not just a missing feature."""
    if not _CAPABILITY_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"invalid capability name {name!r}: must match {_CAPABILITY_NAME_RE.pattern}",
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.evidence_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="comp-use capability server")
    app.state.settings = settings
    app.state.run_manager = RunManager()
    app.mount("/evidence", StaticFiles(directory=str(settings.evidence_dir)), name="evidence")

    @app.get("/capabilities")
    def list_capabilities():
        artifacts_dir = settings.artifacts_dir
        if not artifacts_dir.exists():
            return []
        out = []
        for capability_dir in sorted(p for p in artifacts_dir.iterdir() if p.is_dir()):
            name = capability_dir.name
            versions = sorted(int(p.stem[1:]) for p in capability_dir.glob("v*.json"))
            latest_approved = None
            has_pending_draft = False
            for v in reversed(versions):
                candidate = load_artifact(name, artifacts_dir, version=v)
                if candidate.status == "approved" and latest_approved is None:
                    latest_approved = candidate
                if candidate.status == "draft":
                    has_pending_draft = True
            out.append({
                "capability_name": name,
                "description": latest_approved.description if latest_approved else None,
                "version": latest_approved.version if latest_approved else None,
                "input_schema": [p.model_dump() for p in latest_approved.input_schema] if latest_approved else [],
                "output_schema": [p.model_dump() for p in latest_approved.output_schema] if latest_approved else [],
                "has_pending_draft": has_pending_draft,
            })
        return out

    @app.get("/capabilities/{name}")
    def get_capability(name: str):
        _validate_capability_name(name)
        try:
            artifact = load_artifact(name, settings.artifacts_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return artifact.model_dump(mode="json")

    @app.get("/capabilities/{name}/versions")
    def list_versions(name: str):
        _validate_capability_name(name)
        capability_dir = settings.artifacts_dir / name
        if not capability_dir.exists():
            raise HTTPException(status_code=404, detail=f"no capability '{name}'")
        out = []
        for v in sorted(int(p.stem[1:]) for p in capability_dir.glob("v*.json")):
            artifact = load_artifact(name, settings.artifacts_dir, version=v)
            out.append({"version": v, "status": artifact.status, "created_from_run_id": artifact.created_from_run_id})
        return out

    return app
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_server.py -v`
Expected: PASS, all 6 tests.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: no regressions.

- [ ] **Step 7: Commit**

```bash
git add comp_use/server/app.py requirements.txt tests/test_server.py
git commit -m "feat: FastAPI capability server skeleton with read-only catalog endpoints + capability-name validation"
```

---

### Task 7: `invoke`/`discover`/`resume` endpoints

**Files:**
- Modify: `comp_use/server/app.py` (add routes)
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `run_replay`, `run_discover` (Task 4), `RunManager` (Task 5), `validate_required_params` (existing, `comp_use.replay.engine`).
- Produces: `POST /capabilities/{name}/invoke`, `POST /capabilities/{name}/discover`, `GET /runs/{run_id}`, `POST /runs/{run_id}/resume`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_server.py`:

```python
import time

from comp_use.schemas import InterventionRequest, OutcomeType, ReplayResult
import comp_use.server.app as app_module


def _wait_for_status(client, run_id, status, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body["status"] == status:
            return body
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} never reached status={status!r}, last body: {body}")


def test_invoke_400s_on_missing_required_params_without_starting_a_run(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)

    called = []
    monkeypatch.setattr(app_module, "run_replay", lambda *a, **k: called.append(1))

    response = client.post("/capabilities/lookup_member/invoke", json={"params": {}})

    assert response.status_code == 400
    assert called == []


def test_invoke_404s_when_no_approved_version_exists(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="draft"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/invoke", json={"params": {"member_id": "12345"}})

    assert response.status_code == 404


def test_invoke_runs_in_the_background_and_reports_success(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)
    monkeypatch.setattr(
        app_module, "run_replay",
        lambda capability_name, params, confirm_risky, diagnose_drift_on_failure, transport:
            ReplayResult(outcome=OutcomeType.SUCCESS, outputs={"balance": "100"}),
    )

    response = client.post("/capabilities/lookup_member/invoke", json={"params": {"member_id": "12345"}})
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    body = _wait_for_status(client, run_id, "done")
    assert body["result"]["outcome"] == "success"
    assert body["result"]["outputs"]["balance"] == "100"


def test_discover_runs_in_the_background_and_reports_the_new_draft_version(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    monkeypatch.setattr(
        app_module, "run_discover",
        lambda goal, start_url, capability_name, confirm_risky, transport, interactive:
            _artifact(capability_name=capability_name, version=1, status="draft"),
    )

    response = client.post(
        "/capabilities/new_capability/discover",
        json={"goal": "look up a member", "start_url": "http://localhost:5000/member/search"},
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    body = _wait_for_status(client, run_id, "done")
    assert body["discover_result"] == {"succeeded": True, "artifact_version": 1}


def test_discover_reports_failure_when_run_discover_returns_none(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    monkeypatch.setattr(
        app_module, "run_discover",
        lambda goal, start_url, capability_name, confirm_risky, transport, interactive: None,
    )

    response = client.post(
        "/capabilities/new_capability/discover",
        json={"goal": "an impossible goal", "start_url": "http://localhost:5000/member/search"},
    )
    run_id = response.json()["run_id"]

    body = _wait_for_status(client, run_id, "done")
    assert body["discover_result"] == {"succeeded": False, "artifact_version": None}


def test_run_escalates_and_resumes_over_http(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)

    def fake_run_replay(capability_name, params, confirm_risky, diagnose_drift_on_failure, transport):
        transport.notify(InterventionRequest(run_id="ignored", capability_or_goal=capability_name, reason="risky step"))
        note = transport.wait_for_resume()
        return ReplayResult(outcome=OutcomeType.SUCCESS, detail=note)

    monkeypatch.setattr(app_module, "run_replay", fake_run_replay)

    response = client.post("/capabilities/lookup_member/invoke", json={"params": {"member_id": "12345"}})
    run_id = response.json()["run_id"]

    escalated = _wait_for_status(client, run_id, "escalated")
    assert escalated["escalation"]["reason"] == "risky step"

    resume_response = client.post(f"/runs/{run_id}/resume", json={"note": "confirmed manually"})
    assert resume_response.status_code == 202

    done = _wait_for_status(client, run_id, "done")
    assert done["result"]["detail"] == "confirmed manually"


def test_resume_409s_when_run_is_not_escalated(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(), settings.artifacts_dir)
    monkeypatch.setattr(
        app_module, "run_replay",
        lambda capability_name, params, confirm_risky, diagnose_drift_on_failure, transport:
            ReplayResult(outcome=OutcomeType.SUCCESS),
    )

    response = client.post("/capabilities/lookup_member/invoke", json={"params": {"member_id": "12345"}})
    run_id = response.json()["run_id"]
    _wait_for_status(client, run_id, "done")

    resume_response = client.post(f"/runs/{run_id}/resume", json={"note": "too late"})
    assert resume_response.status_code == 409


def test_get_run_404s_for_unknown_run_id(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/runs/does_not_exist")
    assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_server.py -v -k "invoke or discover or resume or escalat or get_run_404"`
Expected: FAIL — `/capabilities/{name}/invoke` etc. don't exist yet (404 on all of them from FastAPI's own routing, not the intended assertions), and `app_module.run_replay`/`run_discover` don't exist as module attributes to monkeypatch.

- [ ] **Step 3: Implement**

In `comp_use/server/app.py`, add the imports and Pydantic request models, and the new routes inside `create_app`:

```python
from typing import Any

from pydantic import BaseModel

from comp_use.cli import load_artifact, run_discover, run_replay
from comp_use.replay.engine import validate_required_params
```

```python
class InvokeRequest(BaseModel):
    params: dict[str, Any] = {}


class DiscoverRequest(BaseModel):
    goal: str
    start_url: str


class ResumeRequest(BaseModel):
    note: str = ""
```

Inside `create_app`, after the existing three routes:

```python
    @app.post("/capabilities/{name}/invoke", status_code=202)
    def invoke_capability(name: str, body: InvokeRequest):
        _validate_capability_name(name)
        try:
            artifact = load_artifact(name, settings.artifacts_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        validation_error = validate_required_params(artifact, body.params)
        if validation_error:
            raise HTTPException(status_code=400, detail=validation_error)

        def target(transport):
            return run_replay(name, body.params, False, False, transport)

        run_id = app.state.run_manager.start("invoke", name, target)
        return {"run_id": run_id, "status": "running"}

    @app.post("/capabilities/{name}/discover", status_code=202)
    def discover_capability(name: str, body: DiscoverRequest):
        _validate_capability_name(name)

        def target(transport):
            artifact = run_discover(body.goal, body.start_url, name, False, transport, False)
            if artifact is None:
                return {"succeeded": False, "artifact_version": None}
            return {"succeeded": True, "artifact_version": artifact.version}

        run_id = app.state.run_manager.start("discover", name, target)
        return {"run_id": run_id, "status": "running"}

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        record = app.state.run_manager.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown run_id '{run_id}'")
        escalation = None
        if record.escalation is not None:
            screenshot_url = None
            if record.escalation.screenshot_path:
                from pathlib import Path
                screenshot_url = f"/evidence/{record.escalation.run_id}/{Path(record.escalation.screenshot_path).name}"
            escalation = {
                "reason": record.escalation.reason,
                "current_step": record.escalation.current_step,
                "screenshot_url": screenshot_url,
            }
        return {
            "run_id": record.run_id,
            "kind": record.kind,
            "status": record.status,
            "escalation": escalation,
            "result": record.result.model_dump(mode="json") if record.result else None,
            "discover_result": record.discover_result,
            "error": record.error,
        }

    @app.post("/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: str, body: ResumeRequest):
        resumed = app.state.run_manager.resume(run_id, body.note)
        if not resumed:
            record = app.state.run_manager.get(run_id)
            if record is None:
                raise HTTPException(status_code=404, detail=f"unknown run_id '{run_id}'")
            raise HTTPException(status_code=409, detail=f"run is '{record.status}', not 'escalated'")
        return {"status": "running"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_server.py -v`
Expected: PASS, all tests in the file (Task 6's 5 plus this task's 8).

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add comp_use/server/app.py tests/test_server.py
git commit -m "feat: invoke/discover/resume endpoints over RunManager"
```

---

### Task 8: `approve`/`reject`/`retire` endpoints

**Files:**
- Modify: `comp_use/server/app.py` (add routes)
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `approve_artifact`, `reject_artifact`, `retire_artifact` (Task 2).
- Produces: `POST /capabilities/{name}/versions/{version}/approve`, `.../reject`, `.../retire`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_server.py`:

```python
def test_approve_endpoint_flips_draft_to_approved_and_is_visible_in_catalog(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="draft"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/versions/1/approve")

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert client.get("/capabilities/lookup_member").status_code == 200


def test_approve_endpoint_409s_on_a_version_that_is_not_a_draft(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="approved"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/versions/1/approve")

    assert response.status_code == 409


def test_approve_endpoint_404s_on_an_unknown_version(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="draft"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/versions/99/approve")

    assert response.status_code == 404


def test_reject_endpoint_flips_draft_to_rejected(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="draft"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/versions/1/reject")

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_retire_endpoint_flips_approved_to_rejected(tmp_path):
    client, settings = _client(tmp_path)
    save_artifact(_artifact(status="approved"), settings.artifacts_dir)

    response = client.post("/capabilities/lookup_member/versions/1/retire")

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    # retired means no longer picked up as the default
    assert client.get("/capabilities/lookup_member").status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_server.py -v -k "approve_endpoint or reject_endpoint or retire_endpoint"`
Expected: FAIL — routes don't exist (404 from FastAPI's own routing).

- [ ] **Step 3: Implement**

In `comp_use/server/app.py`, update the import and add the three routes:

```python
from comp_use.cli import approve_artifact, load_artifact, reject_artifact, retire_artifact, run_discover, run_replay
```

```python
    @app.post("/capabilities/{name}/versions/{version}/approve")
    def approve(name: str, version: int):
        _validate_capability_name(name)
        try:
            artifact = approve_artifact(name, version, settings.artifacts_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return artifact.model_dump(mode="json")

    @app.post("/capabilities/{name}/versions/{version}/reject")
    def reject(name: str, version: int):
        _validate_capability_name(name)
        try:
            artifact = reject_artifact(name, version, settings.artifacts_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return artifact.model_dump(mode="json")

    @app.post("/capabilities/{name}/versions/{version}/retire")
    def retire(name: str, version: int):
        _validate_capability_name(name)
        try:
            artifact = retire_artifact(name, version, settings.artifacts_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return artifact.model_dump(mode="json")
```

(`approve_artifact`/`reject_artifact`/`retire_artifact` raise `FileNotFoundError` only for an unknown capability name or version number — not for an invalid state transition, which is `ValueError` — matching the two `except` clauses above.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_server.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add comp_use/server/app.py tests/test_server.py
git commit -m "feat: approve/reject/retire endpoints for capability version lifecycle"
```

---

### Task 9: `comp-use serve` and `comp-use approve` CLI subcommands

**Files:**
- Modify: `comp_use/cli.py` (`main()`, add `_run_serve`, `_run_approve`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `create_app` (Task 6/7/8), `approve_artifact` (Task 2).
- Produces: `comp-use serve [--host] [--port]`, `comp-use approve --capability-name X --version N`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py` (follow the existing pattern used by `test_main_dispatches_discover_subcommand_with_parsed_args` in this file for how subcommand dispatch is already tested — mirror it rather than re-deriving the pattern):

```python
def test_main_dispatches_approve_subcommand_with_parsed_args(tmp_path, monkeypatch):
    save_artifact(_artifact(version=1, status="draft"), tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(artifacts_dir=tmp_path, evidence_dir=tmp_path / "evidence"))
    monkeypatch.setattr(
        "sys.argv",
        ["comp-use", "approve", "--capability-name", "lookup_member", "--version", "1"],
    )

    cli.main()

    reloaded = load_artifact("lookup_member", tmp_path, version=1)
    assert reloaded.status == "approved"


def test_run_serve_launches_uvicorn_with_the_app(monkeypatch):
    calls = {}

    def fake_run(app, host, port):
        calls["host"] = host
        calls["port"] = port

    monkeypatch.setattr("uvicorn.run", fake_run)

    args = argparse.Namespace(host="0.0.0.0", port=9000)
    cli._run_serve(args)

    assert calls == {"host": "0.0.0.0", "port": 9000}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -v -k "approve_subcommand or run_serve_launches"`
Expected: FAIL — `approve` isn't a recognized subcommand yet (argparse `SystemExit`/error), and `cli._run_serve` doesn't exist.

- [ ] **Step 3: Implement**

In `comp_use/cli.py`, add near the bottom of `main()` (alongside the existing `discover_parser`/`replay_parser`):

```python
def _run_approve(args) -> None:
    settings = load_settings()
    artifact = approve_artifact(args.capability_name, args.version, settings.artifacts_dir)
    print(f"Approved {args.capability_name} v{artifact.version}")


def _run_serve(args) -> None:
    import uvicorn
    from comp_use.server.app import create_app

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port)
```

```python
    approve_parser = subparsers.add_parser("approve")
    approve_parser.add_argument("--capability-name", required=True)
    approve_parser.add_argument("--version", type=int, required=True)
    approve_parser.set_defaults(func=_run_approve)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.set_defaults(func=_run_serve)
```

(Add both blocks before `args = parser.parse_args()` in `main()`, matching where `discover_parser`/`replay_parser` are currently defined.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: no regressions. This is the last code task — confirm the total pass count against the count from before Task 1 plus every new test added across Tasks 1-9.

- [ ] **Step 6: Commit**

```bash
git add comp_use/cli.py tests/test_cli.py
git commit -m "feat: comp-use serve and comp-use approve CLI subcommands"
```

---

### Task 10: Live evidence + README/REPORT updates

This task is done by hand, not delegated to an automated worker — it requires the real mock app, a real LLM key, and the real server running simultaneously, plus judgment calls about what's demo-worthy. Run it once Tasks 1-9 are merged.

**Files:**
- Modify: `README.md` (new "Capability server" section with exact commands)
- Modify: `REPORT.md` (Architecture section: mention the server as an additional entrypoint; Artifact schema section: document `status`)
- Create: new `evidence/discover_.../` and `evidence/replay_.../` directories from the live run below (exact names generated at run time)

- [ ] **Step 1: Start the mock app and the capability server**

```bash
python -m mock_app.app &            # or however the existing README already starts it
COMP_USE_HEADLESS=1 python -m comp_use.cli serve --port 8000 &
```

- [ ] **Step 2: Drive one full discover → approve → invoke cycle entirely over HTTP**

```bash
curl -s -X POST http://localhost:8000/capabilities/lookup_member_api_demo/discover \
  -H "Content-Type: application/json" \
  -d '{"goal": "look up member 12345 and read their current savings balance", "start_url": "http://localhost:5000/member/search"}'
# -> {"run_id": "discover_...", "status": "running"}

curl -s http://localhost:8000/runs/<run_id>   # poll until status == "done"
# -> {"status": "done", "discover_result": {"succeeded": true, "artifact_version": 1}, ...}

curl -s -X POST http://localhost:8000/capabilities/lookup_member_api_demo/versions/1/approve

curl -s -X POST http://localhost:8000/capabilities/lookup_member_api_demo/invoke \
  -H "Content-Type: application/json" \
  -d '{"params": {"member_id": "67890"}}'
# -> {"run_id": "replay_...", "status": "running"}

curl -s http://localhost:8000/runs/<run_id>   # poll until status == "done"
# -> {"status": "done", "result": {"outcome": "success", ...}}
```

- [ ] **Step 3: Drive one run that hits escalation, to prove `/resume` over HTTP**

Use a capability/params combination known to hit a risky step (e.g. `transfer_funds` without `confirm_risky`, matching how the existing README already demonstrates escalation for the CLI path). Confirm `GET /runs/{run_id}` reports `status: "escalated"` with a populated `escalation.reason` and a working `escalation.screenshot_url`, then confirm `POST /runs/{run_id}/resume` drives it to `"done"`.

- [ ] **Step 4: Verify the evidence**

```bash
python -m pytest -q   # full suite, one final time
```

Confirm the new `evidence/discover_.../` and `evidence/replay_.../` directories from steps 2-3 contain the same shape of `log.jsonl`/screenshots as every other evidence run in the repo — this is what makes the API path "genuinely real," per the spec's Testing plan (§8).

- [ ] **Step 5: Update README.md**

Add a new section (after the existing CLI demo-path section) documenting: how to start the server (`comp-use serve`), the exact `curl` sequence from Steps 2-3 above (with real command output), and one line noting no auth exists yet (matches REPORT.md's Safety section, doesn't contradict it).

- [ ] **Step 6: Update REPORT.md**

In the Architecture section, add 2-3 sentences noting the capability server as a second entrypoint over the same core (`ReplayEngine`/`DiscoveryAgent` untouched), pointing at the spec doc. In the Artifact schema section, add the `status` field to whatever field-by-field description already exists there, with the one-sentence "why" (closes the "unreviewed version becomes live" gap — already stated at length in Safety/Cuts, so keep this pointer brief, not a repeat).

- [ ] **Step 7: Commit**

```bash
git add README.md REPORT.md evidence/
git commit -m "docs: document the capability server + live evidence for the discover/approve/invoke/escalate cycle over HTTP"
```
