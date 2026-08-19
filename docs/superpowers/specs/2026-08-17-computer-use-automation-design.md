# Computer-Use Automation System — Design Spec

Date: 2026-08-17
Status: Approved for implementation planning

## 1. Context & Goal

Build a small but real system that: (1) uses an LLM to discover how to accomplish a
goal by driving a live web UI, (2) records the successful run as a typed, versioned,
reusable "capability" artifact, (3) replays that artifact deterministically — no LLM in
the decision loop — with input params in / typed outputs out, (4) enforces safety
guardrails throughout, and (5) can escalate to a human who takes over the *same* live
session when the system can't safely proceed on its own.

This mirrors interface.ai's real problem: legacy bank back-office apps with no API,
where the only way in is driving the UI like a human operator. Full assignment text:
`Assignment A — Computer-Use Automation System.pdf` (repo root).

Non-goals (explicitly out of scope per the assignment): the "agent-facing product"
that talks to end users and decides *what* to do; building against a real bank system;
scaling infrastructure (queues, clusters, multi-tenant plumbing); a full real-time
co-browsing operator console.

## 2. Target application

A self-built, local, **legacy-styled** mock bank back-office web app (Flask,
server-rendered HTML — nested tables, no test IDs, minimal/no semantic markup) exposing
three flows, chosen to exercise both read and write/risky capabilities:

1. **Member lookup** — search by member ID → detail view (balances). Read-only.
2. **Open sub-account** — from member detail, multi-field form → confirmation screen.
   Write, moderate risk.
