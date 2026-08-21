# Agent-Facing Capability Server — Design Spec

Date: 2026-08-21
Status: Approved for implementation planning
Builds on: `docs/superpowers/specs/2026-08-17-computer-use-automation-design.md`

## 1. Context & goal

The assignment's §8 optional stretch goals name an **agent-facing capability
interface**: "expose saved artifacts as a catalog of callable capabilities (a small
tool/function-calling surface, or an API endpoint) that an AI agent could discover and
invoke by name with typed args." REPORT.md previously treated this as out of scope. This
spec reverses that call for one thin slice: a REST API over the existing `discover` /
`invoke` operations, reusing all existing core logic (`ReplayEngine`, `DiscoveryAgent`,
`Guardrail`, `EscalationController`) unchanged.

Alongside it, this spec closes a real gap surfaced during design review: today, a new
artifact version (from `discover` or from `--diagnose-drift-on-failure`'s drift patch)
is described as "gated on human review," but nothing in the code actually enforces
that — `load_artifact()`'s "pick the highest version number" default means an unreviewed
version is live the instant it's saved. Exposing `discover` over an API — where no human
is necessarily watching the browser — makes that gap concrete rather than theoretical, so
this spec adds a minimal `draft`/`approved` status to close it. This is a narrow slice of
the separate "Confidence & approval" stretch goal (no reliability scoring), justified as
closing a correctness gap the capability-server work would otherwise create, not scope
creep for its own sake.

**Non-goals** (explicit, to keep this thin): no auth/multi-user model, no persistent run
store (in-memory only, lost on server restart), no worker pool/queue (each run is one
real headed browser on its own thread — a known, stated scaling cut, consistent with the
assignment's own "don't build scaling infrastructure" guidance), no `ServerStreamingTransport`
(CDP screencast for a *remote* human — still the same "designed, not built" cut as the
original spec; this system assumes the human and the server share a machine, same as
`LocalSharedBrowserTransport` today).

## 2. Schema change: draft / approved status

`comp_use/schemas.py`, `Artifact`:

```python
class Artifact(BaseModel):
    ...
    status: Literal["draft", "approved", "rejected"] = "approved"
```

Default `"approved"` keeps all 7 already-committed artifact files valid with no
migration. Two producers set `status="draft"` explicitly going forward:

- `drift.py`'s `propose_drift_patch` — the patched artifact it returns already gets
  `status="draft"` (it's model-proposed and unattended by construction).
- `discover` (both CLI and API) — see §4.

`comp_use/cli.py`'s `load_artifact(capability_name, artifacts_dir, version=None)`:
when `version` is `None` ("give me the latest"), skip non-`"approved"` versions (`draft`
and `rejected` alike). Walk version numbers descending, load each, return the first with
`status == "approved"`. If none are approved (including "no artifact exists at all"),
raise `FileNotFoundError` with a message distinguishing the two cases: no versions at all
vs. versions exist but none are approved (actionable: "approve one via `comp-use approve`
or pass `--version` explicitly"). Passing an explicit `version=N` bypasses the filter —
how you test a draft before approving it.

New helper, `comp_use/cli.py`:

```python
def approve_artifact(capability_name: str, version: int, artifacts_dir: Path) -> Artifact:
    artifact = load_artifact(capability_name, artifacts_dir, version=version)
    artifact.status = "approved"
    save_artifact(artifact, artifacts_dir)
    return artifact
```

New CLI subcommand: `comp-use approve --capability-name X --version N`.

## 3. Refactor: extract callable cores from the CLI's `_run_discover` / `_run_replay`

Both functions currently take an `argparse.Namespace`, hardcode
`LocalSharedBrowserTransport()`, and only `print()` their result. The server needs the
same logic with an injectable transport and a real return value. Minimal, non-breaking
refactor — behavior for existing CLI callers is unchanged:

- `_run_discover(args)` → thin wrapper that builds a `transport =
  LocalSharedBrowserTransport()`, calls a new `run_discover(goal, start_url,
  capability_name, confirm_risky, transport, interactive=True) -> Artifact | None`, then
  prints. `run_discover` contains today's `_run_discover` body verbatim, with `transport`
  and `interactive` as parameters instead of a hardcoded local and an implicit True.
  - `interactive=True` (CLI default): after a successful discovery, prompt `Approve as
    new default? [y/N]` on stdout/stdin; `y` → `artifact.status = "approved"` before
    saving. Anything else (including EOF, matching the existing `EOFError`-hardening
    precedent in `transport.py`) leaves it `"draft"`.
  - `interactive=False` (server path): always `"draft"`, no prompt.
- `_run_replay(args)` → thin wrapper that builds `LocalSharedBrowserTransport()`, calls
  `run_replay(capability_name, params, confirm_risky, diagnose_drift_on_failure,
  transport) -> ReplayResult`, then prints `result.model_dump_json(indent=2)`.

Both `run_discover`/`run_replay` still open their own `sync_playwright()` block and their
own browser — no shared browser state between concurrent runs.

## 4. `QueueTransport` — the escalation seam for HTTP

New class in `comp_use/escalation/transport.py`, alongside `LocalSharedBrowserTransport`,
implementing the same `ControlTransport` interface:

```python
class QueueTransport(ControlTransport):
    def __init__(self, on_notify: Callable[[InterventionRequest], None]):
        self._on_notify = on_notify
        self._resume_queue: queue.Queue[str] = queue.Queue()

    def notify(self, request: InterventionRequest) -> None:
        self._on_notify(request)

    def wait_for_resume(self) -> str:
        return self._resume_queue.get()  # blocks until /resume posts a note

    def resume(self, note: str) -> None:
        self._resume_queue.put(note)
```

`EscalationController` and `ReplayEngine`/`DiscoveryAgent` are untouched — they already
depend only on the `ControlTransport` interface. `notify()`'s callback is how the run's
status becomes visible to `GET /runs/{run_id}` (see §5); `resume()` is called by the
`POST /runs/{run_id}/resume` handler.

## 5. `RunManager` — background execution + status

New module `comp_use/server/run_manager.py`. One `RunRecord` per in-flight or completed
run, held in-memory in a `dict[str, RunRecord]` behind a `threading.Lock`:

```python
@dataclass
class RunRecord:
    run_id: str
    kind: Literal["discover", "invoke"]
    status: Literal["running", "escalated", "done", "error"]
    capability_name: str
    escalation: InterventionRequest | None = None
    transport: QueueTransport | None = None
    result: ReplayResult | None = None            # kind == "invoke"
    discover_result: dict | None = None            # kind == "discover": {"succeeded", "artifact_version"}
    error: str | None = None                       # unhandled exception in the worker thread
```

`RunManager.start(kind, capability_name, target_fn: Callable[[QueueTransport], Any]) ->
str`: generates a `run_id` (reusing the existing `f"{kind}_{int(time.time())}"`
convention), builds a `QueueTransport` whose `on_notify` callback updates that record's
`status="escalated"` / `escalation=request`, spawns a `threading.Thread` running
`target_fn(transport)`, stores the thread's return value into `result` or
`discover_result` and flips `status="done"` on completion (or `status="error"` /
`error=str(exc)` on an unhandled exception — the worker thread must never crash silently),
and returns `run_id` immediately.

`target_fn` is built by the route handler (`app.py`), closing over the real
`run_discover`/`run_replay` call — this keeps `RunManager` itself generic and unit-testable
with a fake `target_fn` that needs no real browser (see §7).

## 6. FastAPI app — `comp_use/server/app.py`

New dependency: `fastapi`, `uvicorn`. New CLI subcommand `comp-use serve [--host] [--port]`
launches it (`uvicorn.run(app, ...)`) — everything else in `cli.py` is unchanged.

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/capabilities` | List capability dirs under `artifacts_dir`; for each, the latest *approved* artifact's `capability_name`, `description`, `version`, `input_schema`, `output_schema`, plus `has_pending_draft: bool` (any higher-numbered draft exists). Skips a capability with no approved version but a name directory (still reports it, `latest_approved: null`). |
| `GET` | `/capabilities/{name}` | Full schema (all fields) of the latest approved artifact. `404` if none approved. |
| `GET` | `/capabilities/{name}/versions` | All versions on disk with `{version, status, created_from_run_id}`. |
| `POST` | `/capabilities/{name}/discover` | Body: `{goal, start_url}`. Pre-flight: none (discover has no input_schema to validate against yet). Starts a `run_discover(..., transport=queue_transport, interactive=False)` run via `RunManager`. Returns `202 {run_id, status: "running"}`. |
| `POST` | `/capabilities/{name}/invoke` | Body: `{params}`. Pre-flight: `load_artifact(name, artifacts_dir)` (approved-only) then `validate_required_params` — on failure, return `400` immediately, no thread spawned (mirrors `_run_replay`'s existing pre-browser validation-error fast path). Otherwise starts `run_replay(...)` via `RunManager`. Returns `202 {run_id, status: "running"}`. |
| `GET` | `/runs/{run_id}` | `{status, kind, escalation: {reason, current_step, screenshot_url} \| null, result: ReplayResult \| null, discover_result: {...} \| null, error: str \| null}`. `404` if unknown `run_id`. |
| `POST` | `/runs/{run_id}/resume` | Body: `{note}`. `409` if `status != "escalated"`. Otherwise calls the record's `transport.resume(note)`, returns `202 {status: "running"}` (client polls `GET /runs/{run_id}` again for the next state). |
| `POST` | `/capabilities/{name}/versions/{version}/approve` | Calls `approve_artifact` (`draft → approved`). Returns the updated artifact metadata. `404` if that version doesn't exist, `409` if it's already `approved` or `rejected`. |
| `POST` | `/capabilities/{name}/versions/{version}/reject` | Draft-only. `status → "rejected"`. The file is never deleted (same "never destroy history" reasoning as `next_artifact_version` always bumping instead of overwriting) — it's excluded from both "latest approved" and future draft-review listings, but stays on disk as a record of what was proposed and turned down. `409` if the version isn't currently `draft`. |
| `POST` | `/capabilities/{name}/versions/{version}/retire` | Approved-only. `status → "rejected"`. The "take a capability out of service" operation — e.g. a version turns out to be broken or unsafe after being approved. Same non-destructive reasoning as `reject`. `409` if the version isn't currently `approved`. |

No `DELETE` endpoint exists in this pass — see §9 for why, and the deferred design for
when one is added.

`screenshot_url` in `GET /runs/{run_id}`'s escalation payload: the escalation screenshot
already saved to `evidence/<run_id>/...png` by `EscalationController`/`_run_discover`/
`_run_replay`; the server mounts `evidence_dir` as static files so this is a servable
path, not a new mechanism.

## 7. Designed but deferred: hard delete + admin-only auth

**Why delete is excluded now, not just "not gotten to yet."** Every destructive-looking
operation this system already has (artifact versioning, drift patches, reject/retire
above) is deliberately *non-destructive* — nothing already built ever removes a file from
disk. A hard `DELETE` would be the first operation in the entire system that permanently
erases part of the audit trail of what this automation was ever able to do against a real
financial back-office surface. Shipping that on a network-reachable endpoint with **no
authentication at all** (this slice's own stated Cut, §8) is a materially different risk
than every other cut in this project: the others are missing *capabilities* (multi-tenant,
desktop surface); an unauthenticated delete on a regulated-data system would be a standing
*vulnerability* the moment the server is reachable by more than one trusted person. That
asymmetry — "missing feature" vs. "live foot-gun" — is why this pass ships `reject`/
`retire` (fully covers every real "stop using this version" need, reversibly) and
deliberately stops short of delete, rather than shipping delete "for completeness" and
noting the auth gap as an afterthought.

**The deferred design, so it's a real seam and not just a TODO:** a simple per-key role
model — each API key configured server-side carries a `role: "admin" | "operator"`
(`{"key": "...", "role": "admin"}` entries in server config, checked via an
`X-API-Key` header). `approve`/`reject`/`retire`/`discover`/`invoke` are available to
either role; a new `DELETE /capabilities/{name}/versions/{version}` — hard removal, files
included — is checked against `role == "admin"` specifically, returning `403` for an
`operator` key (or for any request without a valid key at all, once auth exists — today,
with no auth layer, delete simply isn't exposed rather than being exposed-but-unchecked).
This mirrors how real banks/credit unions actually scope destructive back-office actions:
narrow, named admin permission, not "anyone who can reach the internal tool."

## 8. Testing plan

- `tests/test_transport.py` (extend): `QueueTransport` unit tests — `notify()` invokes
  the callback with the exact `InterventionRequest`; `wait_for_resume()` blocks until
  `resume(note)` is called from another thread, then returns that exact note.
- `tests/test_run_manager.py` (new): `RunManager` with a fake `target_fn` (no browser,
  no LLM) verifying the `running → escalated → done` and `running → done` and
  `running → error` state transitions, and that concurrent runs get distinct `run_id`s
  and don't share state.
- `tests/test_server.py` (new): FastAPI `TestClient` against the real endpoints, with
  `run_discover`/`run_replay` monkeypatched to fake callables (no real Chromium/LLM in
  this unit-test tier — consistent with how `test_replay_engine.py`/
  `test_discovery_agent.py` already use fakes) covering: capability listing reflects
  draft/approved correctly, `invoke` 400s on missing required params without starting a
  run, `invoke` on a capability with only draft versions 404s, the full
  `running → escalated → resume → done` cycle via the HTTP endpoints, and
  `approve` flips a draft to approved and makes it visible in `GET /capabilities`.
- `tests/test_schemas.py` / existing artifact tests: default `status="approved"` parses
  old artifact JSON unchanged; `load_artifact()`'s draft-skipping logic gets its own
  test (already-drafted highest version is skipped in favor of the next approved one
  down, or a clear `FileNotFoundError` when all versions are drafts).
- **Live evidence** (not unit tests): one real end-to-end pass driven entirely over HTTP
  — `POST /capabilities/x/discover` against the real mock app with a real LLM client,
  poll to `done`, `POST .../approve`, `POST /capabilities/x/invoke`, poll through any
  real escalation via `POST /runs/{id}/resume`, to `done` with `outcome: success`.
  Captured under `/evidence/` the same as existing discover/replay runs, demonstrating
  the API path is genuinely real, not just unit-tested.

## 9. Cuts (this slice)

- No auth — anyone who can reach the server can invoke/discover/approve/reject/retire.
  Stated limitation; see §7 for the deferred admin-role design and why hard delete
  specifically waits on it rather than shipping unauthenticated.
- No hard delete — see §7.
- In-memory run store only — a server restart loses all run history/status. Stated;
  production would persist run state (ties to the already-documented "production
  artifact/evidence storage — not built" cut).
- No worker pool — N concurrent runs is N real headed Chromium instances on N threads.
  Fine for a demo; explicitly not scaling infrastructure per the assignment's own
  guidance not to prematurely build that.
- `ServerStreamingTransport` (remote human takes control via CDP screencast) — still
  not built, same as the original spec's Cuts. `QueueTransport` solves *signaling*
  resume over HTTP, not remote *viewing/control* of the browser itself.
