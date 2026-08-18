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
`observe()`, role/text/CSS `act()`, checkpoint checks, screenshot. `PlaywrightSurface`
is the built implementation. CLI wiring is `comp_use/cli.py`; settings in
`comp_use/config.py`.

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
3. **`business_outcome`** — reserved for UI-reported non-success (e.g. insufficient
   funds on the mock transfer form). Not yet classified automatically in replay; the
   mock app still renders that state for discovery/demo.
4. **`recoverable`** — reserved for dismiss-and-retry; not wired in this build.
5. **`hard_failure`** — checkpoint miss; result includes `step_index`, `expected`,
   `observed`.

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
  `--confirm-risky` skips the pause.
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

## Evidence walkthrough (captured 2026-08-17)

These runs used the live mock app at `http://localhost:5000` and Playwright
Chromium. Discovery used `FakeLLMClient` with the same tool-call shape as OpenRouter
(no API key in this environment). Replay used `ReplayEngine` with **no LLM**.

### 1. Discovery — `lookup_member`

- Goal: *Look up member 12345 and view their account balances*
- Evidence: `evidence/discover_1787012923/log.jsonl` + `final.png`
- Outcome: `succeeded=True`, landed on `http://localhost:5000/member/12345`
- Artifact: `artifacts/lookup_member/v1.json` — `input_schema: [member_id]`, steps
  navigate → type_text (goal_parameter `member_id`) → click Search, success
  checkpoint `url_matches` `member/12345`

### 2. Replay — success

- Command equivalent: `python -m comp_use.cli replay --capability-name lookup_member --params "{\"member_id\": \"12345\"}"`
- Evidence: `evidence/replay_1787012925/log.jsonl` + `final.png`
- Result: `{"outcome": "success"}` after navigate, type_text, click

### 3. Replay — validation_error (no browser)

- Command: `python -m comp_use.cli replay --capability-name lookup_member --params "{}"`
- Evidence: `evidence/replay_1787012926_validation/log.jsonl`
- Result: `{"outcome": "validation_error", "detail": "missing required param 'member_id'"}`
- The engine returns before any `surface.act`; the CLI also skips launching Chromium
  on this path.

To re-run **live** OpenRouter discovery, set `OPENROUTER_API_KEY` and follow README
Demo path.