3. **Funds transfer** — between two accounts for a member, multi-field form →
   confirmation screen. Write, high risk (irreversible in the mock's own terms).

Rationale: self-built means no ToS/rate-limit risk, no dependency on a third party
staying up, and full control over injecting the runtime error states the assignment
cares about (validation errors, not-found, permission denial, confirmation dialogs,
session timeout). A legacy-styled surface (not a modern clean-DOM app) directly
exercises the "no clean DOM, no test IDs" reality called out as the common case.

## 3. Language & core tech choices

- **Python**, single process. Strong LLM SDK support, first-class Playwright API,
  Pydantic for typed schemas, fast to stand up a Flask mock app.
- **Playwright**, driving a real (headed, for escalation reasons — see §7) browser.
- **Perception**: hybrid — accessibility-tree snapshot (primary signal: cheap,
  precise, robust to nested-table markup) with a screenshot attached as supplementary
  visual context for the LLM to disambiguate when the tree text is ambiguous.
- **Action**: Playwright locators built from accessibility-tree data — role/name/text
  based, not CSS/XPath, with a CSS fallback recorded but deprioritized.
- **LLM provider**: OpenRouter (free-tier models) behind a swappable `LLMClient`
  abstraction (single interface, provider chosen via config). Documented in
  README/REPORT as swappable to a local model (Ollama/vLLM) for full data locality in
  a real deployment — see §8 (Safety) for why this matters.
- **Storage**: flat JSON files for artifacts (`/artifacts/<capability>/v<N>.json`) and
  JSONL + screenshots for evidence/logs (`/evidence/<run_id>/`). No database. README
  documents the production-scale storage design (versioned artifact store + object
  storage + approval workflow) without building it — appropriate-simplicity per the
  assignment's own guidance not to prematurely build infrastructure.

## 4. Architecture

Single Python process, five components:

- **Mock App** (Flask) — the target surface, run locally.
- **Agent Loop** (discovery) — LLM-driven observe → decide → act loop. Takes a
  natural-language goal + target entry point. Drives Playwright, perceives via
  accessibility tree + screenshot, calls tools (`click`, `type`, `navigate`, `extract`,
  `finish`), stops on goal completion, max-steps, timeout, or dead-end. On success,
  compiles the step trace into an artifact (§5).
- **Replay Engine** (production path) — given a saved artifact + input params,
  validates params against `input_schema` (pre-flight), then executes steps directly
  via Playwright locators, checking per-step and final checkpoints, classifying
  outcomes (§6), and returning declared outputs. No LLM call.
- **Guardrail layer** — wraps every action in both loops: allowlist check (domain/route
  + action type), risk-tier classification (safe vs. risky), and a redaction pass on
  anything about to be sent to the LLM or written to disk. Shared by both loops, not
  duplicated.
- **Escalation Controller** — holds a `control` state (`agent` / `human` / `none`) on
  the live browser session. Raises `InterventionRequest`s, blocks the active loop,
  hands the same live (headed) browser to a human via a local control surface, and
  resumes the loop on signal. See §7.

No services, no queues, no multi-process orchestration — synchronous single process,
justified by scope (assignment explicitly does not reward building scaling infra).

**Caller boundary (assumption).** The system does not decide for itself whether to
discover or replay a goal — the caller states explicitly which capability it wants,
by name (CLI: `discover --capability-name X` / `replay --capability-name X`). We are
not building a router that takes a raw natural-language goal, checks whether a
matching artifact already exists, and dispatches to replay-if-known /
discover-if-not. This follows directly from the brief's own framing in §1: *"the
agent-facing product decides what to do; this system is how it reliably and safely
does it."* Deciding *which* capability to invoke — including matching a fuzzy goal to
an existing artifact — is that upstream product's job, not this system's. The
optional stretch goal in §8 of the brief ("expose saved artifacts as a catalog...
invoke by name with typed args") is consistent with this: even the stretch version is
invoke-by-name, not infer-intent-and-auto-select. We did not build the catalog/API
layer itself (that remains a stretch goal, not attempted — see §11 Cuts); the
assumption here is only about *where the dispatch decision lives*, not about
building the dispatcher.

## 5. Artifact schema

Not prescribed by the assignment beyond a minimum field list (§3.2 of the brief) —
schema design is ours and is one of the most heavily weighted evaluation criteria.

```jsonc
{
  "capability_name": "open_sub_account",
  "version": 1,
  "target": { "app": "mock_bank", "base_url": "http://localhost:5000" },
  "description": "Opens a new sub-account for an existing member.",
  "input_schema": [
    { "name": "member_id", "type": "string", "required": true,
      "description": "Existing member's ID", "example": "12345" },
    { "name": "deposit_amount", "type": "number", "required": true,
      "description": "Initial deposit amount in USD", "example": 500 }
  ],
  "output_schema": [
    { "name": "sub_account_id", "type": "string",
      "description": "Newly created sub-account identifier" },
    { "name": "confirmation_number", "type": "string" }
  ],
  "steps": [
    {
      "action": "navigate", "target": "/member/search",
      "risk_tier": "safe"
    },
    {
      "action": "type_text",
      "locator": { "strategy": "role", "value": {"role": "textbox", "name": "Member ID"},
                   "fallback": { "strategy": "css", "value": "#member-id-input" } },
      "value_source": { "type": "goal_parameter", "param_name": "member_id" },
      "risk_tier": "safe"
    },
    {
      "action": "click",
      "locator": { "strategy": "role", "value": {"role": "button", "name": "Search"} },
      "risk_tier": "safe",
      "checkpoint": { "type": "element_visible",
                       "locator": {"role": "heading", "name_contains": "Member Detail"} }
    }
    // ... remaining steps: open sub-account form, fill deposit_amount, submit,
    // reach confirmation, extract sub_account_id + confirmation_number
  ],
  "success_checkpoint": {
    "type": "element_visible",
    "locator": { "role": "heading", "name_contains": "Confirmation" }
  },
  "created_from_run_id": "run_2026-08-17T12-00-00Z_abc123"
}
```

Key design decisions:

- **`input_schema` is derived, not inferred.** During discovery, every `type_text` /
  `select_option` tool call the LLM makes must include a `value_source` field:
  `{"type": "goal_parameter", "param_name": "...", "param_type": "..."}` or
  `{"type": "fixed", "reason": "..."}`. The LLM tags this inline, when it has full
  context of the goal text — far more reliable than inferring it after the fact from a
  transcript. Post-run compilation collects every `goal_parameter`-tagged step into
  `input_schema`.
- **Locators are role/text-first** (from the accessibility tree) with a recorded CSS
  fallback — this is the seam that would need to change for a desktop surface (swap
  for native accessibility-API element IDs) or a different tenant's app instance (same
  schema, different concrete `locator.value`, see §9).
- **Per-step checkpoints, not just a final one** — lets replay fail fast at the exact
  step that diverged, with a debuggable "what step, what was expected, what was
  observed."
- **Versioned** (`version` field) and **reviewable** — plain JSON, diffable, a human
  reviewer or calling agent can read exactly what a capability does/needs/returns
  without executing it.

## 6. Determinism & error handling (replay)

Four-bucket result taxonomy, returned as a structured result object:

1. **`validation_error`** — pre-flight only. Before any browser interaction, provided
   params are checked against `input_schema` (required fields present, types match).
   Fails immediately with which param was missing/malformed. Never touches the UI.
   (This bucket exists because a missing/bad param is neither a business outcome
   discovered mid-flow nor a runtime UI failure — it's caught before execution starts,
   and cheaply.)
2. **`success`** — all steps completed, `success_checkpoint` verified, declared
   `output_schema` fields extracted and returned.
3. **`business_outcome`** — a legitimate, expected non-success result the UI itself
   reports (e.g. "no such member," "insufficient funds"). Detected via configured
   per-capability outcome patterns (text/element match). Returned as data, not an
   error — this is the assignment's core "business outcome vs. failure" distinction.
4. **`recoverable`** — a known transient/interstitial condition (a dismissable dialog,
   a slow load) that replay handles automatically (dismiss, wait/retry once) and then
   continues; the recovery action taken is recorded in the result for observability.
5. **`hard_failure`** — anything else: an unrecognized state, a checkpoint that never
   resolves, an unhandled error. Stops replay and surfaces `{step, expected,
   observed}` for debugging.

Determinism itself comes from: fixed step order, role/text-based locators (stable
against layout noise, matches how the UI is actually operated), explicit
checkpoint-based waits (never blind `sleep`), and zero LLM calls in this path — same
artifact + same params always attempts the same actions in the same order.

## 7. Escalation & handoff

`InterventionRequest {capability_or_goal, current_step, screenshot, reason}` raised
when: discovery can't decide after N retries, a step is tagged `risky` and the
capability isn't flagged for unattended execution, or replay hits a `hard_failure`.

Mechanism: the browser session carries a `control` state (`agent` / `human` / `none`).
On escalation, the active loop sets `control = "human"` and blocks; the human interacts
directly with the **same live, headed** Playwright browser window (not a fresh
session); a minimal local control surface (CLI or a tiny local web page) shows the
`InterventionRequest` context and offers a "resume" action. On resume, `control =
"agent"`, and the loop re-observes current state before continuing — it does not
assume its last known state is still accurate. Every human action taken during the
handoff is logged to the same evidence trail as the automated steps, preserving one
continuous record of the run.

`ControlTransport` is designed as an interface with two implementations:

- **`LocalSharedBrowserTransport`** (built, fully functional) — human uses the same
  visible browser window directly. Appropriate for a single-machine demo/dev setup.
- **`ServerStreamingTransport`** (designed only, not built) — for a server-deployed
  agent with no local display: stream the headless browser's rendered output (e.g. via
  CDP screencast frames or a VNC-style bridge) to a small web UI where an operator's
  clicks/keystrokes proxy back into the same CDP session. Documented in REPORT.md with
  the data flow and why it's not implemented (assignment explicitly allows mocking the
  operator console as long as the control-transfer *model* is real). Transport choice
  (local vs. server) is a launch-time config decision — the interface is what makes
  that swappable later without touching the Escalation Controller's logic.

