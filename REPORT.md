# REPORT — Computer-Use Automation System

Maps the implemented system to the design specs:
[computer-use-automation-design.md](docs/design/specs/computer-use-automation-design.md) and
[agent-facing-capability-server-design.md](docs/design/specs/agent-facing-capability-server-design.md).

## Architecture

Single Python process, small interfaces between components. Only discovery talks to an LLM.

| Component | Role | Code |
|---|---|---|
| Mock app | Legacy-styled Flask bank UI, deliberately hostile (§4) | `mock_app/` |
| Discovery agent | Observe → decide → act; compiles a successful run into an artifact | `comp_use/discovery/` |
| Replay engine | Deterministic step execution, no LLM | `comp_use/replay/engine.py` |
| Guardrail | Allowlist, risk confirmation, redaction | `comp_use/guardrail.py` |
| Escalation | Human-in-the-loop handoff | `comp_use/escalation/` |
| Capability server (optional) | REST API over discover/invoke (§8) | `comp_use/server/` |

**Perception & action** go through `Surface` (`comp_use/surface.py`): accessibility-tree `observe()`, role/text/CSS `act()`, checkpoint checks, screenshot. `PlaywrightSurface` is the built implementation.

**The mock app is deliberately hostile** — nested/decoy tables, an inert `<iframe>`, no test IDs or `aria-label`, a disabled decoy "Advanced Search" button sharing a name with the real "Search" button, and a confirm-interstitial on both risky flows. This caught a real bug: Playwright's `get_by_role(name=...)` substring-matches by default, so the decoy matched too — fixed by passing `exact=True` in `_resolve()`.

**Capability server** — a second entrypoint (FastAPI, `comp_use/server/app.py`) wrapping the same `run_discover`/`run_replay` functions the CLI calls. `ReplayEngine`, `DiscoveryAgent`, `Guardrail`, `EscalationController` are untouched by it. Escalation over HTTP uses `QueueTransport` (blocks on a queue instead of `input()`) plus a background-thread `RunManager`: `POST /invoke` returns immediately with a `run_id`, `POST /runs/{id}/resume` unblocks it from a different thread.

**Scope assumption:** the caller (an upstream agent-facing product) picks discover vs. replay by name — this system executes reliably, it doesn't infer intent (§1: *"the agent-facing product decides what to do; this system is how it reliably and safely does it"*). No fuzzy-goal-to-capability router was built.

**Known caveats:**
- `TEXT_PRESENT` checks raw `page.content()`, not rendered text — correct here since the mock app has no client-side JS hiding content, but would false-positive against an app that hides matching text via CSS/JS.
- The mock app's ID counters are module-level globals, not scoped per instance — a latent test-isolation risk, not an active bug.

## Artifact schema

Typed Pydantic models (`comp_use/schemas.py`). A capability is versioned JSON at `artifacts/<capability_name>/v<N>.json` — example: [`artifacts/lookup_member/v1.json`](artifacts/lookup_member/v1.json).

- **`input_schema`** is derived from discovery, not inferred after the fact: each field's `value_source` (`goal_parameter` vs. `fixed`) tells `compile_artifact()` what to collect. `validate_required_params()` is the single source of truth for "does this `params` dict satisfy this artifact," shared by `ReplayEngine.run()` and the CLI's pre-browser check, so the two can't drift apart. **Known gap:** `InputParam.type` is declared but never validated at runtime — every param in this build is a string in practice.
- **`output_schema`** is derived the same way from `extract` steps. `open_sub_account`/`transfer_funds` both extract a confirmation/transaction ID; replay returns them in `ReplayResult.outputs`.
- **Locators** are role/text-first with an optional CSS `fallback`.
- **`success_checkpoint`** is derived from the final page's own heading, not the literal final URL — a URL would only match a replay that reproduces the exact same per-run path segment (a member ID, a generated ID). Re-running `discover` bumps the version instead of overwriting.
- **`status: "draft" | "approved" | "rejected"`** (default `"approved"`, existing artifacts parse unchanged). Ensures an unreviewed version — a fresh discovery, or a proposed drift patch — can never silently become what unattended replay loads. Only `approve`/`reject`/`retire` change it.

## Determinism & error handling

Replay (`comp_use/replay/engine.py`) never calls an LLM — same artifact, same params, same actions, same order. Five outcomes (`OutcomeType`):

1. **`validation_error`** — required params missing; returns before any browser opens.
2. **`success`** — all steps ran, checkpoint held; `extract` values returned in `outputs`.
3. **`business_outcome`** — a legitimate non-success result (e.g. "insufficient funds"). Declared per-artifact as `outcome_patterns`, checked before every step — not only after a checkpoint miss, since the app can diverge mid-sequence — and before falling back to `hard_failure`. **Known limitation:** the LLM never populates `outcome_patterns` itself; every entry is hand-authored. **Fixed:** re-discovery used to silently drop hand-authored patterns the moment a new version saved; `_run_discover` now carries them forward from the previous version automatically.
4. **`recoverable`** — a transient, dismiss-and-retry condition. Both risky flows declare one for a stale/double-submitted review token — a real mock-app crash (`KeyError`/500) fixed to render "Session Expired" instead.
5. **`hard_failure`** — checkpoint miss, or a raised action, with no matching pattern. Result includes `step_index`, `expected`, `observed`.

