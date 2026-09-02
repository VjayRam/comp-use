# Computer-Use Automation System

[![Tests](https://github.com/VjayRam/comp-use/actions/workflows/tests.yml/badge.svg)](https://github.com/VjayRam/comp-use/actions/workflows/tests.yml)

A system that uses an LLM to discover how to accomplish a goal by driving a live web
UI, records the successful run as a typed, versioned, reusable capability artifact,
and replays that artifact deterministically (no LLM in the decision loop) with typed
inputs/outputs, safety guardrails, and human-in-the-loop escalation.

Built for the interface.ai take-home assignment
(`Assignment A — Computer-Use Automation System.pdf`). **Status: implementation
complete.** Setup and demo commands below; full design rationale is in
[REPORT.md](REPORT.md) and the [design specs](docs/design/specs/).

## What this will do

1. Take a natural-language goal + target app.
2. Run an LLM-driven discovery agent that drives a real (mock, legacy-styled) bank
   back-office web app via Playwright, using an accessibility-tree view of the page
   (screenshots are captured as evidence, not fed to the LLM's decision loop — see
   REPORT.md's Cuts).
3. Record a successful run as a versioned JSON capability artifact (typed inputs,
   typed outputs, per-step locators and checkpoints).
4. Replay that artifact deterministically against new inputs, with no LLM call,
   detecting and classifying runtime outcomes (success / business outcome /
   recoverable / hard failure / validation error).
5. Escalate to a human and hand over the *same* live browser session when the agent or
   a replay run can't safely proceed on its own.
6. Enforce an allowlist, risk-tiered action handling, and redaction of sensitive data
   before it's sent to the LLM or persisted anywhere.
7. **Optional:** on a replay failure, diagnose whether the target control just
   drifted (moved/renamed) rather than genuinely broke, and propose a patched
   artifact version for human review — never auto-applied. See "Drift-aware
   self-healing replay" in [REPORT.md](REPORT.md#determinism--error-handling).
8. **Optional:** expose saved capabilities to an AI agent over a REST API —
   discover/invoke/approve/reject/retire, with the same human-escalation model
   signaled over HTTP instead of a terminal prompt. See
   [Capability server](#capability-server-optional-agent-facing-api) below.

## System design

```mermaid
flowchart LR
    Goal["Goal\n(discovery)"] --> Agent["Discovery Agent\n(LLM-driven)"]
    Params["Typed params\n(replay)"] --> Replay["Replay Engine\n(no LLM)"]

    Agent --> Artifact[("Capability\nArtifact")]
    Artifact --> Replay

    Agent --> Surface["Surface\n(drives the app)"]
    Replay --> Surface
    Surface --> App["Mock legacy\nbank app"]

    Agent -.stuck.-> Human["Human operator\n(takes over live session)"]
    Replay -.stuck.-> Human

    Agent --> Evidence[("Evidence & logs")]
    Replay --> Evidence
```

A **discovery** run takes a natural-language goal, uses an LLM to drive the mock app
through the Surface, and on success saves a reusable **capability artifact**. A
**replay** run takes that artifact plus typed params and re-runs it deterministically
— no LLM involved — through the same Surface. Either path can hand control of the
live session to a **human operator** if it gets stuck, and both leave an evidence
trail behind. (Guardrails — allowlisting, risk tiers, redaction — apply throughout
but are omitted here for readability; see the design spec for the full picture.)

## Production-scale design (not built — see [Cuts](docs/design/specs/computer-use-automation-design.md#11-cuts-explicit-for-reportmd-7))

This repo is single-tenant, single-process. At the assignment's described scale
(hundreds of tenants, ~20 apps each, many sharing the same vendor product), the same
components would become services:

```mermaid
flowchart LR
    AIAgent["AI agent product\n(caller, per tenant)"] --> API["Capability API\n(catalog + invoke)"]

    API --> ReplayWorkers["Replay workers\n(pooled, per-tenant sessions)"]
    ReplayWorkers --> TenantApps["Tenant app instances\n(same vendor product,\nmany tenants)"]

    Discovery["Discovery runs\n(on new/changed capability)"] --> LocalLLM["On-prem / local LLM\n(no UI data leaves tenant env)"]
    Discovery --> ArtifactDB[("Artifact store\nbase + per-tenant overrides")]
    ReplayWorkers --> ArtifactDB

    ReplayWorkers --> EvidenceStore["Evidence & log store\n(redacted, per-tenant isolated)"]

    ReplayWorkers -.stuck.-> OperatorConsole["Operator console\n(streamed session handoff)"]

    DriftCheck["Drift detector\n(sampled replays)"] --> ArtifactDB
    DriftCheck -.flags stale variant.-> Discovery
```

Key differences from the built version:

- **Discovery becomes rare, replay becomes the hot path** — most traffic is capability
  *invocation*, triggered by the AI agent product; discovery only runs on a new or
  drifted capability.
- **One base artifact per vendor app, with per-tenant overrides**, not one recording
  per tenant — a sparse `variant_overrides` diff (spec §9), so onboarding a tenant on
  an already-known vendor product doesn't mean re-recording from scratch.
- **On-prem/local LLM for discovery** — the only LLM-touching path, on regulated
  financial data, so it'd run against a locally-hosted model in production (spec §8).
- **A real operator console** — a streamed session (`ServerStreamingTransport`,
  spec §7) instead of the local shared-browser handoff, so an operator can be anywhere.
- **A drift detector** — samples replays per tenant/variant and flags stale checkpoints
  before a production failure surfaces them.

Per the assignment's own guidance, none of this is built — designing the abstractions
so they *could* scale this way (`Surface`, canonicalized locators, `ControlTransport`,
the outcome taxonomy) is the deliverable; the services/queues/pooling above are not.

## Setup

Use **Python 3.11 or 3.12** (Playwright 1.47's `greenlet` pin fails on 3.13). From the
repo root, with `pip`:

```bash
python -m venv .venv
# activate it: Windows: .venv\Scripts\activate    Unix: source .venv/bin/activate
pip install -r requirements.txt && playwright install chromium
copy .env.example .env   # Windows; on Unix: cp .env.example .env
```

Or with [`uv`](https://docs.astral.sh/uv/):

```bash
uv venv
uv pip install -r requirements.txt
uv run playwright install chromium
copy .env.example .env   # Windows; on Unix: cp .env.example .env
```

Every command later in this README (`python -m pytest`, `python -m comp_use.cli ...`,
`python run_mock_app.py`) works the same way under `uv` — just prefix it with `uv run`
(e.g. `uv run python -m pytest -v`), or activate `.venv` first
(`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on Unix) and drop
the prefix entirely.

Edit `.env` and set `OPENROUTER_API_KEY` (required only for `discover`; replay never
calls the LLM). `OPENROUTER_VISION_MODEL` has a working default and rarely needs
changing — it's a fallback for when the accessibility tree alone isn't enough for
the model to locate an element (see REPORT.md's Heterogeneity section).

**Optional second provider.** Set `NVIDIA_API_KEY` ([build.nvidia.com](https://build.nvidia.com))
to add NVIDIA NIM as an automatic fallback on any LLM-call failure (rate limit,
timeout, malformed response). Leave it blank to disable — behavior is then identical
to OpenRouter-only. `MODEL_PROVIDER` (`openrouter` default, or `nvidia`) picks which
provider is tried first; if the chosen primary has no key, it falls back to whichever
provider does rather than build a client guaranteed to fail. Live-verified both
directions — see `FallbackLLMClient` in `comp_use/llm_client.py`.

Run tests:

```bash
python -m pytest -v
```

On Windows with the project venv: `.venv\Scripts\python -m pytest -v`

Optional: `COMP_USE_HEADLESS=1` (or PowerShell `$env:COMP_USE_HEADLESS="1"`) launches
Chromium headless instead of a visible window.

**Running without live services.** `python -m pytest -v` is fully self-contained — no
API key, no manually-started mock app (each test spins up its own throwaway Flask
instance and uses `FakeLLMClient`, a scripted stand-in, wherever a real LLM call would
happen). `replay` also never calls an LLM, so the checked-in example artifacts let you
run the Demo path's replay commands below with only the mock app running, skipping
`discover` entirely. Only `discover` (and `--diagnose-drift-on-failure`) needs a live
key and a live mock app — the one path the assignment requires be genuinely real
(§4: "the discovery run has to be real").

## Demo path

Leave the mock app running in one terminal, then discover / replay in another.

**Terminal 1 — mock bank app** (`http://localhost:5000`):

```bash
python run_mock_app.py
```

**Terminal 2 — discover** (needs `OPENROUTER_API_KEY`; opens a headed browser unless
`COMP_USE_HEADLESS=1`):

```bash
python -m comp_use.cli discover \
  --goal "Look up member 12345 and view their account balances" \
  --start-url "http://localhost:5000/member/search" \
  --capability-name lookup_member
```

Expected: `Saved artifact to artifacts/lookup_member/v1.json` and
`evidence/discover_<timestamp>/log.jsonl`.

A checked-in example already exists at `artifacts/lookup_member/v1.json` (captured
against this mock app). You can skip discover and go straight to replay.

**Replay a successful lookup** (no LLM):

```bash
python -m comp_use.cli replay --capability-name lookup_member --params "{\"member_id\": \"12345\"}"
```

PowerShell:

```powershell
.venv\Scripts\python -m comp_use.cli replay --capability-name lookup_member --params '{"member_id": "12345"}'
```

Expected: `ReplayResult` JSON with `"outcome": "success"` and
`evidence/replay_<timestamp>/`.

**Replay with a missing param** (validation error — no browser window):

```bash
python -m comp_use.cli replay --capability-name lookup_member --params "{}"
```

Expected: `"outcome": "validation_error"` and detail `missing required param 'member_id'`.

Risky steps (opening a sub-account, transferring funds) pause for human
confirmation on both `discover` and `replay` unless you pass `--confirm-risky`.
Type `resume` in the CLI when you have finished in the shared browser window,
then a one-line note describing what you did (optional — press Enter to skip it).

**Drift-aware self-healing replay** (optional — see REPORT.md for the full
write-up and live evidence): add `--diagnose-drift-on-failure` to `replay`. If
a step's recorded control genuinely can't be found (not a checkpoint mismatch),
a vision model checks whether it just moved/got renamed, and — only if it finds
a plausible match — saves a patched artifact as a new version and escalates for
human review. Nothing is auto-applied; the failed run's own result is unchanged.

```bash
python -m comp_use.cli replay --capability-name transfer_funds \
  --params "{\"member_id\": \"12345\", \"from_account\": \"ACC-001\", \"to_account\": \"ACC-002\", \"amount\": \"25\"}" \
  --confirm-risky --diagnose-drift-on-failure
```

## Capability server (optional, agent-facing API)

Beyond the CLI: a REST API that lets an AI agent discover and invoke saved
capabilities by name with typed args (the assignment's §8 "agent-facing capability
interface" stretch goal), plus a `draft`/`approved`/`rejected` status so an unreviewed
version can never silently become what unattended `invoke` picks up. Full design,
including what's deliberately *not* built (auth, hard delete):
[agent-facing-capability-server-design.md](docs/design/specs/agent-facing-capability-server-design.md).

**Terminal 1 — mock bank app** (as above): `python run_mock_app.py`

**Terminal 2 — capability server:**

```bash
COMP_USE_HEADLESS=1 python -m comp_use.cli serve --port 8000
```

**Terminal 3 — drive it over HTTP.** `discover`/`invoke` return immediately with a
`run_id`; poll `GET /runs/{run_id}` for status (`running` → `escalated` → `done`, or
straight to `done`). This mirrors the CLI's `discover`/`replay`, just over HTTP with
the browser running on the server's machine (headed there, not on the caller's):

```bash
# 1. Discover a new capability - lands as a DRAFT, not immediately usable
curl -s -X POST http://localhost:8000/capabilities/lookup_member_api_demo/discover \
  -H "Content-Type: application/json" \
  -d '{"goal": "look up member 12345 and read their current savings balance", "start_url": "http://localhost:5000/member/search"}'
# -> {"run_id": "discover_1787337829532", "status": "running"}

curl -s http://localhost:8000/runs/discover_1787337829532   # poll until "done"
# -> {"status": "done", "discover_result": {"succeeded": true, "artifact_version": 1}, ...}

# 2. A draft isn't live yet - the catalog 404s until it's approved
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/capabilities/lookup_member_api_demo
# -> 404

curl -s -X POST http://localhost:8000/capabilities/lookup_member_api_demo/versions/1/approve

# 3. Now invoke it like any AI agent would - typed params in, typed outputs out
curl -s -X POST http://localhost:8000/capabilities/lookup_member_api_demo/invoke \
  -H "Content-Type: application/json" -d '{"params": {"member_id": "12345"}}'
# -> {"run_id": "invoke_1787337949337", "status": "running"}

curl -s http://localhost:8000/runs/invoke_1787337949337   # poll until "done"
# -> {"status": "done", "result": {"outcome": "success", "outputs": {"savings_balance": "8200.50"}}}
```

**Escalation over HTTP** (a risky step, e.g. `transfer_funds`, always pauses — the
API never passes `--confirm-risky`): `POST .../invoke` returns a `run_id` whose
status becomes `"escalated"`, carrying a `reason`, `current_step`, and a
`screenshot_url` served from `/evidence/...`. A human reviews it, then:

```bash
curl -s -X POST http://localhost:8000/runs/<run_id>/resume \
  -H "Content-Type: application/json" -d '{"note": "confirmed the transfer manually"}'
```

...which unblocks the same background thread mid-replay (via `QueueTransport`,
signaling instead of blocking on a terminal `input()`) and the run proceeds to
`"done"` with its real outputs, exactly like the CLI's escalation path.

**No auth exists yet** — anyone who can reach the server can invoke/discover/approve.
Deliberate, stated (spec §7, REPORT.md Safety) — and there's **no hard-delete
endpoint** at all, since an unauthenticated destructive endpoint on regulated
financial artifacts is a materially different risk than a missing feature. The
deferred design (per-API-key `admin`/`operator` roles) is documented, not built.

## Exercising every outcome (full command reference)

The Demo path above covers the basics. This section is a complete run sheet —
every `OutcomeType`, both escalation paths, versioning, and self-healing — in a
sensible order. Requires `OPENROUTER_API_KEY` set for the `discover` steps;
`replay` never needs it. Balances/counters (`ACC-001`'s balance, `SUB-000N`,
`TXN-000N`) will differ from whatever's in `REPORT.md` after you run these —
they mutate shared mock-app state, that's expected. PowerShell needs `'...'`
JSON quoting per the Demo path section above; steps involving `curl` need
bash/git-bash/WSL.

**0. Setup**

```bash
# Terminal 1 — leave running for everything below
python run_mock_app.py
```

```bash
# Terminal 2
python -m pytest -v
```

**1. Discovery — real LLM, all three capabilities**

```bash
python -m comp_use.cli discover --goal "Look up member 12345 and view their account balances" \
  --start-url "http://localhost:5000/member/search" --capability-name lookup_member

# risky flows — pause for confirmation; type `resume` + a note, or pass --confirm-risky
python -m comp_use.cli discover --goal "Open a new sub-account for member 12345 with a 500 dollar deposit and report the confirmation number" \
  --start-url "http://localhost:5000/member/search" --capability-name open_sub_account --confirm-risky

python -m comp_use.cli discover --goal "For member 12345 transfer 100 dollars from account ACC-001 to account ACC-002" \
  --start-url "http://localhost:5000/member/search" --capability-name transfer_funds --confirm-risky
```
Expected: artifacts saved as the next version for each (existing versions aren't
overwritten), plus a real `evidence/discover_<ts>/log.jsonl` per run.

**2. Discovery — risky escalation, unconfirmed** (proves discovery itself pauses, not just replay)

```bash
python -m comp_use.cli discover --goal "Open a new sub-account for member 12345 with a 500 dollar deposit" \
  --start-url "http://localhost:5000/member/search" --capability-name open_sub_account_escalation_demo
```
Expected: `[ESCALATION] step N is risk_tier=risky...` appears *before* the confirm
click happens. Type `resume`, then a note.

**3. Replay — `success`** (deliberately different params than discovery used, to prove generalization)

```bash
python -m comp_use.cli replay --capability-name lookup_member --params "{\"member_id\": \"67890\"}"
python -m comp_use.cli replay --capability-name open_sub_account --params "{\"member_id\": \"12345\", \"deposit_amount\": \"250\"}" --confirm-risky
python -m comp_use.cli replay --capability-name transfer_funds --params "{\"member_id\": \"12345\", \"from_account\": \"ACC-001\", \"to_account\": \"ACC-002\", \"amount\": \"50\"}" --confirm-risky
```
Expected: `{"outcome": "success", ...}` each time; the last two include real
`outputs` (`confirmation_number` / `transaction_id`).

**4. Replay — `business_outcome`**

```bash
python -m comp_use.cli replay --capability-name lookup_member --params "{\"member_id\": \"00000\"}"
python -m comp_use.cli replay --capability-name transfer_funds --params "{\"member_id\": \"12345\", \"from_account\": \"ACC-001\", \"to_account\": \"ACC-002\", \"amount\": \"999999\"}" --confirm-risky
```
Expected: `"outcome": "business_outcome"`, detail `"no_such_member"` /
`"insufficient_funds"`.

**5. Replay — `validation_error`** (no browser opens — see Demo path above for the single-command version)

**6. Replay — `hard_failure`**

```bash
python -m comp_use.cli replay --capability-name open_sub_account_escalation_demo --params "{\"member_id\": \"00000\", \"deposit_amount\": \"250\"}" --confirm-risky
```
Expected: `"outcome": "hard_failure"` with a `TimeoutError: ...` detail (no
`outcome_pattern` declared for this capability) — also produces
`evidence/replay_<ts>/final.png`. (Uses `open_sub_account_escalation_demo`
rather than `open_sub_account` here specifically because it has no
`outcome_patterns`; `open_sub_account` now declares a `no_such_member`
pattern from step 1, so the same params against it correctly return
`business_outcome` instead — see step 4.)

**7. Replay — `recoverable`** (triggered at the HTTP layer directly — a normal
single replay always gets a fresh review token, so this needs a manual double-submit)

```bash
LOC=$(curl -s -D - -o /dev/null -X POST http://localhost:5000/member/12345/transfer \
  -d "from_account=ACC-001&to_account=ACC-002&amount=10" | grep -i '^location' | tr -d '\r')
TOKEN=$(echo "$LOC" | grep -oE '[a-f0-9]{8}$')

curl -s -o /dev/null -X POST "http://localhost:5000/member/12345/transfer/review/$TOKEN/confirm"  # consumes the token
curl -s -X POST "http://localhost:5000/member/12345/transfer/review/$TOKEN/confirm" | grep -i "Session Expired"  # stale
```
Expected: `Session Expired` in the second response — the exact page
`transfer_funds`'s `recoverable` `OutcomePattern` matches on. This only shows the raw
page state, not `ReplayEngine`'s reaction to it (a normal `comp-use replay` always
gets a fresh token, so it can't naturally reach this state mid-run) — see step 8 for
that, which exercises the engine itself against this exact condition.

**8. Locator fallback + `recoverable` auto-retry** (the two most recently added
robustness features — a working fallback locator, and a bounded, automatic retry for
`recoverable` outcomes instead of only ever detecting-and-reporting them)

```bash
python demo_edge_cases.py
```

This drives a real headless Chromium against the real mock app (no LLM, no API key
needed) and prints two parts:

- **Part A — `Locator.fallback` (`comp_use/surface.py`).** Clicks using a `Locator`
  whose primary strategy (`role=button name="This Button Does Not Exist"`) doesn't
  resolve on the page at all; `_resolve_with_fallback` falls through to the declared
  `.fallback` locator (the real "Search" button) instead of raising. **Look for:**
  `primary locator was bogus, fallback resolved to the real Search button: PASS` and
  a final URL of `.../member/12345` — proof the fallback locator, not the primary,
  actually drove the click.
- **Part B — `OutcomePattern` retry (`comp_use/replay/engine.py`).** Manufactures a
  real stale review token (confirms a sub-account once for real, then re-visits the
  *same* now-consumed review URL — exactly what an accidental double-submit or a
  stale bookmark produces) and exercises `ReplayEngine`'s retry logic directly
  against that real page in two ways:
  - **B1** uses the checked-in `open_sub_account` artifact completely unmodified.
    **Look for:** three lines reading `retry #1/2/3 attempted...`, then `gave up
    after 3 retries -> outcome=recoverable detail='session_expired'` — proof the new
    default (`OutcomePattern.max_retries=3`) applies automatically even to an
    artifact whose pattern predates the field. Then open the printed evidence log
    path (`evidence/demo_recoverable_default_<ts>/log.jsonl`) and confirm it
    contains exactly three `"event_type": "recoverable_retry"` lines with
    `"attempt": 1, 2, 3`.
  - **B2** attaches a `recovery_action` pointed at the app's real `"Start over"`
    link (the one actually rendered on its `session_expired.html`) and re-runs
    against the same stale page. **Look for:** `_should_retry performed the REAL
    recovery_action...: True`, a changed `url after recovery_action ran` (now
    `.../sub-account/new`, not the review page), and `condition still matches after
    recovery: False` — proof the recovery action was a real click against a real
    element that genuinely cleared the matched condition, not just a label change.
    Its evidence log (`evidence/demo_recoverable_real_action_<ts>/log.jsonl`) has
    exactly one `recoverable_retry` line with `"max_retries": 1`.

Known, documented limitation this demo makes visible rather than hides: B2 proves
the recovery action *clears the condition*, not that the run reaches `SUCCESS`
afterward — this particular real scenario needs the whole multi-step sub-account
form re-filled after "Start over," which a single `recovery_action: Step` can't do.
The full recovery→`SUCCESS` path is proven instead by
`tests/test_replay_engine.py::test_recoverable_pattern_retries_and_succeeds_once_recovery_action_clears_it`,
against a synthetic single-step-dismissible interstitial (the case this feature is
actually designed for). Full design rationale for both features is inline where
they're implemented: `OutcomePattern` in `comp_use/schemas.py` (why `max_retries`
defaults to 3, why `business_outcome` is never retried) and `_resolve_with_fallback`
in `comp_use/surface.py` (why non-terminal attempts get a short probe timeout, why a
total failure surfaces the *primary* locator's error).

**9. Drift-aware self-healing replay** (needs a deliberately broken artifact —
one locator typo'd to simulate drift)

```bash
python -c "
import sys; sys.path.insert(0, '.')
from comp_use.cli import save_artifact
from comp_use.config import load_settings
from comp_use.schemas import ActionType, Artifact, Checkpoint, CheckpointType, Locator, LocatorStrategy, RiskTier, Step, ValueSource
settings = load_settings()
artifact = Artifact(
    capability_name='transfer_funds_drift_demo',
    target={'app': 'mock_bank', 'base_url': 'http://localhost:5000'},
    steps=[
        Step(action=ActionType.NAVIGATE, target='http://localhost:5000/member/12345/transfer', risk_tier=RiskTier.SAFE),
        Step(action=ActionType.TYPE_TEXT, locator=Locator(strategy=LocatorStrategy.ROLE, value={'role':'textbox','name':'From Account'}), value_source=ValueSource(type='fixed', reason='demo'), value='ACC-001', risk_tier=RiskTier.SAFE),
        Step(action=ActionType.TYPE_TEXT, locator=Locator(strategy=LocatorStrategy.ROLE, value={'role':'textbox','name':'To Account'}), value_source=ValueSource(type='fixed', reason='demo'), value='ACC-002', risk_tier=RiskTier.SAFE),
        Step(action=ActionType.TYPE_TEXT, locator=Locator(strategy=LocatorStrategy.ROLE, value={'role':'textbox','name':'Amount'}), value_source=ValueSource(type='fixed', reason='demo'), value='10', risk_tier=RiskTier.SAFE),
        Step(action=ActionType.CLICK, locator=Locator(strategy=LocatorStrategy.ROLE, value={'role':'button','name':'Transfer'}), risk_tier=RiskTier.SAFE),
        Step(action=ActionType.CLICK, locator=Locator(strategy=LocatorStrategy.ROLE, value={'role':'button','name':'Cofirm Transfer'}), risk_tier=RiskTier.RISKY),
    ],
    success_checkpoint=Checkpoint(type=CheckpointType.ELEMENT_VISIBLE, locator=Locator(strategy=LocatorStrategy.ROLE, value={'role':'heading','name':'Confirmation'})),
    created_from_run_id='cli_demo',
)
print(save_artifact(artifact, settings.artifacts_dir))
"

python -m comp_use.cli replay --capability-name transfer_funds_drift_demo --confirm-risky --diagnose-drift-on-failure
```
Expected: `"outcome": "hard_failure"` (unchanged — this run genuinely failed) but
`"proposed_patch_version": 2`; an `[ESCALATION]` prompt to review the patch (type
`resume` + a note); `artifacts/transfer_funds_drift_demo/v2.json` has the
corrected locator, `v1.json` untouched. Re-running the same replay command
afterward (`--confirm-risky` is still required — the healed step is still
`risk_tier: risky` — but `--diagnose-drift-on-failure` can be dropped) should
now succeed, using v2 automatically:

```bash
python -m comp_use.cli replay --capability-name transfer_funds_drift_demo --confirm-risky
```

If you run it *without* `--confirm-risky`, it escalates for risky-step
confirmation like any other risky replay; make sure you're in an interactive
terminal (a non-interactive stdin makes the CLI raise a clear error instead of
hanging) or just pass `--confirm-risky`.

**10. Artifact versioning** (re-run discover for an existing capability)

```bash
python -m comp_use.cli discover --goal "Look up member 12345 and view their account balances" \
  --start-url "http://localhost:5000/member/search" --capability-name lookup_member
```
Expected: creates the *next* version file without touching the existing one —
check `artifacts/lookup_member/` afterward.

**11. Full regression check**

```bash
python -m pytest -v
```

## Project layout

```
/mock_app/          legacy-styled Flask target application
/comp_use/          discovery, replay, guardrails, CLI
/comp_use/server/   optional FastAPI capability server (discover/invoke over HTTP)
/artifacts/         saved capability artifacts (JSON)
/evidence/          logs + screenshots from discovery and replay runs
/docs/              design specs and implementation plans
run_mock_app.py     start the mock bank app on :5000
demo_edge_cases.py  live demo: Locator.fallback + recoverable auto-retry (see "Exercising every outcome" step 8)
REPORT.md           design write-up (architecture, schema, determinism, etc.)
```

## Contact

- Email: vijayram.enag2002@gmail.com
- Portfolio: [vijay-ram.vercel.app](https://vijay-ram.vercel.app)
- GitHub: [@VjayRam](https://github.com/VjayRam)
- LinkedIn: [vijay-ram-enaganti](https://www.linkedin.com/in/vijay-ram-enaganti)