## 8. Safety

- **Allowlist**: config-driven, permitted domains/routes + permitted action types.
  Enforced by the Guardrail layer on every action in both discovery and replay; acting
  outside it is a hard block, not a warning.
- **Risk tiers**: every step/action is `safe` (read, navigate) or `risky` (mutates
  state — sub-account open, transfer). Risky actions require an explicit
  `confirm_risky=true` flag on the capability invocation to execute unattended;
  otherwise they trigger escalation (§7) for human confirmation before proceeding.
- **Redaction**: a single redaction function, applied at two boundaries — (a) before
  any accessibility-tree/screenshot content is sent to the LLM provider, and (b) before
  anything is written to logs/evidence/artifacts. Configured field patterns (account
  numbers, full names, balances) are masked/replaced with placeholders in both cases.
  Using the *same* function at both boundaries avoids divergence between "what we show
  the model" and "what we persist."
- **Third-party exposure**: using a hosted LLM (even free-tier) means redacted-but-real
  UI content still leaves the machine per discovery run. REPORT.md documents this
  limitation explicitly and describes the production mitigation: point the same
  `LLMClient` abstraction at a locally-hosted model (Ollama/vLLM) so no UI content
  leaves the institution's environment at all. Not built here — config-only swap,
  documented as the intended production posture.