**Crash-proofing.** Every external call in both engines — `surface.act()`, `Guardrail.check_allowlist()`, `surface.screenshot()`, the LLM call itself — is wrapped; nothing crashes the CLI with a raw traceback. Built from real failures, not assumed correct:

- A bad ARIA role in a real model's decision hung the process → wrapped `surface.act()`, now retries via the vision fallback.
- `check_allowlist()` raised outside its try/except → moved inside; `AllowlistViolation` now returns `HARD_FAILURE`.
- `surface.screenshot()` was unprotected at 8 call sites → one shared `safe_screenshot()` helper, used everywhere including `EscalationController`'s before/after capture (the single most safety-critical site — a screenshot failure there must never block the human from being notified).
- The LLM call itself was unwrapped → a real malformed JSON response crashed a run; fixed with a tolerant parser fallback *and* an outer try/except. Both real failures committed as evidence (`evidence/discover_1787177243`, `evidence/discover_1787177306`).

Failure details are `f"{type(exc).__name__}: {exc}"`, not a bare message, so a genuine code bug and an expected environmental failure (a Playwright timeout) stay distinguishable.

**Two LLM providers.** `OpenRouterClient` and `NvidiaNimClient` share a base; `FallbackLLMClient` tries the primary and retries the other on any failure. `MODEL_PROVIDER` picks which is primary; with no `NVIDIA_API_KEY`, behavior is unchanged. Live-verified both directions: a real NVIDIA timeout fell back to OpenRouter, hit a real rate limit, then succeeded back on NVIDIA.

**Drift-aware self-healing replay** (`--diagnose-drift-on-failure`, optional, beyond the assignment's requirements): on a `hard_failure` where a step's own locator didn't resolve (not a checkpoint mismatch), a vision model checks whether the control just moved/renamed. If found, a **new** artifact version is proposed with only that step's locator patched, and a human is escalated to review it — never auto-applied. `ReplayEngine` itself stays fully LLM-free; diagnosis is a separate module (`comp_use/drift.py`) invoked by the CLI only after replay's own result already exists. Live-verified end to end against a deliberately typo'd locator, including a real malformed model response along the way: `evidence/replay_1787173876` (malformed reply, logged honestly, not discarded) → `evidence/replay_1787174182` (fixed, patch proposed as v2) → `evidence/replay_1787174265` (replay succeeds on v2).

## Heterogeneity & multi-tenant

Designed, not built as extra runtimes (§9):

- **Surface** — swap `PlaywrightSurface` for a desktop accessibility API; `locator.strategy` can grow `native_id` without touching replay/guardrails. *Proven*: the vision fallback (below) is a tested instance of the same "perception needs a fallback" idea a desktop `Surface` would rely on.
- **Multi-tenant** — a `variant_overrides` sparse diff per tenant on a base artifact. *Not proven* — no second tenant has ever run, no `variant_overrides` field exists today. Open failure modes: an override could pass authoring but fail a specific tenant's live app (nothing validates it before shipping); rollback across a fleet of overrides isn't designed.
- **Drift** — sampled replay per tenant would flag a stale variant. *Partially proven* — the single-artifact, human-gated *repair* half is built (above); the fleet-wide *sampling/scheduling* half (which tenants, how often, false-positive/negative handling) is not.

**Vision fallback** — the concrete, testable slice of "apps without a usable DOM tree": when `DiscoveryAgent` can't produce a valid locator from the accessibility tree alone, the next decision retries with a screenshot, routed to a separate vision-capable model (most free-tier text models silently reject image content, so this needs a deliberate model switch). Live-verified against a real OpenRouter vision model with a deliberately under-described tree.

## Escalation & handoff

`EscalationController` holds `control` in `{agent, human, none}`. `escalate()` notifies the transport, blocks on `wait_for_resume()`, returns to `agent`. Every `InterventionRequest` carries a real screenshot.

`LocalSharedBrowserTransport` is the built transport: print the request, wait for the operator to type `resume` in the **same headed browser window**. (`ServerStreamingTransport` — a remote operator console — is designed only, see Cuts.)

**Discovery escalates on all three §3.6 triggers**, not just replay:
1. A `risky` step — checked *before* the action runs, so it never happens unconfirmed.
2. `max_steps` exhausted without `finish`.
3. Three consecutive skipped/invalid decisions — fires mid-run, not just at the end.

**What changed during the handoff is captured, not just that it happened.** When a `Surface` is available, `escalate()` captures the accessibility tree and a screenshot immediately before and after `wait_for_resume()`, diffs the trees, and logs both alongside the operator's own one-line free-text note on a single `escalation_human_action` event. A full click-by-click co-browsing recorder is out of scope (§3.6's own carve-out); this is the "some record" the assignment does require.

