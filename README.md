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
   recoverable / hard failure / input error).
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

## MERIDIAN CORE adaptation — chatbot, dashboard, and a live legacy target

This same core also drives **MERIDIAN CORE**
(`https://web-sample.interface-hiring.com`), a hosted, real, legacy-styled
credit-union member-servicing console (`Adaptation Project — MERIDIAN CORE.pdf`) —
wrapped with a callable capability API, a chatbot, and a dashboard so the
whole thing is demoable end to end, not just runnable from the CLI. **Status:
implementation complete** for all 7 required functions (check balance, member
inquiry — by number *and* by last name, funds transfer, open new share, update
member info, place account hold) plus sign-on, each recorded as a real,
replayable capability against the live target. Full write-up (what adapting
took, the API's shape, exceptional-state handling, how the safety/evidence/
escalation guarantees survive the new surface, and what's cut):
[`ADAPTATION_WRITEUP.md`](ADAPTATION_WRITEUP.md). Deeper engineering detail
lives in [`CODEMAP.md`](CODEMAP.md) (per-file walkthrough),
[`ENHANCEMENTS.md`](ENHANCEMENTS.md) (every fix, why), and
[`IMPACTS.md`](IMPACTS.md) (design decisions vs. their effect on cost/latency/
reliability).

### Extra setup (on top of the Setup section above)

**Docker Desktop** — used for two things: a dedicated Postgres container (the
primary store for artifacts/runs/evidence once the chatbot/dashboard are in
the picture — a discovery/replay run through the server writes both there),
and, optionally, an isolated sandbox container per run (headed Chromium +
noVNC) so the dashboard/chat can stream a live, human-controllable view of
the browser instead of a window on the server's own machine.

```bash
docker run -d --name comp-use-postgres \
  -p 127.0.0.1:51504:5432 \
  -v comp-use-pgdata:/var/lib/postgresql/data \
  -e POSTGRES_USER=compuse -e POSTGRES_PASSWORD=compuse -e POSTGRES_DB=compuse \
  postgres:16-alpine
```

`-p 127.0.0.1:51504:5432` **pins the host port** — without an explicit host
port, Docker assigns a random one on every container restart, and `.env`'s
`COMP_USE_DB_URL` below would need updating each time (data itself is safe
either way — it lives in the `comp-use-pgdata` named volume, independent of
the container).

Add to `.env` (see `.env.example` for the full annotated block):

```bash
COMP_USE_DB_URL=postgresql://compuse:compuse@127.0.0.1:51504/compuse
COMP_USE_SANDBOX=1   # omit/unset to run a local Playwright browser instead
```

Postgres applies its schema on first connection
(`comp_use/pg/schema.sql` via `comp_use/pg/store.py`) — no separate migration
step. Unset `COMP_USE_DB_URL` and the server falls back to the on-disk
`artifacts/`/`evidence/` layout the original take-home uses (fine for CLI-only
use against the mock app; the chatbot/dashboard expect Postgres).

**Frontend** (dashboard + chatbot, a Vite/React app under `frontend/`):

```bash
cd frontend
npm install
copy .env.local.example .env.local   # Windows; Unix: cp ...  (or just create it)
```

`frontend/.env.local`:

```
VITE_API_BASE=http://127.0.0.1:8126
```

### Running it

Three processes, each in its own terminal (Postgres from the `docker run`
above is a fourth, already running in the background):

```bash
# Terminal 1 — capability server (also serves the chatbot/dashboard's API)
.venv\Scripts\python -m comp_use.cli serve --port 8126     # Windows
# or: python -m comp_use.cli serve --port 8126               # Unix / uv run

# Terminal 2 — dashboard + chatbot dev server
cd frontend && npm run dev
```

Open the URL Vite prints (typically `http://localhost:5173`) — the **Dashboard**
tab shows the capability catalog and run history; **+ New workflow** opens the
**Chat** tab, which asks which target site to work against (pick "Meridian" or
paste a URL), then takes natural-language requests and maps them to an
existing capability (with a confirm step before it runs) or proposes recording
a new one via discovery.

### Demo path (MERIDIAN CORE, via the API — mirrors what the chatbot/dashboard do)

All 7 required functions plus sign-on are already recorded and approved as
capabilities. Invoke any of them directly:

```bash
# Read a member's shares and balances (total_balance is computed by replay's own
# sum_currency, never by a model)
curl -s -X POST http://127.0.0.1:8126/capabilities/meridian_member_balance/invoke \
  -H "Content-Type: application/json" \
  -d '{"params": {"operator_id":"teller1","password":"password","branch":"MAIN-001 - Main Office","member_number":"103001"}}'
# -> {"run_id": "invoke_...", "status": "running"}

curl -s http://127.0.0.1:8126/runs/<run_id>   # poll until "done"
# -> {"status": "done", "result": {"outcome": "success",
#     "outputs": {"shares_and_balances": "Share ID\tType\tBalance\tStatus\n...",
#                 "total_balance": "$1,215.50"}}}

# Transfer funds — a risky/irreversible step, always pauses for confirmation
curl -s -X POST http://127.0.0.1:8126/capabilities/meridian_transfer_funds/invoke \
  -H "Content-Type: application/json" \
  -d '{"params": {"operator_id":"teller1","password":"password","branch":"MAIN-001 - Main Office","member_number":"103001","source_share_id":"103001-MMKT-11","destination_share_id":"103001-MMKT-10","amount":"1.00","memo":"demo"}}'
# poll -> {"status": "escalated", "escalation": {"reason": "step N is risk_tier=risky...", "screenshot_url": "..."}}

curl -s -X POST http://127.0.0.1:8126/runs/<run_id>/resume \
  -H "Content-Type: application/json" -d '{"note": "confirmed the transfer"}'
# poll again -> {"status": "done", "result": {"outcome": "success", "outputs": {"confirmation_number": "CN..."}}}

# The brief's own named exceptional-state example: a teller attempting a
# supervisor-only action
curl -s -X POST http://127.0.0.1:8126/capabilities/meridian_place_account_hold/invoke \
  -H "Content-Type: application/json" \
  -d '{"params": {"operator_id":"teller1","password":"password","branch":"MAIN-001 - Main Office","member_number":"102777","share_id":"102777-MMKT-3","reason_code":"FRAUD","notes":"demo"}}'
# -> {"status": "done", "result": {"outcome": "business_outcome", "detail": "...is not authorized to perform this function..."}}
```

The remaining capabilities: `meridian_sign_on`,
`meridian_member_inquiry_by_number`, `meridian_member_inquiry_by_name` (by last
name), `meridian_open_new_share`, `meridian_update_member_info` — same
`POST /capabilities/{name}/invoke` shape; `GET /capabilities` lists every one
with its current typed `input_schema`/`output_schema`.

**Recording a new one** (discovery — needs a model provider key, `NVIDIA_API_KEY`
by default; see [Setup](#setup) — replay never calls a model at all; runs inside
the sandbox if `COMP_USE_SANDBOX=1`, watchable live at the `novnc_url` the run
reports):

```bash
curl -s -X POST http://127.0.0.1:8126/capabilities/meridian_place_hold_v2/discover \
  -H "Content-Type: application/json" \
  -d '{"goal": "Sign on as a specified operator, place a hold on a specified share for a specified reason, and report the confirmation.", "start_url": "https://web-sample.interface-hiring.com"}'
# poll GET /runs/<run_id> -> discover_result.succeeded, then:
curl -s -X POST http://127.0.0.1:8126/capabilities/meridian_place_hold_v2/versions/1/approve
```

Same flow from the chatbot: describe the task in plain language; if no
existing capability matches, it proposes recording a new one (stating the
goal it will use) and asks for confirmation before starting discovery.

#### The goals used to record the current catalogue

These are the exact `goal` strings behind the eight approved capabilities, so
the catalogue can be rebuilt from scratch. `param_hints` is not decoration:
discovery refuses to finish while a declared input was never actually entered
(a pre-filled edit form is otherwise easy to submit unchanged), so the hints
are what make the recording complete rather than merely successful.

Four things in the wording earn their place, each after a recording went wrong
without them:

- **State the credentials outright.** A goal that only says "a specified
  operator" gets a capability whose recorded example operator id is the literal
  string `operator_id`.
- **Say "using a select_option action"** for dropdowns. Otherwise a `<select>`
  gets *clicked*, the branch stays at its default, and sign-on is recorded
  twice — once failing, once working.
- **Say what to report back, by name.** Without it three read-only capabilities
  recorded no `extract` at all and replayed "successfully" with nothing to show.
- **Use values that are live-valid, and amounts well within balance.** A share
  that is on `HOLD`, or an amount over the balance, sends discovery onto the
  rejection page instead of the flow being recorded.

```bash
BASE=https://web-sample.interface-hiring.com
SIGNON="Sign on to the MERIDIAN-style web application at $BASE as operator teller1 \
with password 'password'. Choose 'MAIN-001 - Main Office' from the branch dropdown \
using a select_option action (operator_id, password and branch will each vary per \
call), then submit the sign-on form."

# 1. meridian_sign_on
#    hints: operator_id, password, branch
"$SIGNON Confirm you have arrived at the MAIN MENU, and extract the line that states
 which operator is signed on. Do not perform any other action."

# 2. meridian_member_inquiry_by_number
#    hints: + member_number
"$SIGNON Open 'Member Inquiry / Selection'. Search by member number for member 103001
 (member_number will vary per call). Then extract the search RESULTS table listing the
 matched member - the table containing the member number and name, not the search form
 above it. Do not open the member's record."

# 3. meridian_member_inquiry_by_name
#    hints: + last_name
"$SIGNON Open 'Member Inquiry / Selection'. Use a select_option action to change the
 'Search by' dropdown to Last Name, then search for last name 'Vaughan' (last_name will
 vary per call). Then extract the search RESULTS table listing the matched member(s) -
 the table containing member numbers and names, not the search form above it."

# 4. meridian_member_balance
#    hints: + member_number
"$SIGNON Open 'Member Inquiry / Selection', search by member number for member 103001
 (member_number will vary per call), and open that member's record. Extract the SHARES /
 BALANCES table showing every share id, type, balance and status, as an output named
 shares_and_balances. Then, using derive_as/derive_op, also report the sum of all the
 share balances as an output named total_balance."

# 5. meridian_transfer_funds   (risky: escalates at Post Transfer)
#    hints: + member_number, source_share_id, destination_share_id, amount, memo
"$SIGNON Open 'Member Inquiry / Selection', search by member number for member 103001
 (member_number will vary per call), open that member's record, and choose 'Funds
 Transfer'. Select source share '103001-MMKT-11' and destination share '103001-MMKT-10'
 (source_share_id and destination_share_id will vary per call), enter an amount of 1.00
 (amount will vary per call, always well within the source share's balance) and a memo
 (memo will vary per call). Click Continue to reach the confirmation screen, then click
 'Post Transfer' to actually commit the transfer. Finally extract the confirmation
 number shown on the TRANSFER POSTED screen."

# 6. meridian_open_new_share   (risky: escalates at Open Share)
#    hints: + member_number, share_type, initial_deposit
"$SIGNON Open 'Member Inquiry / Selection', search by member number for member 102777
 (member_number will vary per call), open that member's record, and choose 'Open New
 Share'. Use a select_option action to choose the Regular Shares share type (share_type
 will vary per call) and enter an initial deposit of 5.00 (initial_deposit will vary per
 call). Continue to the review screen and then post it to actually open the share.
 Finally extract the confirmation number and the new share id from the resulting screen."

# 7. meridian_update_member_info
#    hints: + member_number, email, phone, address
"$SIGNON Open 'Member Inquiry / Selection', search by member number for member 102777
 (member_number will vary per call), open that member's record, and choose 'Update Member
 Information'. Set the e-mail to 'katherine.johnson@example.com', the phone to '555-0187'
 and the mailing address to '14 Cornerstone Ave, Springfield' (email, phone and address
 will each vary per call). Save the changes, then extract the confirmation message or the
 updated contact details shown afterwards."

# 8. meridian_place_account_hold   (supervisor-only; risky: escalates at Apply Hold)
#    hints: + member_number, share_id, reason_code, notes
"Sign on to the MERIDIAN-style web application at $BASE as operator super1 with password
 'password' - this is a supervisor-only function, so a teller cannot complete it. Choose
 'MAIN-001 - Main Office' from the branch dropdown using a select_option action
 (operator_id, password and branch will each vary per call), then submit the sign-on form.
 Open 'Member Inquiry / Selection', search by member number for member 102777
 (member_number will vary per call), open that member's record, and choose 'Place Account
 Hold'. Use select_option actions to choose share '102777-MMKT-13' (share_id will vary per
 call) and reason code FRAUD (reason_code will vary per call), and enter notes (notes will
 vary per call). Continue to the review screen, then post the hold to actually place it.
 Finally extract the confirmation number shown afterwards."
```

Each is submitted the same way, with its hints:

```bash
curl -s -X POST http://127.0.0.1:8126/capabilities/meridian_member_balance/discover \
  -H "Content-Type: application/json" \
  -d "{\"goal\": \"$GOAL\", \"start_url\": \"$BASE\",
       \"param_hints\": [\"operator_id\",\"password\",\"branch\",\"member_number\"]}"
```

The three risky capabilities (5, 6, 8) pause at their commit step during
recording exactly as they do during replay — resume to record the step, and it
is marked `risk_tier=risky` in the artifact so every future replay pauses there
too.

### Running offline / mocked

`python -m pytest -v` never touches the live target, Postgres, or a real LLM —
every test spins up its own throwaway fixture and uses `FakeLLMClient`.
Against MERIDIAN CORE specifically: `replay` never calls an LLM (only
`discover` needs `OPENROUTER_API_KEY`, and the sample app's public demo
credentials, already checked into `.env` — no real credentials or PII
involved), so every capability above can be exercised with only the
capability server running, no live LLM call in the loop.

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

Edit `.env` and set a model provider key — required only for `discover`; replay
never calls a model at all. `MODEL_PROVIDER` defaults to `nvidia`, so
`NVIDIA_API_KEY` (from build.nvidia.com) is the one to set; whichever provider you
do not choose is used as an automatic fallback when its key is present.

`MODEL_PROVIDER=openrouter` works too, but note OpenRouter's free tier caps the
whole **account** at 50 model requests per day across every free model, and a
single discovery run spends 15–20 of them — two or three recordings exhaust it and
everything afterwards fails with HTTP 429 regardless of which free model is named.

The `*_VISION_MODEL` settings have working defaults and rarely need changing —
they are a fallback for when the accessibility tree alone isn't enough for the
model to locate an element (see REPORT.md's Heterogeneity section).

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

**Replay with a missing param** (input error — no browser window):

```bash
python -m comp_use.cli replay --capability-name lookup_member --params "{}"
```

Expected: `"outcome": "input_error"` and detail `missing required param 'member_id'`.

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

**5. Replay — `input_error`** (no browser opens — see Demo path above for the single-command version)

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

**Safety & robustness fixes found in self-review (no new demo commands — covered by
unit tests and the live checks above).** Three gaps found by re-reading the shipped
code rather than by the assignment's own checklist:
- The allowlist is now re-checked **after** every action too, not just before — a
  `CLICK` has no explicit target, so a click that navigates off-allowlist was
  previously never validated at all (only an explicit `NAVIGATE`'s destination was).
- All four `escalation.escalate(...)` call sites (`discovery/agent.py`,
  `replay/engine.py` ×2, `cli.py`'s drift-diagnosis review) now catch a failing
  transport (e.g. the non-interactive-stdin case above) and return a clean,
  structured outcome instead of a raw traceback.
- `browser.close()` in `run_discover()`/`run_replay()` now runs in a `finally`
  block, so an unhandled exception mid-run can no longer skip cleanup and leak the
  Chromium process.

Full write-up, rationale, and the tests proving each: `ENHANCEMENTS.md` items 9-11.

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
terminal, or just pass `--confirm-risky`. A non-interactive stdin no longer
hangs *or* crashes with a raw traceback — it comes back as a structured
`"outcome": "hard_failure"` with `"expected": "escalation to complete (human
confirmation for a risky step)"` explaining exactly why, same as any other
replay failure.

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
/mock_app/          legacy-styled Flask target application (original take-home)
/comp_use/          discovery, replay, guardrails, CLI
/comp_use/server/   FastAPI capability server (discover/invoke over HTTP; chat endpoints)
/comp_use/chat/     chat agent - natural-language front door onto the capability API
/comp_use/pg/       Postgres store (artifacts/runs/evidence) - primary once configured
/comp_use/sandbox*  isolated Docker sandbox (headed Chromium + noVNC) for live-watchable runs
/frontend/          dashboard + chatbot UI (Vite/React)
/artifacts/         saved capability artifacts (JSON) - fallback store, Postgres primary
/evidence/          logs + screenshots - fallback store, Postgres primary
/docs/              design specs and implementation plans
run_mock_app.py     start the mock bank app on :5000 (original take-home target)
demo_edge_cases.py  live demo: Locator.fallback + recoverable auto-retry (see "Exercising every outcome" step 8)
REPORT.md           original take-home design write-up (architecture, schema, determinism, etc.)
ADAPTATION_WRITEUP.md   MERIDIAN CORE adaptation write-up (what changed, why, what's cut)
CODEMAP.md / ENHANCEMENTS.md / IMPACTS.md   full engineering log for the adaptation
```

## Contact

- Email: vijayram.enag2002@gmail.com
- Portfolio: [vijay-ram.vercel.app](https://vijay-ram.vercel.app)
- GitHub: [@VjayRam](https://github.com/VjayRam)
- LinkedIn: [vijay-ram-enaganti](https://www.linkedin.com/in/vijay-ram-enaganti)
