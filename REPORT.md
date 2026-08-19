# REPORT — Computer-Use Automation System

This report maps the implemented system to
[docs/superpowers/specs/2026-08-17-computer-use-automation-design.md](docs/superpowers/specs/2026-08-17-computer-use-automation-design.md).
Concrete files are named under each heading.

## Architecture

Single Python process. Five components sit behind small interfaces; only discovery
talks to an LLM.

| Component | Role | Implementation |
|---|---|---|
| Mock app | Legacy-styled Flask bank UI | `mock_app/app.py`, `mock_app/data.py`, `mock_app/templates/` |
| Discovery agent | Observe → decide → act; compile artifact | `comp_use/discovery/agent.py`, `comp_use/discovery/compiler.py` |
| Replay engine | Deterministic step execution, no LLM | `comp_use/replay/engine.py` (must not import `LLMClient`) |
| Guardrail | Allowlist, risk confirmation, redaction | `comp_use/guardrail.py` |
| Escalation | `control` state + operator handoff | `comp_use/escalation/controller.py`, `comp_use/escalation/transport.py` |

Perception and action go through `Surface` (`comp_use/surface.py`): accessibility-tree
`observe()` (via Playwright's `aria_snapshot()`), role/text/CSS `act()`, checkpoint
checks, screenshot. `PlaywrightSurface` is the built implementation. CLI wiring is
`comp_use/cli.py`; settings in `comp_use/config.py`.

**The mock app is deliberately hostile, per §4's "intentionally hostile surface"
option.** `mock_app/templates/` has no test IDs anywhere; every page nests a layout
table inside a content table plus an unrelated decoy "Recent Activity" table and a
literal `<iframe>` widget (both inert noise `Surface` never needs to enter, since
Playwright's page-level locators only see the top frame — a live demonstration that
role/text locators are naturally frame-scoped, not that we built frame-aware
resolution); form fields use plain `<label for>` association instead of `aria-label`;
the search page carries a disabled "Advanced Search" panel with its own,
similarly-named "Advanced Search" button so a naive substring-matched locator for
"Search" resolves ambiguously. That last one caught a real bug: Playwright's
`get_by_role(name=...)` substring-matches by default, so `_resolve()` in
`comp_use/surface.py` now passes `exact=True` — otherwise "Search" matches "Advanced
Search" too and replay throws a strict-mode violation instead of clicking the right
control. Both risky flows (open a sub-account, transfer funds) also require an extra
"Are you sure?" review/confirm step before committing, which is what
`OutcomePattern`/mid-sequence detection below exists to handle cleanly.

**Assumption: the caller picks discover vs. replay, by name.** This system does not
infer intent from a natural-language goal and decide for itself whether to discover
or replay — the CLI takes an explicit `--capability-name` either way. We assume that
decision belongs to the upstream agent-facing product, per the brief's own framing:
*"the agent-facing product decides what to do; this system is how it reliably and
safely does it"* (assignment §1). A router that maps a fuzzy goal to an existing
artifact is one read of the optional "agent-facing capability interface" stretch goal
(§8) — and even that stretch goal is phrased as invoke-by-name, not
infer-and-auto-select — so we treated it as out of scope rather than a gap.

## Artifact schema

Typed Pydantic models in `comp_use/schemas.py`. A capability is versioned JSON at
`artifacts/<capability_name>/v<N>.json`. Example:
[`artifacts/lookup_member/v1.json`](artifacts/lookup_member/v1.json).

- `input_schema` is **derived**, not inferred after the fact: each `type_text` /
  `select_option` during discovery carries a `value_source` of `goal_parameter` or
  `fixed`. `compile_artifact()` collects `goal_parameter` names into `input_schema`.
- Locators are role/text-first with an optional CSS `fallback`.
- Each `Step` has `risk_tier` (`safe` / `risky`) and an optional per-step
  `checkpoint`. The artifact also has a final `success_checkpoint`.

## Determinism & error handling

Replay (`comp_use/replay/engine.py`) never calls an LLM. Same artifact + same params
attempt the same actions in the same order. Outcomes (`OutcomeType` in
`comp_use/schemas.py`):

1. **`validation_error`** — required params missing/malformed; no browser interaction.
2. **`success`** — all steps ran; `success_checkpoint` held.
3. **`business_outcome`** — a legitimate, expected non-success UI result (e.g.
   "insufficient funds" on the mock transfer form). An `Artifact` carries
   `outcome_patterns: list[OutcomePattern]` — each pairs a `Checkpoint` with the
   outcome it means. Whenever a per-step or the final `success_checkpoint` fails to
   match, `ReplayEngine._match_outcome_pattern` checks these *before* falling back to
   `hard_failure`; if one matches, its outcome/detail is returned instead. This is the
   mechanism that keeps "no such member"/"insufficient funds" from being reported as a
   crash — see the evidence walkthrough below for a real run that hits it.
4. **`recoverable`** — same `outcome_patterns` mechanism, reserved for
   dismiss-and-retry conditions; no capability in this build declares one (nothing in
   the mock app produces a dismissable interstitial), so it's wired but unexercised.
5. **`hard_failure`** — checkpoint miss, or an action that raised (missing element,
   timeout), with no matching outcome pattern; result includes `step_index`,
   `expected`/`detail`, `observed`.

Outcome patterns are checked **before every step is attempted**, not only after a
checkpoint miss — the app can diverge onto a business-outcome page mid-sequence
(e.g. "insufficient funds" appears after step 7 of `transfer_funds`, but step 8 is
still "click Confirm Transfer," a control that page never renders). Blindly attempting
step 8 timed out with a raw Playwright exception the first time this was tested for
real; `ReplayEngine.run` now re-checks `outcome_patterns` at the top of every loop
iteration and also wraps `surface.act` in try/except, re-checking outcome patterns
before falling back to `hard_failure` with the exception text as `detail`. No action
in this system is ever allowed to crash the CLI with a raw traceback — everything
resolves to one of the five `ReplayResult` outcomes.

## Heterogeneity & multi-tenant

Designed, not built as extra runtimes (spec §9). The seams that would change:

- **Surface** — swap `PlaywrightSurface` for a desktop accessibility API; artifact
  `locator.strategy` can grow `native_id` without touching replay/guardrails.
- **Multi-tenant** — locator values are the tenant-specific part; a future
  `variant_overrides` map would be a sparse diff on a base artifact.
- **Drift** — sampled replay of checkpoints per tenant would flag a stale variant.

Nothing in this repo executes more than one mock tenant.

## Escalation & handoff

`EscalationController` (`comp_use/escalation/controller.py`) holds `control` in
`{agent, human, none}`. `escalate()` sets `human`, logs the `InterventionRequest`,
notifies the transport, blocks on `wait_for_resume()`, then returns to `agent`.

`LocalSharedBrowserTransport` (`comp_use/escalation/transport.py`) is the built
transport: print the request, wait for the operator to type `resume` while they use
the **same headed Playwright window**. Replay wires this in when a step is `risky`
and `confirm_risky` is false.

`ServerStreamingTransport` (CDP screencast / click proxy) is designed only — see
Cuts.

## Safety

- **Allowlist** — `Settings.allowed_url_prefixes` defaults to
  `http://localhost:5000`; `allowed_action_types` is the five UI actions. Enforced in
  `Guardrail.check_allowlist` on discovery and replay.
- **Risk tiers** — `requires_confirmation(step, confirm_risky)`; CLI
  `--confirm-risky` skips the pause. `DiscoveryAgent._classify_risk` flags a click
  risky when its control name contains "confirm" or "delete" — i.e. the control that
  actually commits an irreversible change, not the link/button that merely navigates
  toward it. (An earlier version matched "transfer"/"sub-account" against the
  navigation link instead of the commit button — backwards, since the link is fully
  reversible and the commit isn't. Adding the confirm-interstitial step surfaced
  this and it's now fixed.)
- **Redaction** — one function, `Guardrail.redact`, used before LLM-bound text would
  be logged and before JSONL persistence (`comp_use/evidence.py` walks strings in the
  event payload). Patterns: 9–12 digit IDs and `$1,234.56`-style amounts.

Hosted OpenRouter still means redacted UI text leaves the machine on **discover**.
Point `LLMClient` at a local model for production locality; `FakeLLMClient` is the
test double.

## Cuts

Unchanged from spec §11:

- `ServerStreamingTransport` — designed, not built.
- Production artifact/evidence storage (DB + object store + approval) — not built;
  files on disk only.
- Desktop `Surface` and true multi-tenant execution — not built.
- On-prem LLM — documented, not built; OpenRouter is the live discover path.
- Agent-facing NL-to-params product in front of replay — out of scope; replay takes
  JSON params.

## Evidence walkthrough (captured 2026-08-18, against the hardened mock app)

All runs below are against the live mock app at `http://localhost:5000` (hostile
markup: nested/decoy tables, an iframe widget, no `aria-label`, a disabled
"Advanced Search" decoy, confirm-interstitial steps on both risky flows — see
Architecture) and real Playwright Chromium. Discovery evidence used
`FakeLLMClient` (deterministic script, same tool-call shape as `OpenRouterClient`);
every replay below used `ReplayEngine` with **no LLM**.

### Discovery (all three capabilities)

- `evidence/discover_1787105308_lookup_member/log.jsonl` →
  `artifacts/lookup_member/v1.json`
- `evidence/discover_1787105310_open_sub_account/log.jsonl` →
  `artifacts/open_sub_account/v1.json` (includes the "Confirm Sub-Account" review
  step)
- `evidence/discover_1787105313_transfer_funds/log.jsonl` →
  `artifacts/transfer_funds/v1.json` (includes the "Confirm Transfer" review step)

All three succeeded (`trace.succeeded=True`) end to end through the label-based
locators, the decoy panel, and the review/confirm interstitial.

### Replay — success

- `lookup_member`, `member_id=67890` (a different member than any discovery used —
  confirms the artifact generalizes): `evidence/replay_1787105466/log.jsonl` →
  `{"outcome": "success"}`
- `open_sub_account`, `deposit_amount=250`:
  `evidence/replay_1787105470/log.jsonl` → `{"outcome": "success"}`
- `transfer_funds`, `amount=50`: `evidence/replay_1787105473/log.jsonl` →
  `{"outcome": "success"}`

### Replay — `business_outcome`

- `lookup_member`, `member_id=00000` (no such member):
  `evidence/replay_1787105468/log.jsonl` →
  `{"outcome": "business_outcome", "detail": "no_such_member"}`
- `transfer_funds`, `amount=999999` (exceeds balance):
  `evidence/replay_1787105611/log.jsonl` →
  `{"outcome": "business_outcome", "detail": "insufficient_funds"}` — this is the
  mid-sequence case: the app diverges onto the "Insufficient funds" page after step
  7 of 9, so the recorded step 8 ("click Confirm Transfer") is never attempted; see
  Determinism & error handling.

### Replay — `hard_failure`

- `open_sub_account`, `member_id=00000` (nonexistent member, no declared outcome
  pattern for this capability): `evidence/replay_1787105625/log.jsonl` →
  `{"outcome": "hard_failure", "step_index": 3, "detail": "Locator.click: Timeout
  30000ms exceeded...", "observed": "http://localhost:5000/member/search?member_id=00000"}`
  — a genuine automation failure reported with enough detail to debug, distinct from
  the business outcomes above.

### Replay — `validation_error` (no browser)

- `python -m comp_use.cli replay --capability-name lookup_member --params "{}"` →
  `{"outcome": "validation_error", "detail": "missing required param 'member_id'"}`,
  returned before any `surface.act` — the CLI skips launching Chromium entirely on
  this path.

All five `OutcomeType` values reachable by replay are demonstrated above except
`recoverable` (see Determinism & error handling — no capability declares one, since
nothing in the mock app produces a dismissable interstitial distinct from the
always-present confirm step).

To re-run **live** OpenRouter discovery instead of `FakeLLMClient`, set
`OPENROUTER_API_KEY` and follow README Demo path. Real OpenRouter-driven discovery
runs against a free-tier model also exist locally under `evidence/discover_*` from
earlier development (not committed) — committing one as the canonical "real LLM"
evidence, rather than the `FakeLLMClient` runs above, is a known open item.