## 9. Heterogeneity & multi-tenant (design only, not built)

- **Surface abstraction**: perception/action sits behind a `Surface` interface —
  `observe() -> tree_snapshot`, `act(locator) -> result`. The built implementation
  wraps Playwright + accessibility tree. A desktop implementation would swap in an OS
  accessibility API (e.g. `pywinauto`/UIA on Windows) behind the same interface; the
  artifact schema's `locator.strategy` gains a `native_id`/`automation_id` option
  alongside `role`/`css`. The rest of the system (artifact schema, replay engine,
  guardrails, escalation) is unaware of which `Surface` is underneath.
- **Multi-tenant reuse**: artifact `steps[].target`/locator values are stored
  canonicalized/parameterized where possible (e.g. `/member/:id` rather than a
  concrete route), plus an optional `variant_overrides` map keyed by
  tenant-or-vendor-version for cases where the same underlying vendor product differs
  slightly per tenant (a relabeled button, a reordered field). A base artifact is
  recorded once against a reference tenant; a variant override is a sparse diff, not a
  re-recording.
- **Drift detection** (design only): periodically replay a small sample of steps from
  an artifact against each tenant instance; a checkpoint that stops resolving flags
  that tenant's variant as needing review/re-recording, without affecting other
  tenants still on the base artifact.

## 10. Deliverables mapping

- `/README.md` — setup, config (allowlist, redaction fields, transport mode), and the
  exact demo commands: run agent on a goal → replay the resulting artifact →
  (optional) trigger an escalation scenario.
- `/REPORT.md` — the seven required headings (Architecture, Artifact schema,
  Determinism & error handling, Heterogeneity & multi-tenant, Escalation & handoff,
  Safety, Cuts), drawing directly from this spec.
- `/evidence/` — one real discovery run (logs + screenshots) and one replay run,
  including at least one replay that hits a `business_outcome` or injected
  `hard_failure` to demonstrate the taxonomy in §6.

## 11. Cuts (explicit, for REPORT.md §7)

- `ServerStreamingTransport` — designed, not built (see §7).
- Production-scale artifact/evidence storage (DB + object storage + approval workflow)
  — designed, not built (see §3).
- Desktop `Surface` implementation and true multi-tenant execution — designed, not
  built (see §9); only one concrete surface (the mock legacy web app) is implemented.
- Local/on-prem LLM deployment for full data locality — documented as the production
  posture, not built; OpenRouter free-tier is used for the actual discovery run.
- Any "agent-facing product" / natural-language-to-structured-params front end in front
  of replay — out of scope per the assignment's own framing (§1); replay is
  demonstrated with structured typed params directly. (Optionally revisited as a single
  stretch goal — undecided, not committed to this spec.)

## 12. Open item

Whether to attempt one optional stretch goal (§8 of the assignment) — most likely
candidate discussed: a thin capability catalog + single-LLM-call NL-to-params dispatch
demonstrating the "agent-facing capability interface" stretch goal — is deferred until
the core (sections 2–9 above) is complete and evidenced.