## Safety

- **Allowlist** — `allowed_url_prefixes`/`allowed_action_types`, enforced by `Guardrail.check_allowlist` in both engines.
- **Risk tiers** — `Guardrail.requires_confirmation()` is the single source of truth, called from both engines. A click is risky when its control name contains "confirm"/"delete" — the commit action, not the navigation link toward it (an earlier version had this backwards).
- **Redaction** — one function, `Guardrail.redact`, applied everywhere real data reaches an output: JSONL persistence *and* console `print()` (found live that only the log was covered, not the terminal). Patterns cover the mock bank's structured IDs (`ACC-`, `SUB-`, `TXN-`, `CONF-`) and dollar amounts. **Deliberate scope decision:** the bare `member_id` is *not* redacted (matches the assignment's own open usage of `"12345"`) — these are hand-picked patterns for this app's ID shapes, not a general PII detector.
- **No destructive operations, anywhere.** Every versioning-adjacent operation (artifact versions, drift patches, the capability server's `reject`/`retire`) only flips state or adds a new version — nothing removes a file. The capability server deliberately has **no hard-delete endpoint**: an unauthenticated destructive endpoint on regulated financial data is a materially different risk than a missing feature. The deferred design (per-API-key `admin`/`operator` roles) is documented, not built.

Hosted OpenRouter means redacted UI text still leaves the machine on `discover`. Point `LLMClient` at a local model for production locality.

## Cuts

**Which §8 stretch goals, and why not more.** Two, both grown from existing primitives rather than new subsystems: **Assisted fallback** (drift-aware self-healing replay) and **Agent-facing capability interface** (the capability server, plus the narrow "gate unattended replay on an approval state" slice of Confidence & approval — taken on because exposing `discover` over HTTP without it would leave a real safety gap). Not attempted: code generation, cross-tenant canonicalization, multi-run stability — each would mean a third, unrelated subsystem.

Not built:
- `ServerStreamingTransport` (remote operator console) — designed only.
- Production artifact/evidence storage (DB + object store) — files on disk only.
- Desktop `Surface` and true multi-tenant execution.
- On-prem LLM for discovery — documented, not built.
- Auth on the capability server, and hard delete — designed (per-key admin/operator roles), deliberately not built until auth exists.
- Screenshot as the *primary* perception channel — not planned; it's a targeted fallback only.

**What we'd build next:** (1) capability-server auth — unblocks safely exposing `discover`/`approve` beyond one trusted operator; (2) `variant_overrides` + a real second tenant — proves the Heterogeneity design, not just describes it; (3) structured logging; (4) `ServerStreamingTransport` — now more relevant since the server already unblocks `resume` remotely, but not remote *viewing* of the browser.

## Evidence walkthrough

Every run below is against the live mock app + real Playwright Chromium. Discovery rows use `FakeLLMClient` (scripted, same tool-call shape as the real client) unless marked "real LLM"; every replay row used `ReplayEngine` with no LLM.

| Scenario | Result | Evidence |
|---|---|---|
| Discovery, all 3 capabilities | Succeeded end to end through the decoy panel + confirm interstitial | `evidence/discover_1787149758_open_sub_account/`, `evidence/discover_1787149762_transfer_funds/`, `evidence/discover_1787105308_lookup_member/` |
| Replay `success` (different member than discovery used — proves generalization) | `{"outcome": "success"}` | `evidence/replay_1787105466/` |
| Replay `success` with output | `{"outcome": "success", "outputs": {"confirmation_number": "CONF-000001"}}` | `evidence/replay_1787149826/` |
| Replay `business_outcome` (no such member) | `{"outcome": "business_outcome", "detail": "no_such_member"}` | `evidence/replay_1787105468/` |
| Replay `business_outcome` (mid-sequence divergence) | `{"outcome": "business_outcome", "detail": "insufficient_funds"}` | `evidence/replay_1787105611/` |
| Replay `hard_failure` | `{"outcome": "hard_failure", "step_index": 3, ...}` + `final.png` | `evidence/replay_1787150562/` |
| Replay `validation_error` | Returns before any browser opens | `replay --capability-name lookup_member --params "{}"` |
| Replay `recoverable` (stale review token) | Real page renders "Session Expired"; checkpoint matches | See Determinism section above |
| Discovery, real LLM (non-scripted) | Real OpenRouter tool-call shape, `lookup_member` end to end | `evidence/discover_1787097059/` |
| Discovery, real LLM, checkpoint-fix verification | `v2.json` generalizes to a different member; `v1.json` untouched | `evidence/discover_1787154166/` |
| Capability server, real HTTP end to end (discover → draft → approve → invoke, plus escalate → resume) | All real: mock app, browser, OpenRouter call | `evidence/discover_1787337829/`, `evidence/replay_1787337971/` |

To re-run live discovery yourself: set `OPENROUTER_API_KEY` and follow the README Demo path.
