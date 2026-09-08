# CODEMAP — `comp_use/`

This document maps every file in `comp_use/` to what it does, why it exists, and which
requirement of the assignment (`Assignment A — Computer-Use Automation System.pdf`) it
satisfies. Read alongside `/REPORT.md` (design rationale/trade-offs) and `/README.md`
(how to run it). Section numbers below (§3.1, §3.2, …) refer to the assignment PDF.

The through-line the assignment asks for:

> The model discovers → the artifact becomes a reusable capability → deterministic replay
> is how the AI agent invokes it in production.

`comp_use/` is organized around exactly that pipeline:

```
comp_use/
├── config.py              settings (allowlist, models, dirs)
├── schemas.py              the Artifact / Step / Locator / ReplayResult data contracts
├── surface.py               how the agent perceives and acts on the UI (Playwright)
├── guardrail.py            allowlist + risk + redaction policy engine
├── tokenizer.py            reversible tokenization of sensitive values before they reach the LLM
├── evidence.py             structured JSONL logging + screenshots, redacted
├── llm_client.py           LLM providers (OpenRouter, NVIDIA NIM) + fallback + prompts
├── drift.py                 optional: vision-based self-healing patch proposal
├── cli.py                   entrypoint: wires everything together (discover/replay/approve/serve)
├── sandbox.py               one isolated, watchable browser container per run
├── discovery/
│   ├── agent.py             the observe → decide → act loop (§3.1)
│   ├── compiler.py          RunTrace → Artifact (§3.2)
│   └── outcome_library.py   the host-level error taxonomy, resolved live per run
├── replay/
│   └── engine.py            deterministic, LLM-free replay (§3.3)
├── escalation/
│   ├── controller.py         pause/handoff/resume state machine (§3.6)
│   └── transport.py          how "notify a human" + "wait for resume" are transported
├── pg/
│   └── store.py             Postgres persistence (primary store once COMP_USE_DB_URL is set)
├── chat/
│   ├── session.py           in-memory chat session state
│   ├── catalog.py           the capability catalogue the chat agent reasons over
│   └── agent.py             turn-by-turn decision logic (invoke / discover / resolve)
├── sandbox_image/
│   └── launch_browser.py    headed Chromium + noVNC inside the sandbox container
└── server/
    ├── app.py               FastAPI capability catalog / invoke API (§8 stretch goal)
    └── run_manager.py        background-thread run tracking for the HTTP server
```

---

## Mini-codemap — every file in a paragraph

The one-screen version: what each file does, how it does it, and why it exists at all.
Each has a full section further down; this is the map you read first.

### Core contracts

**`config.py`** — One `Settings` dataclass plus `load_settings()`, which reads it from
environment variables with working defaults. Holds the URL/action allowlist, model provider
and model names, storage directories, and the redaction patterns. It exists so that §3.4's
"explicit, configurable allowlist" is a value passed into the engines rather than a constant
buried in them — pointing the system at a new target is an env change, not a code change.

**`schemas.py`** — The Pydantic data contracts every other file speaks: `Artifact`, `Step`,
`Locator`, `Checkpoint`, `ValueSource`, `OutcomePattern`, `ReplayResult`, and the
`InputParam`/`OutputParam` typing. Discovery's only job is to produce one of these and
replay's only job is to consume one, so this file is the seam between "a model explored a
UI" and "an agent calls a capability by name." Making it typed and validated is what lets a
malformed model decision fail at the boundary instead of halfway through a browser session.

**`surface.py`** — The perception-and-action abstraction: `observe()` returns an
accessibility tree, `act()` performs one `ActionType`, `check_checkpoint()` answers whether a
state holds. `PlaywrightSurface` is the only implementation, but every engine depends on the
interface, so a desktop or mobile surface could be swapped in without touching discovery or
replay. It also absorbs the target's ugliness — label-vs-value dropdown matching, rendered
text vs HTML source, locator fallback chains — so that ugliness lives in one file instead of
being spread across every capability.

**`guardrail.py`** — The single policy engine: allowlist checks, risk-tier classification,
and one-way redaction of sensitive values. Both `DiscoveryAgent` and `ReplayEngine` call the
same functions, which is the point — a safety rule enforced in one engine and not the other
is not a safety rule. `requires_confirmation()` is deliberately the *only* place that decides
whether a step needs a human, so there is one answer to that question in the codebase.

**`tokenizer.py`** — `SensitiveValueTokenizer` reversibly swaps sensitive values for
placeholders (`[[TOK1]]`) before text reaches the LLM and swaps them back before anything
reaches the browser. It reads the same `redaction_patterns` list `Guardrail.redact()` uses, so
covering a new sensitive shape is one regex in one place. It exists because the model needs to
*reason* about a page containing account numbers without those numbers leaving the machine —
and being two-way is what keeps the actual browser action operating on real data.

**`evidence.py`** — `EvidenceLogger` writes structured JSONL events and screenshots for every
run, redacting on the way out. Everything the dashboard shows and every post-hoc diagnosis in
this project came from these events, not from console output. It exists because §3.5 asks for
observability, and because a run you cannot reconstruct afterwards is a run you cannot debug.

### The pipeline

**`llm_client.py`** — All model interaction: the tool schemas, the system prompts, the
provider clients (OpenRouter, NVIDIA NIM), `FallbackLLMClient` for automatic failover, and the
`RateLimiter` pacing the primary. Forcing a tool call rather than parsing free text is what
makes the model's output land directly on the `Step`/`Locator` schema. It is also the only
file in the system that talks to a model at all — which is precisely what makes "replay never
invokes the LLM" checkable rather than aspirational.

**`discovery/agent.py`** — The observe → decide → act loop (§3.1): show the model the page,
take one decision, perform it, record it, repeat. Around that core sit the parts that make a
*recording* trustworthy rather than merely finished — fault classification that refuses to
record an error path, extract-locator quality gates, loop detection, and escalation on risky
steps. It exists because the assignment's through-line starts here: everything downstream is
only as good as what this loop chose to write down.

**`discovery/compiler.py`** — `compile_artifact()` turns a `RunTrace` into an `Artifact`,
deriving the input schema from every `goal_parameter` the run used and the output schema from
every `extract_as`. It also dedupes consecutive identical steps. This is where an exploratory
transcript becomes a typed, reviewable capability — the moment §3.1 becomes §3.2.

**`discovery/outcome_library.py`** — A host-level library of `OutcomePattern`s keyed on the
target's hostname, attached to every artifact compiled against that host and resolved live at
replay time. It exists because a target's error screens (member not found, permission denied,
maintenance) are the same handful of pages no matter which business flow reached them, so
recognising them belongs to the host, not to each capability. It is the only wholly
target-specific file in the backend — retargeting means editing this one file.

**`replay/engine.py`** — Deterministic execution of a recorded artifact with no model in the
loop: substitute the caller's parameters, run each step, check outcome patterns before and
after, retry recoverable conditions, and return a typed `ReplayResult`. This is what an AI
agent actually invokes in production, and its determinism is the whole value proposition —
the same artifact and the same inputs produce the same actions every time.

**`drift.py`** — The optional self-healing path (§8 "assisted fallback"): when a replay step's
locator no longer matches, send a screenshot and the failed locator to a vision model and ask
whether a control serving the same purpose still exists. If it does, the answer becomes a
*proposed patch* — a new artifact version for a human to approve, never an edit applied
mid-run. That distinction is the whole design: replay stays deterministic and model-free, and
the model's judgement is confined to suggesting a change someone else signs off on.

### Human handoff

**`escalation/controller.py`** — The pause/handoff/resume state machine (§3.6): flip control
to the human, notify, wait, capture what changed, flip back. It screenshots and diffs the
accessibility tree either side of the handoff, so the record shows *what the human did*, not
merely that they intervened. It exists so that "a human takes over" is a first-class,
evidenced state rather than a crashed run someone restarted by hand.

**`escalation/transport.py`** — How "notify a human" and "wait for resume" are carried:
`LocalSharedBrowserTransport` for the CLI (print, block on `input()`), `QueueTransport` for the
server (callback, block on a queue). Splitting transport from controller is what lets the same
escalation logic serve a terminal operator and an HTTP client unchanged. It also carries the
three ways a run is interrupted — takeover, cancel, and interrupt — which are three different
intents that land in three different terminal states.

### Entrypoints and storage

**`cli.py`** — The wiring: `discover`, `replay`, `approve`, `serve`, each assembling the
settings, surface, guardrail, evidence logger, and engine for one command. `_browser_session()`
is the notable piece — a context manager owning the browser (local Chromium or a sandbox
container), so anything that unwinds through it tears the session down, including a run
someone stopped. Every other entrypoint, the HTTP server included, calls the same functions
here rather than reimplementing the pipeline.

**`server/app.py`** — The FastAPI capability API (§8): a catalog of approved capabilities, an
`invoke` endpoint an agent calls by name with typed args, `discover` to record a new one, run
polling, the run-control endpoints, and the draft/approved/rejected version workflow. It is a
thin wrapper over `cli.py`'s functions — the engines are untouched by it. The approval gate is
the load-bearing part: an unreviewed recording can never become what an unattended `invoke`
picks up.

**`server/run_manager.py`** — `RunRecord` + `RunManager`: start a run on a background thread so
an HTTP request can return immediately with a `run_id`, and let later requests observe and
control that in-flight run. All shared state moves under one lock, because escalation, resume,
takeover, and interrupt all arrive from a different thread than the one doing the work. It
exists because a browser-driving run takes minutes and no HTTP handler should hold a
connection open that long.

**`pg/store.py`** — Postgres persistence for artifacts, runs, events, and screenshots, primary
once `COMP_USE_DB_URL` is set and falling back to files otherwise. Every read is best-effort
and non-fatal, so a database outage degrades observability rather than killing runs. It exists
because the file-per-version layout piled up untracked artifacts faster than anyone could
review them, and because the dashboard needs to query history rather than walk a directory.

**`sandbox.py` / `sandbox_image/launch_browser.py`** — One ephemeral Docker container per run,
holding a headed Chromium under Xvfb with noVNC attached; `spawn()` returns a CDP URL to drive
it and a noVNC URL to watch it. x11vnc runs *without* `-viewonly` deliberately, because §3.6
requires a human to take control of the live session, not merely watch it. This is what makes
"the human operates the same session, not a fresh one" true when the browser isn't running on
anyone's desk.

### Chat front door

**`chat/session.py`** — In-memory per-session state: the message history, the selected target
site, and any `PendingAction` awaiting confirmation. Deliberately not persisted — a chat
session is a conversation, while the runs it starts are durable and live in Postgres.

**`chat/catalog.py`** — One function, `build_chat_catalog()`, returning the approved
capabilities the chat agent is allowed to reason over. Keeping it separate means the agent
cannot invent a capability that isn't really there: its whole notion of what exists comes from
this list.

**`chat/agent.py`** — The turn-by-turn decision logic: given the conversation and the catalog,
propose invoking a capability, propose recording a new one, or resolve a pending confirmation.
It is a pure client of the same HTTP surface everything else uses — it never imports
`DiscoveryAgent` or `ReplayEngine` and never starts a run itself, handing a confirmed action
back to the server to run through the ordinary path. Every side-effecting proposal goes through
an explicit yes/no, so the model can suggest but never act alone.

---

## MERIDIAN CORE adaptation (branch `meridian-core-adaptation`)

Everything above this point describes the system as built for the original take-home
(`Assignment A`). The Adaptation Project points the same pipeline at a real, hosted
legacy target (`web-sample.interface-hiring.com`) instead of the local mock app, per
`Adaptation Project — MERIDIAN CORE.pdf`. Live testing against that target surfaced six
real defects in the files below — all fixed and live-verified; full write-up with
before/after logs and artifact excerpts in `EXT_TASK_FIXES.md`, one-paragraph summary
of each in `ENHANCEMENTS.md` §13, and the reliability/cost reasoning behind each in
`IMPACTS.md` §5.12-5.13. This section is a pointer, not a duplicate — see those three
docs for detail; here's just *which files changed and what changed in them*, so the
per-file sections below stay accurate to read alongside the code:

- **`discovery/agent.py`** — `_classify_risk()` gained a `current_url` parameter and a
  second risk signal (any click while the current URL contains `"review"`), plus
  `"post"` added to the keyword list (EXT_TASK_FIXES.md #1). `_run_loop()` gained a
  best-effort `possible_incomplete_capability` evidence warning when a `combobox` was
  seen but no `SELECT_OPTION` was ever performed (#5).
- **`surface.py`** — `PlaywrightSurface.observe()` gained a `_form_field_hints()`
  supplement (`name`/`id`/`type` only, never a field's value) appended to the tree text
  sent to the LLM, addressing `aria_snapshot()`'s blind spot on unlabeled legacy inputs
  (#2). `check_checkpoint()`'s `URL_MATCHES` branch now treats `url_pattern` as a
  regex-with-`*`-wildcards instead of a plain substring, backward compatible with every
  existing literal pattern (#4).
- **`cli.py`** — `_derive_success_checkpoint()` wildcards purely-numeric URL path
  segments before falling back to a literal URL checkpoint (#4). `run_discover()`'s
  `target` construction now derives `app`/`base_url` via `urlparse(start_url)` instead
  of a hardcoded `"mock_bank"` label and a `"/member"`-split that silently broke on any
  URL shape besides the original mock app's (#8).
- **`discovery/compiler.py`** — `compile_artifact()` now dedupes exact-adjacent
  duplicate steps via a new `_dedup_consecutive_steps()` (#8).
- **`llm_client.py`** `_SYSTEM_PROMPT` — gained: stricter `value_source.type`/`strategy`
  enum guidance; a rule to fall back to a `name`/`type`/`id`-keyed `css` locator when a
  `role` locator would be ambiguous (needed for MERIDIAN's unlabeled sign-on form, #3);
  a rule forbidding locators keyed on a field's current value (#2); a rule to call
  `finish` immediately after a successful `extract` rather than repeating it; a rule to
  actively `select_option` a goal-relevant dropdown rather than leaving it at default
  (#5).
- **`config.py`** `load_settings()` — `allowed_url_prefixes` gained an
  `ALLOWED_URL_PREFIXES` environment-variable override; previously the "explicit,
  configurable allowlist" §3.4 requires had no env override at all, only a hardcoded
  `http://localhost:5000` default, which made testing against any other target
  impossible without editing code (#8, permanent fix, not spike-only scaffolding).

None of this changed `replay/engine.py`, `guardrail.py`, `tokenizer.py`, `evidence.py`,
`escalation/`, or `server/app.py` — the discover/replay/escalation/API contracts
described in the rest of this document are unchanged; only the discovery-time
perception/classification/compilation logic feeding into those contracts got more
robust. What's still ahead for the Adaptation Project (session-as-typed-output/input
model, the real capability-recording pass, the capability-API/chatbot/dashboard layer)
is not yet reflected in this document.

---

## Logical walkthrough — following the code end to end

The per-file sections below are a reference. This section is the narrative: pick one thread
and follow it call-by-call, the way you'd walk a CTO or a new engineer through the system on a
whiteboard. Two runs are traced — `comp-use discover` and `comp-use replay` — plus the
escalation detour either can take. Each step names the actual function, what it does, and
which assignment requirement it's discharging, so "why does this code exist" always has a
one-line answer.

### Walkthrough A — `comp-use discover --goal "..." --start-url ... --capability-name lookup_member`

**1. Entry and wiring — `cli.main()` → `_run_discover()` → `run_discover()`**
`main()` parses args and dispatches to `_run_discover(args)`, which builds a
`LocalSharedBrowserTransport()` (the escalation channel — see step 6) and calls
`run_discover()`. This function is the composition root for one discovery run: it builds
`Settings` (`load_settings()`), a `Guardrail(settings)`, an `EvidenceLogger` keyed by a fresh
`run_id`, and an LLM client via `_build_llm_client(settings)` — which picks OpenRouter or
NVIDIA NIM as primary based on `MODEL_PROVIDER`/which API key is actually configured, and
wraps both in `FallbackLLMClient` if a second key exists. *Satisfies:* §3.1's "accept a
goal + a target as input," §4's "LLM provider... is your call."

**2. Standing up the surface — `PlaywrightSurface`, `EscalationController`, `DiscoveryAgent`**
Still inside `run_discover()`: a real Chromium is launched via Playwright
(`p.chromium.launch(headless=_headless())`), wrapped in `PlaywrightSurface(page)` — this is the
concrete implementation of the `Surface` abstraction (`surface.py`) that turns
`ActionType`/`Checkpoint` values into real `page.click()`/`page.fill()`/`page.goto()` calls.
An `EscalationController(evidence, transport, surface=surface)` is built *sharing this exact
surface object* — critical, because §3.6 requires the human to take over "the same live
session... not a fresh one." A `DiscoveryAgent` is constructed from all of the above.
*Satisfies:* §3.1's "must actually interact with a real UI," the surface-abstraction seam
§3.7 asks the write-up to defend.

**3. The loop itself — `DiscoveryAgent.run(goal, start_url)`**
This is the heart of §3.1. Per iteration, in order:
- `surface.observe()` reads the page's **accessibility tree** (not raw DOM) — the bias toward
  a representation that "would still work when the surface has no clean DOM" (§3.1).
- `self.tokenizer.tokenize(observed.accessibility_tree)` replaces every real
  account/transaction/dollar-amount pattern with a stable placeholder token
  (`tokenizer.py: SensitiveValueTokenizer`) **before** anything is sent to the LLM.
  *Satisfies:* §3.4's "avoid leaking... sensitive data," applied to the one channel that
  `Guardrail.redact()` alone can't cover — the outbound model call.
- `llm_client.decide_next_action(goal, tokenized_tree, screenshot_b64, history)` — the actual
  network call to OpenRouter/NVIDIA (`llm_client.py`), asking the model to pick one action via
  a forced tool call (`_TOOL_SCHEMA`). If the tree alone wasn't enough last turn
  (`needs_vision_fallback`), a screenshot is attached and a vision-capable model is used
  instead. *This is the assignment's one non-negotiable requirement*: "the discovery run has
  to be real... a genuine LLM-driven run against a live surface."
- The raw decision is validated defensively (`_locator_from_decision`,
  `_value_source_from_decision`) and, if it names a `locator`/`target`/`text`, detokenized back
  to real values at this single boundary — everything downstream (the actual click, risk
  classification, the persisted `Step`) sees real data again.
- `guardrail.check_allowlist(url, action.value)` — raises `AllowlistViolation` and the
  decision is skipped if the URL/action isn't permitted. *Satisfies:* §3.4's "the agent must
  not act outside" the allowlist, enforced live during discovery, not just at replay time.
- `_classify_risk(action, target, locator)` tags the step `SAFE`/`RISKY` (keyword match on
  "confirm"/"delete" in the target control's name). If `RISKY` and `--confirm-risky` wasn't
  passed, `self._escalate(...)` fires **before** the click happens — see Walkthrough C.
  *Satisfies:* §3.4's "distinguish safe/reversible actions from risky/irreversible ones... and
  handle the risky class conservatively."
- `surface.act(action, locator, target, text)` performs the real click/type/navigate/extract.
  A raised exception is caught, logged, and turned into a retry-with-vision-next-turn rather
  than a crash.
- A `Step` is appended to `trace.steps` — with the crucial detail that if the value came from
  a `goal_parameter` (e.g. the member ID the user typed), the **literal discovery-time value is
  not persisted** — only the fact that it's parameterized is. This is what makes the eventual
  artifact reusable rather than hardcoded to one run's inputs.
- Loop ends on `finish`/`done`, on `max_steps` exhaustion, or on three consecutive
  skipped/invalid decisions (`_DEAD_END_THRESHOLD`) — the three stopping conditions §3.1
  requires ("max steps, timeout, dead-end"), each also wired to escalate (§3.6).

**4. Compiling the transcript into a capability — `_derive_success_checkpoint()` →
`compile_artifact()`**
Back in `run_discover()`, once `trace.succeeded` is true: `_derive_success_checkpoint()`
builds a generic checkpoint from the **final page's own heading** (not the literal URL, which
would contain a run-specific member ID or generated confirmation number and never reproduce
on a different replay). `compile_artifact(trace, ...)` (`discovery/compiler.py`) then walks
`trace.steps` twice — once to derive `input_schema` from every distinct `goal_parameter`, once
to derive `output_schema` from every `extract_as` — and returns a typed `Artifact`. This is the
explicit "decouple the artifact from the raw model transcript" step §3.2 requires: `RunTrace`
(transcript-shaped) goes in, `Artifact` (capability-contract-shaped, per `schemas.py`) comes
out. *Satisfies:* all five §3.2 bullets — ordered steps, locator identification, typed inputs,
typed outputs, a success checkpoint.

**5. Versioning and gating — the approval prompt, `_version_lock`, `save_artifact()`**
The artifact starts `status="draft"`. If run interactively, the CLI asks "Approve as new
default? [y/N]" — a direct implementation of §8's draft→approved gate. Version allocation
(`next_artifact_version()`) and the save happen inside `_version_lock`, so a concurrent
discovery for the same capability name (reachable once `server/app.py` exposes discover over
HTTP) can't race and silently clobber another run's save. Any hand-authored
`outcome_patterns` from the previous version are carried forward, since a fresh run has no way
to observe them itself. The result: `artifacts/lookup_member/v2.json` on disk — reviewable by
a human, and by any agent that later calls `GET /capabilities/lookup_member`. *Satisfies:*
§3.2's "versioned and reviewable."

### Walkthrough B — `comp-use replay --capability-name lookup_member --params '{"member_id": "67890"}'`

**1. Load and pre-validate — `run_replay()` → `load_artifact()` → the pinned-version approval
gate → `validate_required_params()`**
`load_artifact("lookup_member", artifacts_dir, version=version)` behaves two different ways
depending on whether the caller pinned a version:
- **`version=None`** (the default — no `--version` flag, no `"version"` in the invoke body):
  walks saved versions **newest-first** and returns the first with `status == "approved"` — an
  unreviewed draft can never be picked up by an unattended replay/invoke call.
- **`version=N`** (`--version N` on the CLI, or `"version": N` in `POST .../invoke`): loads
  *exactly* that file, regardless of status — `load_artifact()` itself applies no approval
  filter to an explicit version, because it's also used internally to load drafts on purpose
  (e.g. `approve_artifact()` needs to load a draft to approve it). The approval check for a
  **pinned replay/invoke** specifically is therefore done by the caller, immediately after:
  `run_replay()` raises `ValueError(f"version {version} of '{capability_name}' is
  '{artifact.status}', not 'approved'...")` the instant `version is not None and
  artifact.status != "approved"` — before `validate_required_params()` runs and **before
  Chromium is even launched**, so a pinned draft or rejected version never starts a run at all.
  `server/app.py`'s `invoke_capability()` performs the identical check synchronously in the
  HTTP handler (returning `409 Conflict`) so a doomed request never even reaches
  `RunManager.start()` — the same "fail fast, before any browser opens" principle
  `validate_required_params()` already established for missing params, now applied to version
  pinning too.

`validate_required_params(artifact, params)` then checks every `required=True` entry in
`input_schema` is present — again, before Chromium is even launched, so a malformed request
fails fast and cheaply. *Satisfies:* §3.3's execution contract ("given a saved artifact and a
set of input parameters, replay it"), and the `VALIDATION_ERROR` branch of the outcome taxonomy
(§3.3's "distinguish... between expected business outcomes... recoverable conditions... and
hard failures" — a missing param is none of those three; it's a caller error, reported as its
own fourth `OutcomeType`). The version-pinning gate is a related but distinct safety property:
it's not about whether the *params* are valid, but about whether the *artifact itself* is
trusted to run unattended — the same "only an approved version drives production behavior"
guarantee `load_artifact(version=None)` already gives every unpinned caller, extended to cover
the case where a caller deliberately asks for something other than "latest."

**2. Deterministic execution — `ReplayEngine.run(artifact, params)`**
A fresh `PlaywrightSurface` drives a **new** browser session (replay never touches the session
discovery used — see the escalation note in Walkthrough C for why this matters). The loop is a
`while` over the step index (not a plain `for`), specifically so a retry can re-attempt the
**same** step rather than advancing:
- `_match_outcome_pattern(artifact)` is checked **first, every iteration** — has the app
  already diverged onto a known state (e.g. "insufficient funds") from a previous step? This
  runs before every step, not just at the end, because §3.3 explicitly calls out that "the app
  can diverge mid-sequence." If it matches a `RECOVERABLE` pattern with retry budget left
  (`OutcomePattern.max_retries`, **default 3**), `_should_retry()` performs the pattern's
  `recovery_action` (if any — e.g. click a "Try Again" control) and the loop `continue`s back
  to the same step index rather than giving up immediately. Only once the budget is exhausted
  (or the pattern is a `business_outcome`, never retried) does it return
  `ReplayResult(outcome=pattern.outcome, detail=pattern.detail)`.
- If the step is `RISKY` and `confirm_risky` wasn't passed, escalate (same `Guardrail`/
  `EscalationController` machinery as discovery — one policy, two call sites).
- The value to type/select is resolved: literal `step.value`, or the **caller's own**
  `params[param_name]` if the step is parameterized. This is the moment "replay with a
  different member ID than the one discovery used" actually happens — proof the artifact
  generalizes, not just replays its own recording verbatim.
- `guardrail.check_allowlist()` is re-checked here too (defense in depth — replay enforces the
  same policy discovery did, never trusting that a saved artifact is inherently safe to
  re-run) — **and checked again a second time immediately after** `surface.act()` runs, against
  wherever the action actually left the browser. The pre-action check alone can only validate
  an explicit `NAVIGATE` target or the page the browser was already on; a `CLICK` has no
  explicit target, so a click that navigates off-allowlist was previously never validated at
  all. Found and closed as a real gap (`ENHANCEMENTS.md §9`), not a documented cut.
- `surface.act()` performs the real action — itself now transparently trying a locator's
  `.fallback` chain if the primary strategy doesn't resolve (see `surface.py`'s
  `_resolve_with_fallback`). **No LLM call exists anywhere in this file** — the literal
  implementation of "replay it without invoking the LLM for decisions" (§3.3).
- Any raised exception, or a per-step `checkpoint` miss, is checked against outcome patterns
  again (same retry-or-report logic as above), then falls through to `HARD_FAILURE` with
  `step_index`/`expected`/`observed` — "what step, what was expected, what was observed,"
  verbatim from the assignment.
- After all steps, a second small loop re-verifies `artifact.success_checkpoint` the same
  retry-aware way. Only once it holds does the engine return `ReplayResult(outcome=SUCCESS,
  outputs={...})`.

**3. Reporting the result**
`_run_replay()` prints `result.model_dump_json()` — a structured, typed result an orchestrating
AI agent (or a human) can branch on programmatically: `success` with `outputs`,
`business_outcome`/`recoverable` with a `detail` string, or `hard_failure` with full debugging
context. This structured contract — not a boolean, not a raw exception — is the concrete
answer to §3.3's "report a clear, structured result."

**4. Optional post-mortem — `--diagnose-drift-on-failure`**
If the outcome was `HARD_FAILURE` *and* it was specifically the recorded action's own locator
that failed to resolve (`_is_action_locator_failure`), `cli._diagnose_and_propose_patch()`
calls `drift.propose_drift_patch()`: a **separate**, opt-in LLM call (vision model + screenshot
of the live failure) asks "did this control just move/get renamed, or is it genuinely gone?"
If a plausible replacement is found, a **new** artifact version is saved as a draft and a human
is escalated to review it — this run's own `HARD_FAILURE` verdict is never altered. This is
deliberately outside `ReplayEngine` so the "replay never calls an LLM" guarantee stays literal
truth, not just true in the common case.

### Walkthrough C — the escalation detour (can trigger from either A or B)

**1. Trigger.** Any of: a `RISKY` step without `--confirm-risky` (checked before the action
runs, in both `DiscoveryAgent` and `ReplayEngine`), discovery hitting `max_steps`, or three
consecutive skipped/invalid discovery decisions. Whichever engine hit it calls
`self.escalation.escalate(InterventionRequest(run_id, capability_or_goal, current_step,
screenshot_path, reason))` — `InterventionRequest` carries exactly the context §3.6 requires:
"which capability/goal, the current step, the current state or screenshot, and why it
stopped."

**2. Handoff — `EscalationController.escalate()`**
Sets `control = HUMAN`, captures a **before** accessibility tree + screenshot from the live
`Surface` (the same object the automation was just driving — this is what makes "take control
of the live session... not a fresh one" literally true, not just conceptually), then calls
`transport.notify(request)` and blocks on `transport.wait_for_resume()`. The calling
engine's thread is parked here — automation genuinely pauses.

**3. The human's turn — `ControlTransport`**
For the CLI (`LocalSharedBrowserTransport`), the browser was launched headed, so "the operator
console" *is* the real, visible browser window — no separate co-browsing infrastructure was
built, satisfying §3.6's own scope note ("mock the operator UI if needed, but make the handoff
mechanism and the control-transfer model real"). The operator manually does whatever's needed,
then types `resume` (plus an optional one-line note) at the same terminal. For the HTTP server,
`QueueTransport.wait_for_resume()` blocks on a thread-safe queue instead, unblocked by
`POST /runs/{run_id}/resume` from a completely different thread — same controller logic, a
different transport underneath, because §4 leaves architecture/transport as "your call."

**4. Resume and record.** `escalate()` wakes up, captures an **after** tree + screenshot,
diffs the two trees (`difflib`), and logs one `escalation_human_action` evidence event
carrying the diff, both screenshots, and the operator's note. `control` flips back to `AGENT`,
and the original loop (discovery's `for step_index in range(...)` inside `_run_loop()`, or
replay's `while index < len(artifact.steps):`) simply continues from where it was — the pause
was transparent to the calling code. *Satisfies:* §3.6's "preserve context and evidence across
the handoff, and record what the human did," without building the full co-browsing console the
assignment explicitly puts out of scope.

**5. If the transport itself fails.** `wait_for_resume()` can raise — concretely,
`LocalSharedBrowserTransport` does when stdin isn't interactive (live-observed during this
project's own testing). All four call sites that trigger step 1 above (`discovery/agent.py`,
`replay/engine.py` ×2, plus `cli.py`'s optional drift-diagnosis review escalation) catch this
and convert it into a structured, non-crashing outcome instead of a raw traceback — a discovery
run returns `RunTrace(succeeded=False)`, a replay returns `ReplayResult(HARD_FAILURE, ...)`,
and the (optional, best-effort) drift-diagnosis review just logs and returns, leaving the
underlying replay result untouched. See `ENHANCEMENTS.md §10` for the full fix and its tests.

---

## `config.py` — Settings

**What it is.** A single Pydantic `Settings` model (`Settings`) plus a loader
(`load_settings()`) that reads environment variables (via `.env`/`python-dotenv`, loaded in
`cli.main()`).

**Why it's needed.** §3.4 requires "an explicit, **configurable** allowlist." This file is
that configuration surface — every other module reads policy from `Settings` rather than
hardcoding it, so the allowlist/action list/redaction patterns can be changed per
deployment without touching code.

**Fields and what reads them:**
- `allowed_url_prefixes`, `allowed_action_types` — read by `Guardrail.check_allowlist()`
  (§3.4 allowlist enforcement). Default only allows `http://localhost:5000` (the mock app)
  and the five action types the schema defines.
- `redaction_patterns` — a single source of truth shared by **two different consumers**:
  `Guardrail.redact()` (one-way, `"[REDACTED]"`, used for logs/console — §3.4 "never persist
  secrets or raw sensitive data") and `SensitiveValueTokenizer` (two-way, used to keep raw
  values out of the LLM prompt — §3.4 "avoid leaking... sensitive data" applied to the model
  call itself, which is outside what §3.4 technically requires but closes an obvious real
  leak channel). Adding a new sensitive field (SSN, email…) is one regex added here, never a
  code change in either consumer.
- `model_provider`, `openrouter_*`, `nvidia_*` — which LLM backend `cli._build_llm_client()`
  wires up, and which model is used for text decisions vs. vision fallback/drift diagnosis
  (§4 "LLM provider/model... is your call"). **Defaults to `nvidia`**: OpenRouter's free tier
  caps the whole *account* at 50 model requests per day across every free model, and a single
  discovery run spends 15–20 of them, so two or three recordings exhaust it and everything
  afterwards fails on HTTP 429 regardless of which free model is named. Whichever provider is
  not chosen remains the automatic fallback when its key is present.
- `max_discovery_steps` — the discovery loop's stopping condition (§3.1 "max steps, timeout,
  dead-end").
- `artifacts_dir`, `evidence_dir` — where compiled artifacts (§3.2) and run evidence (§3.5)
  are persisted on disk.

`load_settings()` reads `os.environ` with defaults matching the `Settings` class defaults, so
the system runs out-of-the-box against the mock app with no `.env` file at all except for LLM
API keys.

---

## `schemas.py` — The artifact/capability data contracts

**What it is.** All Pydantic models shared across the system. This file is the single most
evaluated piece of the assignment (§3.2: "Design the schema deliberately; it's a focal point
of the evaluation").

**Why it's needed.** §3.2 requires the recorded flow to be a **typed, versioned, serializable
artifact** that is "an agent-invocable capability," not just a step list — it needs "a clear
contract." Every field here exists to answer one of the assignment's explicit bullet points.

**Key classes:**

- `RiskTier` (`SAFE` / `RISKY`) — §3.4's "distinguish safe/reversible from risky/irreversible
  actions." Attached per-`Step`, not per-artifact, because riskiness is a property of a
  specific action (clicking "Confirm Transfer"), not the whole capability.

- `LocatorStrategy` (`ROLE` / `TEXT` / `CSS`) + `Locator` — §3.2's "how each target
  element/control is identified (with your reasoning about robustness)." `Locator.fallback`
  is a self-referential optional field so a locator can degrade to a second strategy if the
  primary one stops resolving (drift tolerance), without needing a separate "backup locator"
  concept bolted on.

- `ValueSource` — the mechanism that lets an artifact express **typed input parameters**
  (§3.2 bullet 3) instead of baking in whatever literal value the LLM typed during discovery.
  `type: "goal_parameter"` means "substitute the caller's param at replay time";
  `type: "fixed"` means "this literal value is intentionally always the same" (e.g. a
  dropdown option that never varies). This is what lets `lookup_member` discovered against
  member `12345` correctly replay against member `67890` — see `discovery/compiler.py`.

- `CheckpointType` (`ELEMENT_VISIBLE` / `TEXT_PRESENT` / `URL_MATCHES`) + `Checkpoint` — §3.2
  bullet 5 and §3.3's "verify the checkpoint/success condition." A `Checkpoint` is "a
  condition you assert to confirm you actually reached the state you expected, rather than
  assuming the click worked" (per the glossary). Used both as `Step.checkpoint` (per-step,
  optional) and `Artifact.success_checkpoint` (required, the overall goal-reached condition).

- `ActionType` (`NAVIGATE` / `CLICK` / `TYPE_TEXT` / `SELECT_OPTION` / `EXTRACT`) — the closed
  vocabulary of things the agent (and replay) can do. `EXTRACT` is what lets an artifact
  declare **typed outputs** (§3.2 bullet 4): reading a value off the page (e.g. a
  confirmation number) rather than just performing an action.

- `Step` — one action in the recorded flow: which action, on what locator, with what value
  (or `value_source` for a parameterized value), what it's tagged to extract (`extract_as`),
  its `risk_tier`, and an optional per-step `checkpoint`. This is the "ordered steps/actions"
  from §3.2 bullet 1, carrying everything needed for both replay execution and human review.

- `InputParam` / `OutputParam` — the typed contract an AI agent calling this capability would
  see: name, primitive type, required-ness, description, example. This is literally the
  "typed input parameters" / "typed outputs... and their shape" language from §3.2, made
  concrete as a schema an API (`server/app.py`) can expose directly.

- `OutcomeType` (`INPUT_ERROR` / `SUCCESS` / `BUSINESS_OUTCOME` / `RECOVERABLE` /
  `HARD_FAILURE`) — the exact three-way split §3.3 demands: "expected business outcomes,"
  "recoverable conditions," and "hard failures," plus a fourth bucket
  (`INPUT_ERROR`) for bad caller input caught before any browser action runs. Renamed from
  `VALIDATION_ERROR` (commit `bff76be`) because on MERIDIAN the name actively misled: the
  host's own `?inject=validation` page and its "Please correct the following" field errors
  are *business outcomes* — the app correctly refusing a well-formed request — so the one
  thing `VALIDATION_ERROR` sounded like it covered was the one thing it did not.
  `INPUT_ERROR` names whose fault it is: the caller's, before a browser ever opened. This is the
  taxonomy the whole replay error-handling story (§3.3, evaluated under "Robustness & error
  handling") is built on. See the glossary's own callout: *"'no such member' is a legitimate
  answer the caller needs, not a crash. Conflating the two is the most common design mistake
  here."* — `OutcomeType.BUSINESS_OUTCOME` exists specifically to not make that mistake.

- `DerivedOutputSpec` (`from_output`, `op: Literal["sum_currency"]`), reachable as
  `OutputParam.derive` — declares an output *computed* from another already-extracted
  output rather than read off the page. `ReplayEngine` resolves these after every step has
  run, using pure text parsing and arithmetic: parse each `$X,XXX.XX` out of
  `from_output`'s text and sum them. The `Literal` matters — the op set is closed to
  exactly what replay can already do deterministically, so an artifact can never declare a
  computation that would need a model at replay time, and **"replay never invokes the LLM"
  stays literally true**. It exists because MERIDIAN's member record lists share balances
  with no total of its own: a "total balance" capability has to compute one. Discovery
  wires it up when the model sets `derive_as`/`derive_op` on an `extract` decision, so the
  artifact records the *declaration*, never a number the model itself worked out.

- `OutcomePattern` — a hand-authored rule: "if this checkpoint is currently true, the
  outcome is X, with this human-readable `detail`." Lets an artifact (or the host-level
  library) declare known non-happy-path states it can recognize (e.g. "insufficient funds,"
  "session expired") independent of which step number the app happened to diverge at.
  `outcome` accepts `BUSINESS_OUTCOME`, `RECOVERABLE` **and `HARD_FAILURE`** — the last so a
  target's own "something broke on our side" page (MERIDIAN's `APPLICATION ERROR`) can be
  *recognised* and reported in those words rather than surfacing as whatever locator happened
  to time out next; it is still a hard failure, just a legible one.
  `detail_locator: Locator | None` points at where the live, specific reason sits on the
  matched page. MERIDIAN names the rule that actually failed ("Insufficient available balance
  in the source share") in a list under a generic banner; without this the caller is told only
  the category, which for a rejected request is the less useful half of the answer.
  `ReplayEngine._match_outcome_pattern()` checks these before *and* after each step, because
  the app can diverge mid-sequence, not only at the very end. Also carries `max_retries` (int,
  **default 3**), `retry_delay_seconds` (float, default 0), and `recovery_action: Step | None`
  — this is what turns a `RECOVERABLE` match from something only ever detected-and-reported
  into something `ReplayEngine` can actually attempt to fix: perform `recovery_action` (if any)
  and retry the *same* step, up to `max_retries` times, before giving up and returning the
  outcome as before. These three fields are meaningful only when `outcome == RECOVERABLE` — a
  `business_outcome` like "no such member" is never retried regardless of what they're set to.
  `max_retries` defaulting to 3 (not 0) means every `RECOVERABLE` pattern — including ones
  written before this default existed, which set none of these fields — now gets a bounded,
  automatic "recheck and see if it cleared" for free, matching §3.3's "wait/retry a transient
  load" language literally rather than only classifying it. Set `max_retries=0` on a specific
  pattern to opt back out (a condition known to never clear on its own).

- `Artifact` — the top-level capability object: `capability_name`, `version` (int, bumped
  every re-discovery — §3.2 "versioned"), `target` (which app/base URL), `description`,
  `input_schema`/`output_schema`, `steps`, `success_checkpoint`, `outcome_patterns`, and
  `status: "draft" | "approved" | "rejected"` (§8's Confidence & approval stretch goal — gates
  whether an unreviewed version can ever be loaded for unattended replay; see
  `cli.load_artifact()`). `created_from_run_id` traces the artifact back to the discovery
  evidence that produced it (§3.5 observability tie-in).

- `ReplayResult` — the structured result contract §3.3 demands ("success with outputs, a
  known business outcome, or a failure with enough detail to debug"): `outcome`, `outputs`,
  `detail`, and on failure `step_index`/`expected`/`observed` — literally "what step, what was
  expected, what was observed" from the assignment text. `proposed_patch_version` is set only
  by the optional drift-diagnosis path (`drift.py`), never by `ReplayEngine` itself.

- `InterventionRequest` — §3.6's "raise an intervention request... carrying enough context to
  act on it": `run_id`, `capability_or_goal`, `current_step`, `screenshot_path`, `reason`.

---

## `surface.py` — Perception & action abstraction

**What it is.** The seam between "how we perceive/act on a UI" and everything above it that
reasons about steps/artifacts — exactly the seam §3.7 asks the write-up to identify
("What's the seam between 'how we perceive/act on a surface' and 'the recorded flow'?").

**Why it's needed.** §3.1 requires the agent to "actually interact with a real UI" via a
mechanism that "would still work when the surface has no clean DOM." Isolating all
page-touching code behind one `Surface` interface is what lets `PlaywrightSurface` (a browser
implementation) be swapped later for a desktop accessibility-API implementation or a legacy
frameset-aware implementation without touching `DiscoveryAgent` or `ReplayEngine` — the
"design for heterogeneity" §3.7 asks for, without building it.

**Key pieces:**

- `ObservedState` — a dataclass pairing `accessibility_tree` (a string snapshot) with `url`.
  This is what `observe()` returns — the accessibility tree, not raw HTML/DOM, is deliberately
  the primary perception channel because it's "often more stable than raw markup, and
  available on desktop apps too" (glossary) — the bias toward no-clean-DOM environments §3.1
  explicitly asks for.

- `_resolve(page, locator)` — translates a single `Locator` (role/text/css) into a live
  Playwright locator object, for exactly the strategy that locator names — no fallback
  handling here, that's `_resolve_with_fallback`'s job (below). `strategy == ROLE` uses
  `get_by_role(role, name=name, exact=True)` — `exact=True` is a deliberately fixed real bug
  (see REPORT.md): Playwright's default substring name match let a decoy "Advanced Search"
  button match a locator meant for "Search."

- `_FALLBACK_PROBE_TIMEOUT_MS = 3000` and `_resolve_with_fallback(page, locator, perform,
  is_success=lambda result: True)` — what makes `Locator.fallback` (see `schemas.py` below) an
  actual robustness mechanism rather than an unused schema field. Resolves `locator` and calls
  `perform(resolved_locator, timeout_ms)` on it; if that raises, **or** the result doesn't
  satisfy `is_success` (needed because Playwright's `is_visible()` returns `False` rather than
  raising when nothing matches — a raised-exception check alone wouldn't catch a checkpoint
  miss), it recurses into `locator.fallback` — walking a multi-level chain in full, not just
  one level deep. Every non-terminal attempt in the chain gets a short 3-second probe timeout
  (an attempt that has a fallback to try next shouldn't wait Playwright's full ~30s default
  before giving up); the terminal attempt (whichever locator has no further `.fallback`) gets
  `None`, i.e. Playwright's own default, since there's nothing left to fall back to. On total
  failure, it re-raises/returns the **primary** attempt's own outcome, not the fallback chain's
  — callers should see why the *recorded* locator failed, not a confusing error from a fallback
  that was never the artifact's real intent.

- `safe_screenshot(surface)` — a free function (not a method) wrapping `surface.screenshot()`
  in a try/except that returns `None` on failure instead of raising. Used everywhere a
  screenshot is "nice to have evidence" but must never crash a run that would otherwise
  succeed, fail cleanly, or escalate — most critically inside `EscalationController.escalate()`,
  since a broken screenshot must never block a human handoff (§3.6).

- `Surface` (abstract base) — `observe()`, `act()`, `check_checkpoint()`, `screenshot()`,
  `current_url()`, and `get_heading_text()`. This interface is intentionally small: it's the
  entire contract `DiscoveryAgent` and `ReplayEngine` depend on, so a new surface
  implementation only has to satisfy these six methods. `get_heading_text()` is the newest
  addition — a best-effort "what's this view's primary heading/title" query, added specifically
  so `cli._derive_success_checkpoint()` (see `cli.py` below) could stop reaching into
  `PlaywrightSurface.page` directly to answer that question itself. It's its own method rather
  than something derived from `observe()`'s tree text because "the main heading" is a different
  concept per surface (a DOM heading role vs. a native window title vs. a desktop control) —
  the same reasoning `check_checkpoint()` already follows for letting each surface interpret
  its own checkpoint types.

- `PlaywrightSurface(Surface)` — the concrete, built implementation:
  - `observe()` — reads `body.aria_snapshot()` (or `page.accessibility.snapshot()` on older
    Playwright) plus `page.url`.
  - `act(action, locator, target, text)` — dispatches on `ActionType`: `NAVIGATE` →
    `page.goto`, `CLICK`/`TYPE_TEXT`/`SELECT_OPTION`/`EXTRACT` all go through
    `_resolve_with_fallback`, `EXTRACT`'s result returned to the caller — this is how a
    capability's declared output actually gets read off the page. Every one of these
    transparently tries a locator's `.fallback` chain if the primary doesn't resolve. Three
    of the four dispatches carry a hard-won detail:
    - **`EXTRACT` uses `inner_text()`, not `text_content()`.** `text_content()` concatenates
      raw text nodes with nothing between them, so a shares table came back as
      `"103001-S0001Regular Shares$760.50HOLD103001-MMKT-2Money Market$4.00…"` — the right
      characters, unreadable as an answer. `inner_text()` returns it as the screen lays it
      out, rows on their own lines and cells tab-separated.
    - **`TYPE_TEXT` fills `text or ""`.** An empty value is a real intention (clear a
      pre-filled field, leave an optional memo blank). Passing `None` through raised
      `Frame.fill() missing 1 required positional argument: 'value'` — an error about our
      internals, not the page — which cost one recording its entire loop-detector budget on a
      single optional field.
    - **`SELECT_OPTION` goes through `_select_option_robust`** (below).
  - `_select_option_robust(l, text, timeout)` — tries three matches in order, because
    discovery only ever sees a `<select>` through the accessibility tree, which exposes
    *labels*, while Playwright's `select_option(str)` matches the `value` attribute:
    (1) by label — what discovery actually recorded; (2) by raw value — for hosts where the
    two coincide; (3) by label with a trailing `($1,234.56)` balance stripped from both
    sides, since MERIDIAN renders a share's *current balance* inside its option text and that
    moves between runs, leaving neither the recorded label nor its value matching anything.
  - `check_checkpoint(checkpoint)` — dispatches on `CheckpointType`: `ELEMENT_VISIBLE` goes
    through `_resolve_with_fallback` too (with `is_success=lambda result: result is True`, so a
    `False` from the primary locator's `is_visible()` also triggers the fallback, not just an
    exception), `TEXT_PRESENT` goes through `_text_is_present`, `URL_MATCHES` does a URL
    substring match. This is the concrete implementation of "verify the checkpoint/success
    condition" from §3.3.
  - `_text_is_present(page, needle)` — matches the page's **rendered** text
    (`page.inner_text("body")`) with runs of whitespace collapsed on both sides, falling back
    to `page.content()` only if the rendered text can't be read. It used to match the HTML
    *source*, which fails silently and looks exactly like a missing error state: MERIDIAN's
    403 carries the sentence `Operator profile <b>teller1</b> is not authorized to perform
    this\n             function.`, so `"…perform this function" in page.content()` is `False`.
    Two live edge cases were reported as bare hard failures for that reason alone, and every
    future anchor crossing a tag, entity or line wrap would have failed the same way. A
    deliberate consequence: text hidden with `display:none` no longer counts as present —
    source matching made hidden copy indistinguishable from a real error banner.
  - `screenshot()` / `current_url()` — thin passthroughs.
  - `get_heading_text()` — `page.get_by_role("heading").first.text_content(timeout=2000)`,
    wrapped in try/except returning `None` on any failure (no heading found, timeout, …) rather
    than raising; strips the result and treats an empty/whitespace-only heading as "none."

---

## `guardrail.py` — Safety & policy engine

**What it is.** The single place §3.4's guardrail requirements are enforced:
`AllowlistViolation` (exception) and `Guardrail` (the policy object).

**Why it's needed.** §3.4 requires the agent to "not act outside" an allowlist, to treat
risky/irreversible actions "conservatively," and to "never persist secrets or raw sensitive
data." `Guardrail` is called from **both** `DiscoveryAgent.run()` and `ReplayEngine.run()`, so
neither engine can silently diverge on what's allowed — one policy object, two call sites.

**Methods:**

- `check_allowlist(url, action_type)` — raises `AllowlistViolation` if the action type isn't
  in `settings.allowed_action_types`, or if the URL doesn't start with one of
  `settings.allowed_url_prefixes`. Called before every action in discovery and replay; in
  replay, an allowlist rejection is deliberately reported as `HARD_FAILURE` with a distinct
  `expected` string ("...to pass the URL/action allowlist") so it's never confused with a
  genuine locator/UI failure by the drift-diagnosis heuristic in `cli.py`.

- `requires_confirmation(risk_tier, confirm_risky)` — returns `True` only when a step is
  `RiskTier.RISKY` and the caller didn't pass `--confirm-risky`. This is the single decision
  point (§3.4's "handle the risky class conservatively... your call, justify it" — the choice
  made here is: **escalate to a human rather than silently block or silently proceed**) shared
  by discovery (escalates before performing the action) and replay (escalates before
  performing the action, using the same method).

- `redact(text)` — one-way redaction: applies every regex in `settings.redaction_patterns`,
  replacing matches with `"[REDACTED]"`. Used by `EvidenceLogger` (before anything touches
  disk) and by `DiscoveryAgent._print()` (before anything touches the terminal) — the
  assignment's "avoid leaking... sensitive data" applied to every place data could leave the
  process, not just the log file.

---

## `tokenizer.py` — Reversible tokenization for the LLM call

**What it is.** `SensitiveValueTokenizer` — a two-way version of the same redaction concept:
instead of replacing a sensitive value with `"[REDACTED]"` (destroying it), it replaces it
with a stable per-run placeholder (`[[TOK1]]`, `[[TOK2]]`, …) that can be mapped back later.

**Why it's needed.** This isn't explicitly required by name in §3.4, but it directly serves
"avoid leaking... sensitive data (this is regulated financial data)" for the one channel
`Guardrail.redact()` alone can't close: the *outbound* LLM API call. `Guardrail.redact()` runs
on the way *out* to logs/console; without a second mechanism, real account numbers, dollar
amounts, and transaction IDs would still be sent to a third-party model provider as part of
the accessibility-tree prompt during `decide_next_action()`. `SensitiveValueTokenizer` closes
that gap while still letting the model reason about the page (it sees "$[[TOK1]]", not
"$4,500.00" — same shape, same reasoning affordance, no real value transmitted).

**Methods:**

- `tokenize(text)` — runs every configured pattern (`redaction_patterns`, same list
  `Guardrail` uses) over `text`, replacing each match via `_replace()`.
- `_replace(match)` — memoizes value→token and token→value maps so the *same* real value gets
  the *same* token throughout a run (letting the model recognize "I've seen this account
  before" without ever seeing it).
- `detokenize(text)` — reverses the substitution using the token→value map.
- `detokenize_value(value)` — recursively detokenizes strings inside a nested dict/list (used
  on the LLM's returned `locator` dict, which may itself contain a tokenized value the model
  echoed back, e.g. inside a `text` match target).

**Called from:** `DiscoveryAgent.run()` tokenizes `observed.accessibility_tree` before
`decide_next_action()`, and detokenizes the model's `locator`/`target`/`text` immediately
after the decision comes back — a single, explicit boundary between "what the LLM reasoned
about" and "what actually drives the browser, gets risk-classified, and gets persisted into
the artifact." Explicitly **not** applied to the vision-fallback screenshot (documented, known
gap — pixels can't be regex-tokenized).

---

## `evidence.py` — Observability (§3.5)

**What it is.** `EvidenceLogger` — structured, append-only JSONL logging plus screenshot
capture, both redacted before they touch disk.

**Why it's needed.** §3.5 requires "a structured log of what the agent did and why, and at
least one richer signal on failure (screenshot, DOM snapshot, trace...)." This is that
mechanism, and it's the same object used by discovery, replay, and escalation, so every run
type produces evidence in the same shape under `evidence/<run_id>/`.

**Functions/methods:**

- `_redact_value(guardrail, value)` — a free function that recursively walks a `dict`/`list`/
  `str` value and applies `guardrail.redact()` to every string leaf. This is what lets
  `log_event()` redact arbitrarily nested event payloads (a raw LLM decision dict, a nested
  `Locator.value`, …) without each call site having to know the payload's shape.
- `EvidenceLogger.__init__(settings, guardrail, run_id)` — creates `evidence/<run_id>/` on
  disk immediately.
- `log_event(event_type, data)` — appends one redacted JSON line to `<run_id>/log.jsonl`.
  Every meaningful moment in a run — a decision, a skip, an escalation, an input error, a
  drift diagnosis — is logged this way, giving a full structured trace of "what the agent did
  and why."
- `save_screenshot(png_bytes, label)` — writes `<run_id>/<label>.png` if bytes were actually
  captured (screenshot capture is best-effort via `safe_screenshot()` upstream — this method
  itself just no-ops on `None`).

---

## `llm_client.py` — LLM providers, prompts, and tool schemas

**What it is.** Everything needed to actually call an LLM for a discovery decision (§3.1: "an
LLM-driven observe → decide → act loop") or a drift diagnosis (optional, §8-adjacent). This
is the file behind the assignment's non-negotiable requirement: *"the discovery run has to be
real. At least one genuine LLM-driven run against a live surface."*

**Constants:**
- `_TOOL_SCHEMA` — the OpenAI-compatible function-calling schema for `decide_next_action`:
  `action` (enum of the five `ActionType`s plus `finish`), `locator`, `target`, `text`,
  `value_source`, `extract_as`, `done`. Forcing a tool call (rather than parsing free text)
  is what makes the model's output land directly on the `Step`/`Locator`/`ValueSource` schema.
  **`locator.value` names its four legal keys (`role`/`name`/`text`/`css`) and sets
  `minProperties: 1`.** As a bare `{"type": "object"}` it accepted `{}`, so
  `{"strategy":"text","value":{}}` was *schema-valid output* — two different models emitted
  exactly that, repeatedly, until runs dead-ended. They were not disobeying the prompt; they
  were satisfying the schema they were given, and a schema beats prose every time.
- `_SYSTEM_PROMPT` — the instructions and worked examples (type_text, click, extract, finish)
  given to the model every turn, including the rule that `extract` must target the *value*
  cell, not its label — directly informed by a real failure mode encountered during testing.
- `_DRIFT_TOOL_SCHEMA` / `_DRIFT_SYSTEM_PROMPT` — the schema/prompt for the optional
  `diagnose_drift` call used only by `drift.py`: given a screenshot and an expected-but-failed
  locator, decide whether a control serving the same purpose still exists (drift) or is
  genuinely gone.

- `NVIDIA_REQUESTS_PER_MINUTE = 40` — the account's published ceiling, paced client-side by
  `_NVIDIA_RATE_LIMITER`.

**`RateLimiter` — pacing the primary provider.** A thread-safe **sliding-window log**: a
deque of the start timestamp of every request, evicting anything older than the window, and
sleeping until the oldest entry ages out when the window is full. A sliding window rather
than a token bucket because the provider states its limit that way, and a bucket's burst
allowance — 40 requests fired the instant a quiet period ends — is exactly what trips a
provider's own limiter; rather than a fixed window, which permits 2x the limit either side
of a boundary; and rather than a leaky bucket, which would pace a 13-call discovery to a
strict cadence and add ~20s of waiting to a run comfortably under quota.

Three details that are each load-bearing:
- **`time.monotonic()`, not `time.time()`** — a clock adjustment mid-run must not hand out a
  burst of free slots or stall every caller for a wall-clock hour.
- **The lock is never held while sleeping**, and the window is re-checked after waking:
  waiters would otherwise serialize and each sleep the full window in turn, and another
  thread may have taken the slot this one just waited for.
- **A slot is taken per HTTP attempt, inside `_post_with_retry`'s loop** — a retry is a real
  request, so pacing only the first attempt would let a retry storm sail past the limit.

Exposed through a `_rate_limiter()` hook on `_OpenAICompatibleClient` that returns `None` by
default; only `NvidiaNimClient` overrides it, returning the **module-level shared** instance
— the quota belongs to the API key, and every concurrent run builds its own client, so a
per-instance limiter would let N runs each send N x the limit. OpenRouter deliberately gets
none: it is the fallback, reached only when the primary is already in trouble, and
throttling it would add latency exactly when it hurts. Why pace at all rather than absorb
the 429: a 429 burns the retry budget and *then* fails the call over to OpenRouter, spending
a free tier capped at 50 requests/day across the whole account — so a burst here can exhaust
tomorrow's fallback too. Covered by 8 tests in `tests/test_llm_client.py`, including a fake
clock (a test that slept 60s to prove a 60s window would never be run) and a 4-thread
contention test.

**Functions:**
- `parse_decision(data)` — extracts the tool-call arguments from a raw chat-completions
  response, tolerating a model that returns plain JSON text instead of a real tool call (strips
  Markdown code fences, and falls back to `json.JSONDecoder().raw_decode()` to recover a valid
  JSON object even if the model appended trailing commentary after it — a real failure mode
  seen from a free-tier model).
- `_normalize_drift_diagnosis(raw)` — tolerates two more real, observed model deviations: a
  model that wraps its answer in `{"diagnose_drift": {...}}` instead of returning it flat, and
  one that uses `"reason"` instead of the schema's `"reasoning"` key.

**Classes:**
- `LLMClient` (interface) — `decide_next_action(...)`, `diagnose_drift(...)`.
- `FakeLLMClient` — a scripted client (list of canned decisions/diagnoses) used by tests and by
  most evidence-generation runs listed in REPORT.md, so the deterministic parts of the system
  can be exercised repeatedly without burning API quota — while the assignment's one
  non-negotiable *real* LLM run is still produced separately (see REPORT.md's evidence table).
- `_OpenAICompatibleClient(LLMClient)` — shared HTTP logic for any OpenAI-compatible chat
  completions endpoint (both providers below implement this exact shape): builds the
  text/vision prompt, decides which model to target (`_text_model()` vs `_vision_model()`
  depending on whether a screenshot is present — the **vision fallback** used when the
  accessibility tree alone doesn't produce a usable locator), and posts via `_post_chat()`.
  Both `chat()` and `_post_chat()` now route through a shared `_post_with_retry()` (
  `ENHANCEMENTS.md` §14) that retries a 429/5xx up to twice with backoff before giving up —
  previously any such response was treated identically to a genuine decision failure, so a
  handful of rate limits in a row (with only one provider configured, i.e. `FallbackLLMClient`
  not in play) could count toward `DiscoveryAgent`'s dead-end threshold and look like the agent
  was stuck when it wasn't. Subclasses only supply `_endpoint()`, `_headers()`, `_text_model()`,
  `_vision_model()`, `_provider_label()`.
- `OpenRouterClient` — talks to `openrouter.ai`. Default/primary provider.
- `NvidiaNimClient` — talks to `integrate.api.nvidia.com`. Exists purely as an automatic
  fallback provider so a rate limit on one free-tier provider doesn't stall the whole
  discovery run.
- `FallbackLLMClient` — wraps a `primary`/`fallback` pair: tries `primary`, and on **any**
  exception (rate limit, timeout, malformed response) retries `fallback`. This is what
  `cli._build_llm_client()` returns when both providers have API keys configured. Live-verified
  both directions per REPORT.md (a real NVIDIA timeout fell back to OpenRouter, hit a real
  rate limit there, then succeeded back on NVIDIA on a later call).

---

## `drift.py` — Optional: vision-based self-healing patch proposal

**What it is.** A small, **opt-in**, human-gated module that diagnoses whether a replay
`HARD_FAILURE` was caused by genuine UI drift (a control renamed/moved, not gone) versus a
real break — going beyond what §3 requires, toward §8's "Assisted fallback" stretch goal, but
scoped down: it never *executes* an LLM-recovered action; it only proposes a *patched
artifact* for a human to review.

**Why it's structured as a separate module (not inside `replay/engine.py`).** `ReplayEngine`
must stay fully LLM-free — that's the core "deterministic replay" promise of §3.3
("re-run the recorded flow **without invoking the LLM** for decisions"). `drift.py` only runs
*after* `ReplayEngine.run()` has already produced its final result, orchestrated by
`cli._diagnose_and_propose_patch()`, so the replay's own `ReplayResult.outcome` is never
touched by this module — a genuine failure stays `HARD_FAILURE` regardless of what drift
diagnosis concludes.

**Contents:**
- `DriftDiagnosis` (dataclass) — `patched_artifact: Artifact | None`, `reasoning: str`.
- `propose_drift_patch(llm_client, surface, artifact, failed_step_index)` — takes a screenshot
  of the live (already-failed) page, sends it plus the failed step's expected locator to
  `llm_client.diagnose_drift()`, and if the model reports `found=True` with a valid
  replacement `Locator`, returns a **deep-copied, unsaved** `Artifact` (`status="draft"`) with
  only that one step's locator patched. Never saves, applies, or replays anything itself —
  saving (as a new version) and human review/escalation are `cli.py`'s job.

---

## `cli.py` — Entrypoint: wires the whole pipeline together

**What it is.** The `comp-use` command-line tool and the shared functions the FastAPI server
(`server/app.py`) also calls directly. This is where §3.1–§3.6 actually come together into
one runnable end-to-end thread, as §5/§6 require ("a complete end-to-end vertical slice that
touches every core requirement").

**Artifact persistence helpers** (§3.2's "versioned and reviewable" + §8's approval workflow):
- `save_artifact(artifact, artifacts_dir)` — writes `artifacts/<capability_name>/v<N>.json`.
- `load_artifact(capability_name, artifacts_dir, version=None)` — with no version given,
  walks versions **newest-first** and returns the first with `status == "approved"`, raising a
  descriptive `FileNotFoundError` if none are approved yet. This is the mechanism that
  prevents an unreviewed draft (a fresh discovery, or a proposed drift patch) from ever being
  silently used by unattended replay — you must pass `--version` explicitly to load a draft.
- `approve_artifact` / `reject_artifact` / `retire_artifact` — state transitions
  (`draft→approved`, `draft→rejected`, `approved→retired`), each validating the artifact is
  currently in the expected starting state before flipping it. **No file is ever deleted** —
  every one of these adds/changes state, never removes data (a deliberate safety choice for
  regulated financial artifacts, per REPORT.md's Safety section). `retire_artifact` now sets a
  distinct `"retired"` status rather than reusing `"rejected"` (`ENHANCEMENTS.md` §14) — a
  retired version was live in production and deliberately withdrawn, not a draft that never
  shipped, and `approve_artifact` accepts re-approving a `"retired"` version (not a `"rejected"`
  one), the rollback path `Artifact.is_default` exists for.
- `next_artifact_version(capability_name, artifacts_dir)` — `max(existing versions) + 1`.

**Concurrency guard:**
- `_version_lock` (module-level `threading.Lock`) — serializes the
  "read-next-version-then-save" sequence in `run_discover()` and
  `_diagnose_and_propose_patch()` so two concurrent discover requests for the same capability
  (reachable once `server/app.py` exposes discover over HTTP) can't both compute the same
  "next version" number and have one silently clobber the other's save. Deliberately **never**
  held across the interactive `input()` approval prompt, so one stuck human doesn't block all
  other version-allocation activity process-wide.

**LLM wiring:**
- `_build_llm_client(settings)` — picks primary/fallback provider order from
  `settings.model_provider`, but falls back to whichever provider actually *has* an API key
  configured if the nominal primary doesn't (never builds a client guaranteed to fail).
  Wraps in `FallbackLLMClient` only if a fallback key exists.

**Checkpoint derivation:**
- `_derive_success_checkpoint(surface, fallback_url)` — after a successful discovery run,
  builds the artifact's `success_checkpoint` from the **final page's own `<h1>`/heading text**
  (an `ELEMENT_VISIBLE` checkpoint on a role="heading" locator) rather than the literal URL
  reached during discovery, since that URL would contain a run-specific dynamic segment (a
  member ID, a generated confirmation number) that a future replay with different params would
  never reproduce exactly. Falls back to a literal `URL_MATCHES` checkpoint only if no heading
  can be found, with a printed warning that this checkpoint won't generalize.

**The discover path (§3.1 + §3.2):**
- `run_discover(goal, start_url, capability_name, confirm_risky, transport, interactive)` —
  launches a headless/headed Chromium via Playwright, builds a `PlaywrightSurface`, an
  `EscalationController`, and a `DiscoveryAgent`, runs the agent loop
  (`agent.run(goal, start_url)`), and on success compiles the resulting `RunTrace` into an
  `Artifact` via `compile_artifact()`. The browser-using body runs inside `try: ... finally:
  browser.close()` (with `browser.close()` itself wrapped in its own try/except, so a
  close-time failure can never mask whatever exception is already propagating) — **not**
  `browser.close()` as a plain last line, which used to mean *any* unhandled exception during
  `agent.run()` (most concretely, the escalation-transport crash `agent.run()` no longer
  produces — see `discovery/agent.py` — but in principle anything else too) skipped cleanup
  entirely and leaked the Chromium process. Ensures the artifact always starts with an explicit
  `NAVIGATE` step to `start_url` (in case the agent's first recorded action was something
  else). If `interactive=True`, prompts "Approve as new default? [y/N]" — a direct,
  human-facing implementation of §8's draft→approved gate. Carries forward any hand-authored
  `outcome_patterns` from the immediately-previous version, since a fresh discovery run has no
  way to observe them itself (they're authored by watching real business/recoverable outcomes,
  not inferred from one successful trace) — without this, every re-discovery would silently
  drop them.
- `_run_discover(args)` — the CLI subcommand handler for `discover`, using the interactive
  `LocalSharedBrowserTransport`.

**The replay path (§3.3):**
- `run_replay(capability_name, params, confirm_risky, diagnose_drift_on_failure, transport,
  version=None)` — loads the artifact via `load_artifact(capability_name, artifacts_dir,
  version=version)`. With `version=None` (the default), that call already only ever returns an
  `approved` artifact (the latest one). With an explicit `version`, `load_artifact()` loads
  that exact file with **no** status filter, so `run_replay()` itself immediately checks
  `artifact.status == "approved"` and, if not, logs a `version_not_approved` evidence event and
  **raises `ValueError`** — before `validate_required_params` runs, before Chromium ever
  launches, and before any step of the loop executes. This is the CLI/library-level
  enforcement of "an explicit, pinned version must itself be approved to replay" — the same
  guarantee `load_artifact(version=None)` already gives every unpinned caller, now extended to
  the pinned case, which previously had no such check at all (any status could be loaded and
  replayed by version number). Past that gate: validates required params **before opening a
  browser** (`validate_required_params`), then launches Chromium and runs `ReplayEngine.run()`.
  On any non-`SUCCESS` outcome, captures a final screenshot for evidence. If
  `diagnose_drift_on_failure` is set and the outcome is `HARD_FAILURE`, calls
  `_diagnose_and_propose_patch()` (see below) — entirely optional and off by default. Same
  `try: ... finally: browser.close()` guarantee as `run_discover()` — see above. Both
  `run_discover()` and `run_replay()` now also wrap their whole body (everything after
  `pg_store.start_run()`) in a try/except that calls `pg_store.finish_run(status="error", ...)`
  before re-raising (`ENHANCEMENTS.md` §14) — previously an exception anywhere in the browser/
  LLM/artifact layer skipped `finish_run()` entirely, leaving the run's Postgres row (and hence
  the dashboard) permanently reporting `status="running"` for a run that had actually crashed
  minutes or hours earlier.
- `_run_replay(args)` — the CLI subcommand handler for `replay`; parses `--params` as JSON,
  passes `args.version` straight through to `run_replay()`, and prints the resulting
  `ReplayResult` as JSON. A `ValueError` from the version-approval gate propagates as an
  uncaught exception (the same pattern `approve_artifact`/`reject_artifact`/`retire_artifact`
  already use for "wrong starting state" errors), ending the CLI invocation without ever
  printing a `ReplayResult` — there's nothing to report, since no run was attempted.
- The `replay` subparser gained `--version` (`type=int, default=None`): omit it to keep today's
  "latest approved" behavior unchanged; pass it to pin a specific version, which must itself be
  `approved`.

**Drift diagnosis orchestration (optional, beyond §3 requirements):**
- `_is_action_locator_failure(artifact, result)` — narrows drift diagnosis to only the case
  where the recorded **action's own locator** failed to resolve (`result.expected == "{action}
  to succeed"`), explicitly excluding a checkpoint mismatch (the action succeeded, but the page
  afterward didn't look as expected — patching the action's locator wouldn't address that).
- `_diagnose_and_propose_patch(settings, evidence, escalation, surface, artifact, result)` —
  calls `drift.propose_drift_patch()`; if a plausible fix is found, saves it as a **new**,
  unsaved-until-now artifact version (draft status, via the same `_version_lock`-guarded
  next-version logic) and raises an `InterventionRequest` via `EscalationController.escalate()`
  so a human reviews the proposed patch before it's ever used — nothing here is auto-applied,
  and `result.outcome` (the actual replay's own verdict) is never modified. The `escalate()`
  call is wrapped: on a transport failure, logs an `escalation_transport_failed` evidence
  event, prints a manual-review instruction (`comp-use approve --capability-name ... --version
  N`), and returns early — the patch is already saved to disk by that point regardless, and the
  real, already-computed `result` from the replay itself is left completely untouched (this
  whole diagnosis pass is best-effort and optional per §9 of the assignment; a failure here
  must never crash `run_replay()` and lose the genuine replay result).

**Approve/serve subcommands:**
- `_run_approve(args)` — CLI handler for `approve`.
- `_run_serve(args)` — starts the FastAPI capability server (`server/app.py`) via `uvicorn`.

**`main()`** — builds the `argparse` CLI with four subcommands: `discover`, `replay`,
`approve`, `serve`, each with its own arguments (`--goal`, `--start-url`,
`--capability-name`, `--params`, `--confirm-risky`, `--diagnose-drift-on-failure`, `--version`,
`--host`, `--port`). Loads `.env` first via `load_dotenv()`.

**`_headless()`** — reads `COMP_USE_HEADLESS` to decide whether Playwright launches a headed
(visible, for demoing/handoff) or headless browser.

---

## `discovery/agent.py` — The observe → decide → act loop (§3.1)

**What it is.** `DiscoveryAgent` — the core LLM-driven loop that actually drives a live
browser to accomplish a natural-language goal, with `RunTrace` as its output record.

**Why it's needed.** This *is* §3.1, almost verbatim: "Run an LLM-driven observe → decide →
act loop against a live surface until the goal is met or a stopping condition is hit (max
steps, timeout, dead-end)." Every design choice in this file traces back to making that loop
both work and be safely/faithfully recordable into an artifact afterward.

**Helper functions:**
- `_optional_str(raw)` — normalizes an LLM-supplied field to `None` if it's empty/"none"/"null"
  (models say "null" as a literal string surprisingly often) or a stripped string otherwise.
- `_LOCATOR_ACTIONS` — the set of `ActionType`s that require a `Locator` (everything except
  `NAVIGATE`).
- `_RISKY_TARGET_HINTS = ("confirm", "delete")` — matched against a click's target/locator
  text to flag it `RISKY`. Deliberately targets the **commit** control (a "Confirm Transfer"
  button), not navigation toward it — an earlier version of this logic had it backwards
  (flagged the link that merely opens a form), which is documented in REPORT.md as a fixed
  bug.
- `_locator_from_decision(raw)` / `_value_source_from_decision(raw)` — defensively parse the
  model's raw JSON-ish `locator`/`value_source` fields into real `Locator`/`ValueSource`
  Pydantic objects, returning `None` (rather than raising) on anything malformed so the loop
  can skip-and-retry instead of crashing on a bad model response.
- `RunTrace` (dataclass) — `run_id`, `goal`, accumulated `steps: list[Step]`, `final_url`,
  `succeeded: bool`. This is the intermediate record between "what the agent actually did" and
  "the compiled `Artifact`" — kept separate from `Artifact` itself so `discovery/compiler.py`
  has a single, clear transformation step (§3.2's "decoupled from the raw model transcript").
- `_classify_risk(action, target, locator)` — applies `_RISKY_TARGET_HINTS` to
  `CLICK`/`NAVIGATE` actions to produce a `RiskTier`.
- `_DEAD_END_THRESHOLD = 3` — how many consecutive skipped/invalid decisions in a row count as
  "stuck" (§3.1's "dead-end" stopping condition, and one of §3.6's three escalation triggers).
- `_EscalationTransportFailed` — a small internal exception `_escalate()` raises when the
  transport itself fails (e.g. a real, live-observed `RuntimeError` when
  `LocalSharedBrowserTransport.wait_for_resume()` hits non-interactive stdin). Never allowed to
  surface as a raw traceback: `run()` (see below) catches it and treats it as a hard stop —
  there's no way to safely get human input at that point, so the run cannot safely continue.

**`DiscoveryAgent` class:**
- `__init__` — takes the `Surface`, `LLMClient`, `Guardrail`, `EvidenceLogger`, `max_steps`,
  an optional `EscalationController`, and `confirm_risky`. Builds its own
  `SensitiveValueTokenizer` seeded from `guardrail.settings.redaction_patterns` — tokenization
  lives at the agent level (not inside `Surface` or `LLMClient`) because it's specifically the
  boundary between "what got observed" and "what gets sent to the model."
- `_escalate(goal, current_step, reason)` — builds an `InterventionRequest` (with a fresh
  screenshot) and calls `self.escalation.escalate()` inside a try/except: a no-op if no
  `EscalationController` was supplied; on success, returns normally; on a transport failure,
  logs an `escalation_transport_failed` evidence event and raises `_EscalationTransportFailed`
  (see above) rather than letting the transport's own exception propagate raw.
- `_print(message)` — redacts before printing to the terminal, so real typed values (a member
  ID, a dollar amount) never appear in the clear in a CI log or shared screen recording — a
  gap the evidence JSONL itself didn't have (it was already redacted by `EvidenceLogger`) but
  the console output did, until this was added.
- `_note_skip(goal, step_index, consecutive_skips)` — increments a skip counter; at
  `_DEAD_END_THRESHOLD` consecutive skips, escalates with reason `"dead_end: N consecutive
  skipped/invalid decisions"` and resets the counter. This fires **mid-run**, not just at
  `max_steps`, so a genuinely stuck agent doesn't have to burn its entire step budget before a
  human is notified.
- `run(goal, start_url)` — a thin wrapper: calls `_run_loop(goal, start_url)` inside a
  try/except `_EscalationTransportFailed`. On that exception (already logged inside
  `_escalate()`), returns a clean `RunTrace(succeeded=False)` instead of letting it propagate.
  Split out from the loop itself specifically so this one `try/except` can catch a failure from
  *any* of the loop's several escalation call sites without reindenting the entire existing loop
  body (a large, otherwise-unnecessary diff for what's a narrow crash-proofing fix).
- `_run_loop(goal, start_url)` — the loop itself (this *is* what used to be `run()`):
  1. Navigates to `start_url`.
  2. For each step (up to `max_steps`): `observe()`s the page, optionally attaches a
     base64 screenshot if the **previous** turn flagged `needs_vision_fallback` (the
     accessibility tree alone wasn't enough to produce a usable locator last time — the model
     gets one more attempt at the *same* state, now with vision), tokenizes the observed tree,
     and calls `llm_client.decide_next_action()`.
  3. **LLM call failures** (rate limit, malformed response, network error) are caught, logged
     as a `skipped_decision`, and treated as a retryable skip — never a crash.
  4. `done`/`action == "finish"` ends the loop with `trace.succeeded = True`.
  5. Otherwise validates the decision step by step: unknown action → skip; invalid/missing
     `value_source` when one was supplied → skip (prevents baking a literal discovery-time
     value into the artifact by mistake); missing locator for a locator-requiring action →
     skip *and* set `needs_vision_fallback = True` for next turn; missing `target` for
     `navigate` → skip.
  6. Detokenizes the decision's `locator`/`target`/`text` back to real values — "the single
     boundary between what the LLM decided and what actually drives the browser."
  7. Runs `guardrail.check_allowlist()` — an `AllowlistViolation` is logged and skipped (§3.4
     enforcement inside the discovery loop itself, not only replay).
  8. Classifies risk via `_classify_risk`; if `requires_confirmation()` is true, escalates
     *before* performing the action (§3.6 trigger #1: "a risky/irreversible step needs a
     person to decide" — decided proactively, not after the fact).
  9. Performs the action via `surface.act()`; a raised exception is caught, logged, and treated
     as a skip that also sets `needs_vision_fallback = True` (the same underlying problem the
     vision fallback exists for: a locator that looked valid didn't actually resolve) - **unless**
     this exact step is the one that just escalated in step 8. In that case (`ENHANCEMENTS.md`
     §16, `IMPACTS.md` §5.17) the human almost certainly performed the action themselves during
     the handoff, so re-attempting it here always fails (the control it targeted is already gone
     from the page) - the step is still recorded in step 11 below (`risk_tier=risky`) instead of
     silently dropped, which previously meant an artifact recorded via a real, successful
     escalation could still ship with **zero** risky steps anywhere in it.
  10. **Re-checks `guardrail.check_allowlist()` a second time**, now against
      `surface.current_url()` *after* the action ran. Step 7's pre-action check validates an
      explicit `NAVIGATE` target correctly, but a `CLICK` has no explicit target — the pre-check
      can only validate the page the browser was already on, not where the click leads. Without
      this second check, a click that navigates off-allowlist was never validated at all (a
      real, found gap — not a documented cut). Unlike a pre-action rejection (which just skips
      the decision and lets the model retry), a post-action violation **aborts the whole
      discovery run** — the browser has already left the allowlisted domain and can't safely
      continue — logging `allowlist_violation_post_action`, escalating, and returning
      immediately with `trace.succeeded` still `False`.
  11. Appends a compiled `Step` to `trace.steps` — deliberately **not** persisting the literal
      typed value when it came from a `goal_parameter` (replay always substitutes the caller's
      own param instead), only persisting a literal `value` for genuinely fixed values.
  12. If `max_steps` is exhausted without `finish`, escalates with reason `"stuck: reached
      max_steps..."` — §3.6 trigger #2.
  13. Returns the final `RunTrace`, including `final_url`.

**Crash-proofing.** `REPORT.md` claims every external call in `DiscoveryAgent` is wrapped so
"nothing crashes the CLI with a raw traceback" — that used to be false for `self.escalation
.escalate(...)` specifically, called (via `_escalate()`) from three trigger points: dead-end,
risky-step, and max-steps-exhausted. All three now go through the same `_escalate()` →
`_EscalationTransportFailed` → `run()`'s catch path described above, closing the gap. See
`ENHANCEMENTS.md §10` for the fix across all four call sites in the codebase (this file has one;
`replay/engine.py` has two; `cli.py` has one more, for the drift-diagnosis review escalation).

### Fault classification during discovery

Discovery used to recognise **none** of the host's error states. It drove on through an
injected fault or a natural rejection as if the error page were simply an unfamiliar screen,
and whatever the model improvised next was recorded as canonical steps — an early
`meridian_check_balance` recording contained a failed sign-on *and its retry* that way. The
damage is not a missing label; it is a corrupted artifact.

`_run_loop` now resolves `outcome_patterns_for_target({"app": urlparse(start_url).netloc})`
once at the top, and after every action — **before the step is recorded** — checks whether the
resulting page matches one:

- **`RECOVERABLE` with a `recovery_action`** → perform it and let the model re-decide. The
  recovery click is deliberately *not* recorded as a step: it is a property of the host being
  briefly unwell today, not of the capability being learned.
- **anything else** (`BUSINESS_OUTCOME`, `HARD_FAILURE`, un-recoverable) → escalate with the
  host's own words (*"the host reported: Sign-on was rejected: invalid operator ID or
  password."*) and do **not** record the step. This is also the "ask the operator when you hit
  something unknown" path: a human can correct the input, sign on with the right role, or stop.

Every case logs a `fault_detected` event with its outcome. Verified live: 5/5 injected and
natural faults classified, and **no failing run produced an artifact**.

### Recording-quality gates

Each of these exists because a capability shipped broken without it, and each is *bounded* —
they can push back on the model, never trap it:

- **`_unusable_extract_reason(locator, extracted)`** rejects an extract locator keyed on the
  value it just read (the next confirmation number is a different string, so `{"text":
  "CN480332"}` matches nothing next run) or one that grabs the whole page (`body`, `html`).
  The value-keyed test measures *how much* of the captured text the anchor reproduces
  (`_VALUE_KEYED_RATIO = 0.8`) rather than whether it appears at all — an earlier `anchor in
  captured` test also rejected the *correct* answer, since a stable label like `"Signed on
  as"` is legitimately a substring of `"Signed on as J. TELLER (TELLER)"`, which left
  discovery oscillating for eight turns between a locator it had been refused and table
  selectors that matched nothing. Bounded by `_MAX_EXTRACT_REJECTIONS = 2` **per run**, not
  per `extract_as` — the model renames the output when it retries, which slipped straight past
  a per-name bound.
- **The `finish` gates.** A finish is refused once if nothing was extracted (three read-only
  capabilities shipped with an empty `output_schema` and replayed "successfully" with nothing
  to show), and once if a `param_hints` entry was never actually entered (an Update Member
  Information recording opened the pre-filled form and went straight to Save Changes,
  submitting the record unchanged while reporting `MEMBER INFORMATION UPDATED`).
- **Neither gate fires once an irreversible step has run.** `committed = any(step.risk_tier ==
  RISKY …)`; when true the finish is final and logs `finish_accepted_after_commit`. A posted
  funds transfer (confirmation already in hand) was pushed back for a missing memo and went
  back around to begin a *second* transfer, stopping only because it ran out of steps. An
  incomplete recording is a bad artifact; a duplicated financial transaction is a different
  category of problem.
- **The extracted value is fed back to the model**, tokenized. Without it the history said
  only "I decided to extract", so on a page that doesn't change as a result the model
  re-decided the identical extract until the loop guard tripped. Regulated values stay masked,
  but the entry now carries an `extracted_note` explaining that `[[TOK…]]` marks a value that
  *was* captured and redacted — without it, a balance table read as a failed extract and the
  balance capability tried ten progressively more elaborate selectors over six minutes, each
  of which had already worked.

---

## `discovery/compiler.py` — RunTrace → Artifact (§3.2)

**What it is.** One function, `compile_artifact(trace, capability_name, target,
success_checkpoint, output_schema)`, that turns a raw `RunTrace` (a list of `Step`s the agent
happened to perform) into a reviewable, typed `Artifact`.

**Why it's a separate module from `discovery/agent.py`.** §3.2 explicitly requires the
artifact to be "decoupled from the raw model transcript" — this function is that decoupling
boundary. `RunTrace` is the transcript-adjacent record; `Artifact` is the clean, reviewable
capability contract. Keeping them in separate modules makes that separation structural, not
just conceptual.

**What it does:**
1. Walks `trace.steps` collecting every distinct `value_source.param_name` where
   `value_source.type == "goal_parameter"`, and builds an `InputParam` for each — this is how
   an artifact's declared input schema is *derived from the discovery run itself* rather than
   hand-written after the fact.
2. Walks `trace.steps` again collecting every `extract_as` name from `EXTRACT` steps, building
   an `OutputParam` for each (deduplicated against any output params already passed in).
3. Constructs and returns the `Artifact`, setting `description` to the original natural-language
   `goal` (so a human/agent reading the artifact later can see what it was meant to
   accomplish) and `created_from_run_id` to `trace.run_id` (traceable back to the evidence
   directory of the discovery run that produced it). `outcome_patterns` is deliberately left
   **empty** for anything the host-level library already covers — `ReplayEngine` resolves that
   library live on every run, so a fix to a shared pattern reaches every capability
   immediately rather than only those recorded after it (see `outcome_library.py` below). The
   field now holds only patterns specific to *this* capability.

**Two dedup passes, in order**, both applied to `trace.steps` before the artifact is built:
- `_dedup_consecutive_steps` — collapses exact-adjacent duplicates (the same locator/action/
  value decided twice in a row, observed live on Funds Transfer). Only *adjacent* exact
  duplicates, since a repeated action elsewhere in a flow may be a genuinely distinct step.
- `_drop_superseded_extracts` — keeps only the **last** `EXTRACT` for any given `extract_as`.
  A model that doesn't get the locator right first time refines it and retries, and every
  attempt was being recorded — observed as four extracts all named `shares_and_balances` with
  progressively narrower selectors. Dropping the earlier ones is output-preserving by
  construction (replay writes `outputs[extract_as]` per extract, so only the last one's value
  ever survived), and each one removed is one fewer locator that can time out and fail a run
  for a value nothing reads.

---

## `discovery/outcome_library.py` — A target-level `OutcomePattern` library (§2.2, §3.3)

**What it is.** One function, `outcome_patterns_for_target(target: dict) -> list[OutcomePattern]`,
keyed on `target["app"]` (the artifact's recorded hostname), returning a hand-curated library of
`OutcomePattern`s for hosts this module recognizes. An unrecognized host (the mock Flask bank, a
brand-new tenant) gets an empty list back, unchanged from before this module existed.

**Resolved live, per run — not snapshotted into artifacts.** `compile_artifact()` used to copy
this library into every freshly discovered artifact's `outcome_patterns`. That meant a fix to a
shared pattern only ever reached capabilities recorded *after* it, and `cli.py`'s
carry-forward then propagated the stale copy into each new version. Concretely: one wrong anchor
(below) stayed baked into all ten already-recorded capabilities and could not be corrected
without re-recording every one of them. Now `compile_artifact()` leaves the field empty for
anything the library covers, `ReplayEngine._match_outcome_pattern()` resolves the library live
on every run, and `Artifact.outcome_patterns` holds only patterns specific to *that* capability
(checked first, since they are more specific than the host-wide taxonomy behind them). See
`IMPACTS.md` §5.19 for the scaling trade-off this represents.

**Why it exists.** `ReplayEngine._match_outcome_pattern()` (see `replay/engine.py` below) has
always correctly distinguished a business outcome from a recoverable condition from a hard
failure — the mechanism §2.2 of the Adaptation brief calls "the load-bearing part" was fully
built. But nothing populated `outcome_patterns` on a freshly discovered artifact; only
*carrying forward* a previous version's hand-authored patterns existed (`cli.py`'s
`run_discover`). Verified against the shipped artifacts before this fix: 6 of 7 `meridian_*`
capabilities had `outcome_patterns: []`, so every exceptional state on any of them collapsed to
`HARD_FAILURE` regardless of its real cause. See `ENHANCEMENTS.md` §15 / `IMPACTS.md` §5.16 for
the full reasoning.

**The nine patterns currently shipped**, every one anchored on page copy captured from the live
target, and every one verified end to end (12/12 edge-case replays, 5/5 discovery-phase faults):

| Anchor | Outcome | Reached by |
|---|---|---|
| `"RECORD NOT FOUND"` | `BUSINESS_OUTCOME` | `?inject=notfound` |
| `"No member records matched your search"` | `BUSINESS_OUTCOME` | a search with no hits |
| `"is not authorized to perform this function"` | `BUSINESS_OUTCOME` | `?inject=permission`; a teller attempting Place Hold |
| `"Invalid operator ID or password"` | `BUSINESS_OUTCOME` | bad sign-on |
| `"The transaction could not be validated"` | `BUSINESS_OUTCOME` + `detail_locator` | HOLD share, overdraw |
| `"Please correct the following"` | `BUSINESS_OUTCOME` + `detail_locator` | invalid e-mail/phone on update |
| `"TRANSACTION REJECTED"` | `BUSINESS_OUTCOME` | `?inject=validation` |
| `"SCHEDULED MAINTENANCE IN PROGRESS"` | `RECOVERABLE` + `recovery_action` | `?inject=maintenance` |
| `"YOUR SESSION HAS TIMED OUT"` | `RECOVERABLE`, `max_retries=0` | `?inject=timeout` |
| `"APPLICATION ERROR"` | `HARD_FAILURE` | `?inject=server` |

Three things about this table are load-bearing:

- **The permission anchor is the denial SENTENCE, never the banner.** It was originally
  `"SUPERVISOR OVERRIDE REQUIRED"` — which the *legitimate* Place Account Hold form also carries
  as a "RESTRICTED FUNCTION" warning label. Every Place Hold replay therefore matched it the
  moment it reached the form, reported "operator is not authorized" while signed on as `super1`
  who *is* authorized, and gave up before performing a single step. Anchors are substring tests
  over a whole page, so a too-generic one is an outage, not a near-miss.
  `tests/test_outcome_library.py::test_healthy_pages_match_nothing` is the guard.
- **The two rejection banners are distinct.** Transaction-level ("could not be validated") and
  field-level ("Please correct the following") are different screens reached by different flows.
  Both carry a `detail_locator` (`font.err + ul`) so the report names the rule that actually
  failed — *"Insufficient available balance in the source share"* — rather than only the category.
- **`HARD_FAILURE` is a legal pattern outcome.** The host's own APPLICATION ERROR screen is still
  a hard failure, but a *recognised* one, reported in those words instead of as whichever locator
  happened to time out next on a page the run never expected to see.

Because this is a target-level library, every current and future MERIDIAN capability gets all
ten checks — during replay *and*, as of the fault-classification work below, during discovery.

---

## `replay/engine.py` — Deterministic replay (§3.3)

**What it is.** `ReplayEngine` — the production execution path: given a saved `Artifact` and a
`params` dict, replays the recorded steps **with no LLM call anywhere in this file**, and
returns a structured `ReplayResult`.

**Why it's needed.** This is §3.3's core requirement verbatim: "Given a saved artifact and a
set of input parameters, replay it without invoking the LLM for decisions... using stable
element/control targeting, verify the checkpoint/success condition, and return any declared
outputs." It's also the file evaluated hardest under "Robustness & error handling," since it's
where the business-outcome/recoverable/hard-failure taxonomy is actually enforced.

**`validate_required_params(artifact, params)`** — a free function (not a method) so it can be
called from `cli.run_replay()` **before** a browser is even launched (skip the cost of opening
Chromium on a request that's already invalid) *and* from `ReplayEngine.run()` itself (so the
engine is correct standalone, independent of the CLI's pre-check), *and* from the chat agent's
confirm-and-run path (`server/app.py`) before trusting an LLM tool call's params. Beyond
presence, it now also checks each supplied value against its declared `InputParam.type`
(`ENHANCEMENTS.md` §14) — previously only presence was checked, so an LLM-proposed `amount` like
`"one hundred"` for a numeric field would flow straight into a live form with nothing rejecting
it first. Returns a human-readable error string or `None`.

**`ReplayEngine`:**
- `__init__(surface, guardrail, evidence_logger, escalation=None)` — same `Guardrail` and
  `Surface` abstractions used by discovery, so replay enforces identical allowlist/risk policy
  without a second implementation of either.
- `_match_outcome_pattern(artifact)` — checks every `OutcomePattern.checkpoint` against the
  live page; returns the **matching `OutcomePattern` itself** (not a pre-built `ReplayResult` —
  changed from an earlier version specifically so the retry logic below can inspect
  `pattern.max_retries`/`recovery_action` before deciding whether to give up), or `None`.
  Checks `artifact.outcome_patterns + outcome_patterns_for_target(artifact.target)` — the
  artifact's own first, since anything hand-authored for *this* capability is more specific
  than the shared host taxonomy behind it. Resolving the library here rather than reading a
  copy frozen into the artifact is what lets a pattern fix reach capabilities recorded before
  it existed. Called from four places in `run()`: before each step (the app may have already
  diverged from a previous step, e.g. onto "insufficient funds"), after a raised exception
  during an action, after a checkpoint miss, and in the final success-checkpoint check —
  because a real app can reach a known non-happy-path state at any of those points, not just
  "the very end." On an action failure it is consulted **first**, before the
  `escalated_for_this_step` "the human probably did it" assumption: a transfer whose source
  share was on HOLD escalated correctly, the human resumed without fixing anything, and the
  "Post Transfer" click then failed only because the rejection page has no such button —
  assuming success there recorded a success-shaped event for a transfer that never posted.
- `_pattern_detail(pattern)` — the pattern's category text plus the live reason read off the
  page when it declares a `detail_locator`. *"The transaction could not be validated"* names
  only the category; *"Insufficient available balance in the source share"* is what a caller
  can act on. Best-effort — a page that no longer renders the reason must not turn a clean
  business-outcome report into a crash.
- `_unclassified_failure(outputs, recovered_from, …)` — builds the result for a failure no
  pattern explains. Normally `HARD_FAILURE`, but **`RECOVERABLE` if a recoverable condition
  was already met and its recovery performed earlier in this run**: an injected maintenance
  interstitial was matched and its "Continue" link clicked correctly, which returned the host
  to a *signed-out* page, so every later step failed against a page showing no error at all.
  Blaming the automation for a transient host condition it recognised and handled is wrong;
  the caller is told to re-invoke. Deliberately **not** solved by restarting from step 0 — a
  flow whose earlier steps are irreversible must never be silently re-run. It also carries
  `outputs` on every result: a run that read three values and then died still knows those
  three values, and dropping them made a partial answer indistinguishable from none.
- `_attempt_recovery(artifact, index, pattern, confirm_risky)` — best-effort: sleeps
  `pattern.retry_delay_seconds` if set, then performs `pattern.recovery_action` via
  `surface.act()` if one is declared (e.g. click a "Try Again"/"Start over" control). A
  `recovery_action` is a real action against the live surface, so — unlike the rest of this
  method, which is deliberately best-effort — it gets the same two guardrails every normal step
  in `run()` gets, **never silently skipped**: `guardrail.check_allowlist()` runs both before
  *and* after the action (see the post-action re-check note below), and
  `guardrail.requires_confirmation()` escalates via `EscalationController` first if the
  recovery action's own `risk_tier` requires it. Both of these deliberately let their
  exceptions **propagate out** of this method (an `AllowlistViolation`, or whatever a failed
  transport raises) rather than being caught here — a policy breach or a failed escalation must
  hard-stop the run, never be silently retried or softened into a mere `recoverable` report.
  Only the actual `surface.act()` call is genuinely best-effort: its failure is logged
  (`recovery_action_failed`) and swallowed, and a success is logged
  (`recovery_action_performed`) — if recovery genuinely didn't work, the next
  `_match_outcome_pattern` check simply sees the same recoverable state again, and the bounded
  retry budget eventually gives up and reports it.
- `_should_retry(artifact, pattern, index, retries_used, confirm_risky)` — the single decision
  point for "retry or give up": `True` only if `pattern.outcome == RECOVERABLE` and
  `retries_used[pattern]` is still below `pattern.max_retries` (default 3 — see `schemas.py`);
  in that case it logs a `recoverable_retry` evidence event, calls `_attempt_recovery` (which
  can raise — see above), increments the counter, and returns `True`. Otherwise `False` —
  either because it's a `business_outcome` (never retried: "no such member" isn't fixed by
  retrying), or the pattern's author set `max_retries=0`, or the budget is exhausted.
  `retries_used` is a `dict[id(pattern), int]` scoped to one `run()` call.
- `_handle_matched_pattern(artifact, pattern, index, retries_used, confirm_risky)` — the single
  place that converts "a pattern matched" into either `None` (retry — caller should `continue`)
  or a final `ReplayResult`. Wraps `_should_retry()` in two `except` clauses, in order: an
  `AllowlistViolation` (a recovery action tried to leave the allowlist) becomes `HARD_FAILURE`
  with `expected="recovery_action to pass the URL/action allowlist"`; any other `Exception` (in
  practice, an escalation transport failure — see below) becomes `HARD_FAILURE` with a distinct
  `expected="recovery_action's escalation to complete"`, so the two causes stay distinguishable
  in evidence even though both are "the recovery path itself broke," not "recovery didn't work."
  Used at all four places in `run()` that check for a matched pattern.
- `run(artifact, params, confirm_risky=False)` — the replay loop. Structured as a `while index
  < len(artifact.steps)` (not a `for`) specifically so a retry can `continue` back to the **same**
  step index instead of advancing — every retry site below does exactly that:
  1. `validate_required_params()` — returns `VALIDATION_ERROR` immediately if params are
     missing, before any step runs.
  2. For each step, in order (index only advances on a fully successful step):
     - Checks outcome patterns first (mid-sequence divergence detection). If matched and
       `_should_retry()` says yes, `continue`s (retries this same step); otherwise returns
       `ReplayResult(outcome=pattern.outcome, detail=pattern.detail)`.
     - If `requires_confirmation()` is true for this step's risk tier, escalates via
       `EscalationController` *before* performing it (§3.6 trigger #3: "a replay hits a
       condition it can't recover from, or a risky/irreversible step needs a person to
       decide") — wrapped in its own try/except: a transport failure (e.g. a real,
       live-observed `RuntimeError` when stdin is non-interactive) returns `HARD_FAILURE`
       with `expected="escalation to complete (human confirmation for a risky step)"`
       instead of crashing `run()` with a raw traceback.
     - Resolves the actual value to type/select: either the step's literal `value`, or —if
       `value_source.type == "goal_parameter"`— the caller's own `params[param_name]`. If a
       step references a param name that's missing (a case `validate_required_params` doesn't
       catch, since it only checks entries the *schema* marks `required=True`), returns a
       `VALIDATION_ERROR` with `step_index` rather than letting a raw `KeyError` crash the run.
     - Runs `guardrail.check_allowlist()`; a violation is reported as `HARD_FAILURE` with a
       distinguishing `expected` string ("...to pass the URL/action allowlist") so
       `cli._is_action_locator_failure()` never mistakes a policy rejection for UI drift.
     - Calls `surface.act()`. Any output is collected under `step.extract_as` in the
       `outputs` dict returned on success. A raised exception is checked against outcome
       patterns first (the app may have crashed *into* a known state, e.g. a stale-token
       error page) — retry-or-report, same as above — then, if still unmatched, reported
       `HARD_FAILURE` with `step_index`, `expected` (the standard `"{action} to succeed"` string
       `cli.py` keys off of for drift diagnosis), and `observed` (the current URL).
     - Logs a `replay_step` evidence event.
     - **Re-checks `guardrail.check_allowlist()` a second time**, now against
       `surface.current_url()` *after* the action ran — not just before. The pre-action check
       above validates an explicit `NAVIGATE` target correctly, but a `CLICK` has no explicit
       target: the pre-check can only validate the page the browser was already on, not where
       the click leads. Without this second check, a click that navigates off-allowlist was
       never validated at all — closed as a real, found gap (not a documented cut). A
       post-action violation returns `HARD_FAILURE` with a distinguishing `expected` string
       (`"{action} result to stay within the URL/action allowlist"`, vs. the pre-check's
       `"{action} to pass the URL/action allowlist"`).
     - If the step declares its own `checkpoint`, verifies it; a miss is checked against
       outcome patterns (retry-or-report again), else reported `HARD_FAILURE` with
       `expected`/`observed`.
  3. After all steps, a second `while` loop re-verifies `artifact.success_checkpoint`; a miss
     goes through the same outcome-pattern retry-or-report path (so even a `RECOVERABLE`
     pattern matched only after the very last step gets its retry budget) before falling
     through to `HARD_FAILURE`.
  4. Returns `ReplayResult(outcome=SUCCESS, outputs=outputs)` once the success checkpoint holds.

This structure is exactly the "distinguish expected business outcomes / recoverable
conditions / hard failures" split §3.3 asks for, made concrete: `OutcomePattern` checks are
tried at every meaningful point *before* anything is allowed to fall through to
`HARD_FAILURE` — and, as of the retry logic above, a `RECOVERABLE` match is no longer only
ever *detected and reported*; it's given a real, bounded chance to actually resolve first,
literally implementing §3.3's "wait/retry a transient load" / "dismiss a known interstitial"
language rather than only classifying it. Live-verified against the mock app's real stale
review-token condition (`README.md`'s "Exercising every outcome" step 8 / `demo_edge_cases.py`)
— both the automatic default-3-retries-then-give-up path and a real `recovery_action` genuinely
clicking a real element and clearing the condition.

**Crash-proofing, extended to escalation.** `REPORT.md` claims every external call in this
engine is wrapped so "nothing crashes the CLI with a raw traceback" — that claim used to be
false for `self.escalation.escalate(...)` specifically, at both call sites in this file (the
risky-step escalation above, and the recovery-action escalation inside `_attempt_recovery`).
`LocalSharedBrowserTransport.wait_for_resume()` genuinely raises `RuntimeError` on
non-interactive stdin — live-observed during this project's own testing. Both sites are now
wrapped: the risky-step one inline in `run()`, the recovery one via `_handle_matched_pattern`'s
broadened `except Exception` clause described above. See `ENHANCEMENTS.md §10` for the full
fix across all four call sites in the codebase (`discovery/agent.py` and `cli.py` have the
other two).

---

## `escalation/controller.py` — Human handoff state machine (§3.6)

**What it is.** `EscalationController` + `ControlState` — the mechanism that pauses
automation, notifies a human, waits, and resumes on the **same live session** — the seam §3.6
explicitly calls out: *"automation must be able to pause, cede control, and resume on the same
session, and there must be a way to know who is (or should be) in control."*

**`ControlState`** (`AGENT` / `HUMAN` / `NONE`) — the explicit "who is in control right now"
flag §3.6 asks for.

**Helper functions:**
- `_diff_trees(before, after)` — a unified diff (via `difflib`) between the accessibility tree
  observed immediately before and immediately after the human's turn. This is what lets the
  system "record what the human did" (§3.6) without building a full click-by-click recorder:
  a structural diff of the page state is a cheap, real signal of what changed.
- `_safe_observe_tree(surface)` — wraps `surface.observe()` in try/except, same reasoning as
  `safe_screenshot`: bringing a human in is "the single most safety-critical path in the
  system" (comment in the source) and must never itself crash, even against an already-broken
  page.

**`EscalationController`:**
- `__init__(evidence_logger, transport, surface=None)` — `control` starts at `AGENT`.
- `escalate(request)` — the full handoff sequence:
  1. Sets `control = HUMAN`, logs `escalation_requested` with the full `InterventionRequest`
     (run_id, capability/goal, current step, screenshot, reason — exactly the context §3.6
     requires: "which capability/goal, the current step, the current state or screenshot, and
     why it stopped").
  2. Captures a **before** accessibility tree and screenshot from the **live surface** (not a
     fresh one — this is the "same session" requirement).
  3. Calls `transport.notify(request)` — tells a human (however the transport implements
     that) that intervention is needed.
  4. Blocks on `transport.wait_for_resume()` — this call is what actually cedes control; the
     automation thread is parked here while a human operates the same browser/session.
  5. On resume, logs `escalation_resumed`.
  6. Captures an **after** tree/screenshot, diffs the trees if both were captured
     successfully, and logs one `escalation_human_action` event carrying the operator's
     free-text note, the tree diff, and both screenshot paths — "preserve context and evidence
     across the handoff, and record what the human did" (§3.6), without a full co-browsing
     recorder (explicitly out of scope per §3.6's own scope note).
  7. Sets `control = AGENT` — hands control back, allowing the calling loop (discovery or
     replay) to resume where it left off.

  Step 7 now happens in a `finally` (see `ENHANCEMENTS.md` §14) — an exception from
  `notify()`/`wait_for_resume()` or any surrounding evidence call previously left `control`
  stuck at `HUMAN` forever, with no way back; the diff in step 6 is also capped at 200 lines
  now (a handoff onto an entirely different page could otherwise produce a diff that's
  essentially both trees concatenated, unbounded, into one evidence event).

Both `DiscoveryAgent` and `ReplayEngine` call `escalate()` through this one controller, so the
handoff mechanism (and its evidence trail) is identical regardless of which engine got stuck.

---

## `escalation/transport.py` — How "notify" and "wait for resume" are carried

**What it is.** The pluggable transport layer behind `EscalationController` — abstracting
*how* a human is notified and *how* the system waits for them, so the same controller logic
works whether the human is at the same terminal (CLI) or an HTTP client (server).

**`ControlTransport`** (interface) — `notify(request)`, `wait_for_resume() -> str`, plus five
optional hooks that default to no-ops because only the server has a UI to wire them to:
`request_takeover()` / `takeover_requested()` / `clear_takeover()`, `interrupt()` /
`interrupt_requested()`, `cancel()`, and `on_novnc_url(url)`.

**Three ways a human interrupts a run, and they are three different intents.** All are
polled or raised at a *step boundary*, never mid-action, so a run never tears in half:

| Method | Intent | Effect | Terminal status |
|---|---|---|---|
| `request_takeover()` | Pause; drive the same live session, hand it back | `DiscoveryAgent`/`ReplayEngine` poll `takeover_requested()` once per step and escalate with reason `"manual takeover requested by operator"` — the ordinary escalation path, so the same `resume()` continues the run | `done` |
| `cancel()` | Unblock an escalation nobody will resume, so the run can finish *reporting* | `wait_for_resume()` raises `EscalationAbandoned` | `error` |
| `interrupt()` | End the run outright | Both loops call `escalation.raise_if_interrupted()` once per step, which raises `RunInterrupted` | `interrupted` |

`clear_takeover()` is called by `EscalationController.escalate()` whenever a pause actually
happens, so an honoured takeover can never fire a second, redundant escalation. Nothing
clears an interrupt — the run is over.

**`RunInterrupted` subclasses `BaseException`, not `Exception`** — the single most
important detail in this file. Every layer between the step loop and `RunManager`'s worker
(`ReplayEngine.run()`, `DiscoveryAgent._escalate()`, the CLI's `finish_run` wrappers)
catches broad `except Exception` and converts what it caught into a *reported failure*. An
operator's stop is not a failure of the capability and must not be recorded as one, so it
has to pass through those handlers untouched. Subclassing `BaseException` makes that true
by construction rather than by remembering to re-raise it in a dozen places
(`tests/test_transport.py` asserts exactly this). The cost is that `cli.py` must carry an
explicit `except RunInterrupted` to call `pg_store.finish_run(status="interrupted")` before
re-raising — without it the run's Postgres row would stay `running` forever, the precise
bug those wrappers exist to prevent.

Nothing tracks Docker handles to stop the container: the exception unwinds through
`cli._browser_session`, a context manager whose `finally` already calls `sandbox.stop()`.

**`LocalSharedBrowserTransport`** — the CLI's real, working (not mocked) transport:
- `notify(request)` — prints the escalation reason/step and instructs the operator to "take
  over the browser window, then type 'resume' here." Because Playwright is launched
  **headed** (when `COMP_USE_HEADLESS` isn't set) and the browser process is the same one the
  automation was driving, this genuinely satisfies "let the human operate the same live
  session... not a fresh one" (§3.6) — the mock/bare operator surface *is* the actual browser
  window, no separate co-browsing infrastructure needed.
- `wait_for_resume()` — blocks on `input()` until the operator types `resume`; raises a clear
  `RuntimeError` (not a hang or a stack trace) if stdin isn't interactive, explaining exactly
  how to avoid triggering escalation in a non-interactive context. Also collects an optional
  one-line note describing what the human did, which flows into the `escalation_human_action`
  evidence event.

**`QueueTransport`** — the transport used by the FastAPI capability server
(`server/run_manager.py`), where there's no interactive terminal:
- `__init__(on_notify)` — takes a callback invoked on `notify()` (used by `RunManager` to flip
  the run's HTTP-visible status to `"escalated"` and store the `InterventionRequest`, so `GET
  /runs/{run_id}` can report it to a caller).
- `notify(request)` — just calls the callback.
- `wait_for_resume()` — blocks on a thread-safe `queue.Queue`, unblocked by a separate thread
  calling `.resume(note)` — which is exactly what `POST /runs/{run_id}/resume` does, from the
  HTTP request-handling thread, while the run's own worker thread stays parked in
  `wait_for_resume()`.

  Two hardening fixes here (`ENHANCEMENTS.md` §14, `IMPACTS.md` §5.15): `wait_for_resume()` now
  drains any stale leftover queue item before blocking — a duplicate `resume()` call (e.g. a
  double-clicked button) could otherwise leave an item that silently satisfies a *later*
  escalation's wait with no human ever looking at it. And it now polls in bounded 1-second
  slices against a `_cancel_event` instead of blocking forever, raising `EscalationAbandoned`
  when `cancel()` is called — the force-unwind path for a run stuck on an escalation nobody is
  ever going to resume (`RunManager.cancel()` / `DELETE /runs/{run_id}?force=true`).

- `interrupt()` sets `_interrupt_event` **and** `_cancel_event`. The second is not
  redundant: an escalated run is parked in `wait_for_resume()`, not at a step boundary, so
  its loop would never see the interrupt poll — setting the cancel event wakes it within one
  1-second slice. `wait_for_resume()` checks the interrupt event *first* precisely so that
  wake-up raises `RunInterrupted` (an operator ending the run) rather than
  `EscalationAbandoned` (an abandoned escalation), which are different intents and land in
  different terminal statuses.

---

## `server/app.py` — Agent-facing capability interface (§8 stretch goal)

**What it is.** A FastAPI app exposing saved artifacts as a callable HTTP API — the §8 stretch
goal explicitly named "Agent-facing capability interface: expose saved artifacts as a catalog
of callable capabilities... that an AI agent could discover and invoke by name with typed
args." Built as a genuinely separate entrypoint (`comp-use serve`) that reuses
`run_discover`/`run_replay`/`approve_artifact`/etc. from `cli.py` unchanged — `ReplayEngine`,
`DiscoveryAgent`, `Guardrail`, and `EscalationController` are untouched by this file.

**`_validate_capability_name(name)`** — enforces `^[a-z0-9_]+$` on any capability name coming
from a URL path parameter before it's ever used to build a filesystem path
(`artifacts_dir / name`). This exists specifically to prevent path traversal (a name like
`".."`) from resolving outside `artifacts_dir` — called out in the code comment as "a live
vulnerability, not just a missing feature" given this stores regulated financial artifacts.
A sibling `_validate_run_id(run_id)` (same regex) now guards every `/runs/{run_id}*` route the
same way — until `ENHANCEMENTS.md` §14, `DELETE /runs/{run_id}` called `shutil.rmtree()` on an
unvalidated caller-supplied path. `_mint_run_id(kind)` mints ids with a uuid suffix (a bare
millisecond timestamp could collide under real concurrency — see §14/`IMPACTS.md` §5.14).

**Request models:** `InvokeRequest` (`params: dict`, `version: int | None = None`),
`DiscoverRequest` (`goal`, `start_url`), `ResumeRequest` (`note`).

**`create_app(settings=None)`** builds the app and registers:
- `GET /capabilities` — lists every capability directory under `artifacts_dir`, reporting the
  latest **approved** version's description/input/output schema (exactly the "typed args" an
  agent needs to discover a capability), plus `has_pending_draft` if a newer, unapproved
  version exists.
- `GET /capabilities/{name}` — the full latest-approved `Artifact`, JSON-serialized.
- `GET /capabilities/{name}/versions` — every version's number/status/`created_from_run_id`
  (this is how a caller discovers which specific version numbers exist and are approved, before
  choosing to pin one via `invoke`'s `version` field below).
- `POST /capabilities/{name}/invoke` (202) — loads the artifact via `load_artifact(name,
  artifacts_dir, version=body.version)`. `body.version` defaults to `None` (unchanged
  behavior: latest approved). If the caller pins a `version` and that version's `status` isn't
  `"approved"`, the handler returns **`409 Conflict`** immediately — before
  `validate_required_params` and before `RunManager.start()` is ever called, so a request for
  an unapproved pinned version never starts a background run (mirrors the `409`s already
  returned by the `approve`/`reject`/`retire` endpoints for a bad state transition). Past that
  gate: validates params against the artifact's `input_schema` synchronously — `validate_
  required_params` now checks each supplied value's *type* too, not just presence
  (`ENHANCEMENTS.md` §14): never trust an LLM tool call's (the chat agent's `propose_invoke`)
  claimed types blindly — then starts a background run via `RunManager.start("invoke",
  name, target)` — where `target` closes over `body.version` and passes it straight through to
  `run_replay(..., version=body.version)`, so the pin is honored all the way to the actual
  replay, not just at the pre-check — and returns immediately with a `run_id`. This is the
  "invoke by name with typed args" half of the §8 stretch goal, made asynchronous because a real
  Playwright run can take real wall-clock time, now extended to let a caller target a specific,
  already-reviewed version instead of only ever "whatever is currently latest."
- `POST /capabilities/{name}/discover` (202) — same async-run pattern, wrapping
  `run_discover()` in non-interactive mode (`interactive=False`, so no `input()` approval
  prompt blocks a server thread) and reporting whether it succeeded and at what version.
- `GET /runs/{run_id}` — polls a run's status (`running`/`escalated`/`done`/`error`/
  `interrupted`), including the escalation's reason/step/screenshot URL (served via the
  mounted `/evidence` static directory) if one occurred, and the final
  `ReplayResult`/discover outcome once done.
- `POST /runs/{run_id}/takeover` (202) — a *voluntary* pause: the run isn't stuck or risky,
  an operator just wants the live browser for a moment. Sets a flag the step loop polls, so
  the status flips to `escalated` only once the loop reaches its next boundary, and the same
  `.../resume` hands control back. 409s on a run that is already `escalated` (it has stopped
  for a human already) or finished.
- `POST /runs/{run_id}/interrupt` (202) — ends the run for good, and with it the sandbox
  container. Valid from `running` **or** `escalated`, unlike takeover: an escalated run
  never reaches a step boundary, so `QueueTransport.interrupt()` unblocks its
  `wait_for_resume()` too. Returns 202 rather than 200 for the same reason takeover does —
  the run stops at its next boundary, which a slow in-flight action can delay by seconds.
- `POST /runs/{run_id}/resume` (202) — the HTTP half of the escalation handoff: unblocks the
  run's `QueueTransport.wait_for_resume()` from a different thread. Returns 409 if the run
  isn't currently `escalated`. Now also clears `RunRecord.escalation` (see `RunManager.resume()`
  below) — previously the stale escalation stayed visible via `GET /runs/{run_id}` forever,
  even after the run went on to finish cleanly.
- `DELETE /runs/{run_id}?force=true` — refuses to delete a `"running"`/`"escalated"` run by
  default (its transport is still live), but `force=true` calls `RunManager.cancel()` instead of
  deleting immediately, force-unwinding an abandoned escalation (`ENHANCEMENTS.md` §14) rather
  than leaking its thread/browser/container forever — the run still needs a follow-up plain
  `DELETE` once it's actually finished unwinding.
- `POST /capabilities/{name}/versions/{version}/approve` / `.../reject` / `.../retire` — HTTP
  wrappers around the same `cli.py` functions the CLI's `approve` subcommand uses, giving the
  §8 approval workflow (draft → approved) an API surface too, not just a terminal command.

---

## `server/run_manager.py` — Background-thread run tracking for the HTTP server

**What it is.** `RunRecord` (dataclass) + `RunManager` — lets an HTTP request return
immediately (`202 Accepted`) while a real Playwright browser session keeps running on a
background thread, and lets later requests (`GET /runs/{id}`, `POST /runs/{id}/resume`) observe
and interact with that same in-flight run.

**Why it's needed.** Discovery and replay are slow, blocking, browser-driving operations — an
HTTP request handler can't hold a connection open for the duration of a whole discovery run
(which may itself pause indefinitely on a human escalation). `RunManager` is the concurrency
primitive that makes "expose this as an API" actually workable.

**`RunRecord`** — `run_id`, `kind` (`"discover"` or `"invoke"`), `capability_name`, `status`
(`running`/`escalated`/`done`/`error`), the current `escalation` (an `InterventionRequest`, if
any), the `QueueTransport` instance backing this run, the final `result`/`discover_result`, and
any `error` string.

**`RunManager`:**
- `start(kind, capability_name, target_fn, run_id=None)` — generates a unique `run_id` (or
  validates a caller-supplied one isn't already in use — a colliding id now raises rather than
  silently overwriting the earlier run's record, `ENHANCEMENTS.md` §14), creates a `RunRecord`,
  builds a `QueueTransport` whose `on_notify` callback flips the record's status to
  `"escalated"` and stores the `InterventionRequest` (this is how `GET /runs/{id}` learns about
  an escalation without polling the worker thread directly), then spawns a daemon
  `threading.Thread` running `worker()`. The transport is now constructed and assigned to the
  record *inside* the same locked block as the collision check, before the record is published
  into `self._runs` — a concurrent `request_takeover()`/`resume()` could previously observe the
  record with `transport` still `None` and crash. `worker()` calls `target_fn(transport)`
  (which is `run_discover`/`run_replay` wrapped in a small closure in `server/app.py`), catches
  any exception into `record.error`/`status="error"`, and otherwise stores the result and sets
  `status="done"`. It catches `RunInterrupted` **first and separately**, setting
  `status="interrupted"` with `error="stopped by an operator"` and no result — an operator
  stopping a run is not a failure of the capability, and recording it as `error` would put a
  red mark against a capability that never misbehaved. By the time that handler runs the
  sandbox container is already gone: the exception unwound through `cli._browser_session`,
  whose `finally` calls `sandbox.stop()`.
- `get(run_id)` — thread-safe lookup.
- `resume(run_id, note)` — only succeeds if the record is currently `"escalated"`; flips status
  back to `"running"`, clears `record.escalation = None` (added in `ENHANCEMENTS.md` §16 — without
  it, `GET /runs/{run_id}` kept reporting the old escalation forever, even after the run finished
  cleanly), and calls `record.transport.resume(note)`, which unblocks the worker thread's
  `wait_for_resume()` call inside `EscalationController.escalate()`.
- `interrupt(run_id)` — ends a `"running"`/`"escalated"` run outright via
  `record.transport.interrupt()`. Distinct from both neighbours: `request_takeover()` pauses
  a run so a human can hand it back, `cancel()` unblocks an abandoned escalation so the run
  can finish *reporting a failure*, and this one stops it for good. Returns `False` for an
  unknown or already-finished run, which `server/app.py` maps to 404/409. Because
  `interrupted` is terminal, `delete()` — which refuses only `running`/`escalated` — will
  then remove the record, so a stopped run can never become a leak.
- `cancel(run_id)` — the force-unwind path for a `"running"`/`"escalated"` run stuck on an
  escalation nobody is ever going to resume (`ENHANCEMENTS.md` §14): calls
  `record.transport.cancel()`, which unblocks `wait_for_resume()` with `EscalationAbandoned`
  instead of a real resume note. `ReplayEngine`/`DiscoveryAgent` already catch any exception out
  of `escalate()` and turn it into a clean, reported failure, so this needed no new handling
  there — only a way to actually fire it.

All mutation of shared `RunRecord` state goes through `self._lock` (a `threading.Lock`),
since the worker thread and the HTTP request thread both touch the same record concurrently.

---

## `pg/store.py` — Postgres persistence (primary store once `COMP_USE_DB_URL` is set)

**What it is.** A flat module of free functions (no class) wrapping `psycopg2` — sync,
not async, because every caller (`DiscoveryAgent`/`ReplayEngine`/`EvidenceLogger`) is
already fully synchronous (Playwright's sync API throughout); an async driver would mean
wrapping every call site in `asyncio.run()`, strictly worse. `comp_use/pg/__init__.py` is
an empty package marker — all the logic lives in this one file.

**`db_enabled()`** — `bool(os.environ.get("COMP_USE_DB_URL"))`. Called before every read/
write throughout the codebase (`cli.py`'s `save_artifact`/`load_artifact`, `server/app.py`'s
every route) — this one function is the fork point between "Postgres primary" and
"on-disk `artifacts/`/`evidence/` fallback."

**`_get_pool()`** — lazily creates a module-global `psycopg2.pool.ThreadedConnectionPool(1,
10, dsn)` on first use; every later call reuses it. **`_execute(query, params)`** and
**`_query(query, params)`** are the two low-level primitives everything else is built on:
both check `db_enabled()` first (no-op / empty-list if not), get a connection from the
pool, run the query, and — critically — **catch and log any exception rather than
raising**. This is the module's central design decision, stated in its own docstring:
*"a Postgres outage must never crash a discovery/replay run."* `_execute` additionally
rolls back on failure before returning the connection to the pool. A few functions
(`delete_run`, `delete_capability`, `set_artifact_status`, `get_screenshot`) inline this
same try/except/rollback/finally shape directly rather than going through `_execute`/
`_query`, because they need a return value more specific than "rows as dicts" (a row
count, raw `bytes`, a boolean) — same non-fatal contract, duplicated rather than
abstracted further.

**Runs table** (`runs`, `run_events`, `run_screenshots` — schema in `pg/schema.sql`):
- `start_run(run_id, kind, capability_name, goal, novnc_url=None, container_name=None)` —
  `INSERT ... ON CONFLICT (id) DO NOTHING` (idempotent — a retried call can't clobber an
  already-started run's row).
- `set_novnc_url(run_id, novnc_url, container_name)` — called once, right after a sandbox
  container's ports are confirmed reachable (see `cli.py`'s `_browser_session`).
- `finish_run(run_id, status, result)` — the terminal write every `run_discover`/
  `run_replay` call now makes exactly once, including on the exception path
  (`ENHANCEMENTS.md` §14) — `status` here is the `OutcomeType` string value or `"error"`/
  `"failed"`, a different vocabulary from `RunManager.RunRecord.status`'s
  `running`/`escalated`/`done`/`error` (the two are read by different callers: this one by
  `GET /runs/{id}`'s Postgres-fallback branch for a run from a previous server lifetime,
  the other by the same route's primary, in-memory-`RunManager` branch for a live run).
- `abandon_orphaned_runs()` — marks every still-open run (`running`/`escalated`) as
  `interrupted`. Called once from `create_app()` at startup and **safe only there**: a run's
  terminal status is written by its own worker thread's `finally`, so a process that is killed
  or crashes leaves rows claiming to be live forever — observed as five phantom runs whose
  browsers had not existed for hours, blocking their capabilities and clearable only by hand.
  A freshly started process owns no threads from a previous one, so anything still open at
  startup is by definition orphaned; there is no race to lose.
- `delete_run(run_id)` — deletes the `runs` row; `run_events`/`run_screenshots` cascade via
  their own `FK run_id -> runs.id ON DELETE CASCADE`, so one `DELETE` clears a run's full
  history in one statement.
- `insert_event(run_id, event_type, data)` / `list_events(run_id)` — the Postgres-backed
  half of `EvidenceLogger.log_event()`; `list_events` backs `GET /runs/{id}/events`.
- `list_runs(limit=50)` / `get_run(run_id)` — back `GET /runs` and `GET /runs/{id}`'s
  Postgres-fallback branch respectively.
- `save_screenshot(run_id, label, png_bytes)` / `get_screenshot(run_id, label)` — the
  Postgres-backed half of `EvidenceLogger.save_screenshot()`; screenshots are stored as
  raw `bytea` (`memoryview(png_bytes)` on write), never redacted (a documented gap — see
  `ENHANCEMENTS.md` §17 item 4's neighboring note and `ADAPTATION_WRITEUP.md` §4).
  `get_screenshot` backs `GET /runs/{id}/screenshots/{label}`.

**Artifacts table** (`artifacts`) — primary store for `Artifact` JSON when `db_enabled()`;
`cli.py`'s `save_artifact`/`load_artifact` fall back to `artifacts/<name>/vN.json` on disk
only when it isn't:
- `save_artifact(capability_name, version, status, data, created_from_run_id)` —
  `INSERT ... ON CONFLICT (capability_name, version) DO UPDATE SET status=..., data=...`
  — the single upsert every status transition (`approve`/`reject`/`retire`/`set-default`/
  `clear-default`) and every direct data patch this session made (parameterizing
  hardcoded credentials, attaching the outcome-pattern library retroactively — see
  `ENHANCEMENTS.md` §17) goes through.
- `load_artifact(capability_name, version)` — exact version, no status filter (the status
  gate for a pinned version lives in the caller — `cli.load_artifact`/`run_replay`).
- `latest_approved_artifact(capability_name)` — `WHERE status='approved' ORDER BY
  (data->>'is_default')::boolean DESC NULLS LAST, version DESC LIMIT 1`: an explicit
  `is_default` wins over "highest version number," mirroring `cli.load_artifact
  (version=None)`'s file-mode resolution exactly. `NULLS LAST` matters for a row written
  before `is_default` existed at all (no key in the JSON at all → SQL `NULL`, not `false`)
  — it must rank behind an explicit `true`/`false`, never accidentally outrank a
  deliberately-chosen default.
- `list_artifact_versions(capability_name)` / `list_capability_names()` — back
  `GET /capabilities/{name}/versions` and the capability catalog listing.
- `delete_capability(capability_name)` — deletes every version; run history is untouched
  (no FK from `runs` to `artifacts` — they key on `run_id`, never `capability_name`+
  `version`), so a deleted capability's past invocations stay visible in run history.
- `set_artifact_status(capability_name, version, status)` — **dead code**, verified by
  grep: no call site anywhere in `comp_use/` or `tests/`. Every real status change goes
  through `save_artifact`'s full-row upsert instead. Flagged, not removed, in the backend
  review (finding #37) — left as-is since removing it wasn't in the fixed batch.

---

## `sandbox.py` / `sandbox_image/launch_browser.py` — isolated, watchable browser per run

**What it is.** `sandbox.py` spawns one Docker container per discovery/replay run, each
running a headed Chromium a human can watch (and, unlike the read-only VNC this was
adapted from, actually **take control of** — see below) via noVNC, driven over CDP from
the host process exactly like a local Playwright browser would be. Adapted from an
internal `hawkeye_sandbox` module, trimmed to just "start a container, get a CDP URL and
a noVNC URL, stop it when done" — no multi-browser spawning, no MCP config generation, no
video recording.

**`SandboxConfig`** (frozen dataclass) — `url` (the page to open on start), `width`/
`height` (1366×768 default), `image` (`comp-use-sandbox:latest`), the two
container-internal ports fixed by the image itself (`novnc_port=6080`,
`cdp_proxy_port=9223` — see `sandbox_image/supervisord.conf`), and `host`/`scheme` for
where those get published on the docker host.

**`SandboxHandle`** (frozen dataclass) — `container_id`, `container_name`, `novnc_url`,
`cdp_url`: everything a caller needs to drive the browser (`cdp_url`, via Playwright's
`connect_over_cdp()`) and hand a human a link to watch/control it (`novnc_url`, surfaced
through `RunManager`'s `on_novnc_url` callback all the way to `GET /runs/{id}`).

**`spawn(cfg, timeout_s=45)`** — the whole container lifecycle in one function:
1. `docker run -d --rm --name <prefix>-<uuid> --shm-size 1g` with the target URL, screen
   dimensions, and CDP port as env vars, publishing both the noVNC and CDP-proxy ports to
   an OS-assigned host port (`-p 127.0.0.1::6080`, no fixed host port — many sandboxes can
   run concurrently without colliding).
2. Polls `docker port <name>` in a loop until both published ports show up in its output,
   parsing the assigned host ports out of the text.
3. **A second, independent poll** against `{cdp_url}/json/version` — a published Docker
   port only means Docker itself is forwarding traffic, not that Chromium (started later
   in the container's own boot sequence, after Xvfb/XFCE via `supervisord`) is actually
   listening yet. Connecting via Playwright too early was observed live to fail with a
   bare "socket hang up" rather than any clear "not ready" signal — this poll turns that
   into a clean wait instead.
4. On either poll timing out, `docker rm -f`s the container before raising `SandboxError`
   — never leaves an orphaned container behind on its own failure path (see
   `ENHANCEMENTS.md` §14/`IMPACTS.md` §5.15's neighboring note on the *other* half of
   this — a container that survives spawn but is never explicitly stopped).
- **`stop(handle)`** — `docker rm -f` on the container id. No confirmation, no graceful
  shutdown request — `--rm` already means the container removes itself on a normal exit,
  this just forces it if it's still running.

**`sandbox_image/launch_browser.py`** — the process `supervisord` runs *inside* the
container to actually launch the browser (everything above only manages the container
from outside):
- `_disable_password_manager_prefs()` — seeds Chromium's profile `Preferences` file
  (`credentials_enable_service: false`, `profile.password_manager_leak_detection: false`)
  *before* first launch, since Chromium merges this file with its own defaults rather than
  overwriting it. Exists because MERIDIAN CORE's real-looking username/password pair,
  typed repeatedly across runs, was observed live triggering Chrome's built-in "this
  password was found in a data breach" dialog — which sits on top of the page and blocks
  the noVNC viewer until a human clicks it away, defeating the entire point of a
  human-watchable/controllable session.
- `main()` — reads `CHROME_URL`/`BROWSER`/`SCREEN_WIDTH`/`SCREEN_HEIGHT`/
  `CHROME_CDP_PORT` from the environment (set by `sandbox.py`'s `docker run` command),
  launches a **persistent context** (not `browser.launch()` — a real user-data dir at
  `/home/pwuser/browser-profile`, so the leak-detection prefs above actually take effect)
  with `--disable-features=PasswordLeakDetection,AutofillServerCommunication,Translate,
  OptimizationHints` and several other unsolicited-popup-suppressing flags (notifications,
  save-password bubble), navigates to `CHROME_URL`, then sleeps forever — the container's
  only job past this point is to keep the browser alive for whatever drives it over CDP
  from the host. Falls back to bundled Chromium if a `chrome`/`msedge` channel binary is
  missing (observed on Linux ARM64).

---

## `chat/` — natural-language front door onto the capability API

**What it is.** A three-module package (`session.py`, `catalog.py`, `agent.py`;
`__init__.py` is an empty package marker) implementing the take-home's "a minimal
conversational front door... that turns a user request into the right capability
invocation(s)" requirement. **Deliberately a pure client of the existing HTTP-facing
capability surface** — `ChatAgent` never imports `DiscoveryAgent`/`ReplayEngine` directly,
and never starts a run itself. A confirmed action comes back as `ChatTurnResult.
to_execute` for `server/app.py`'s `send_chat_message` endpoint to run through the exact
same `RunManager.start()` path `invoke_capability`/`discover_capability` already use — so
whatever guardrails apply to a direct API call apply identically to a chat-triggered one,
because under the hood it's the same call.

### `chat/session.py` — in-memory session state

**Deliberately not persisted** to Postgres or disk (matches `RunManager`'s own
in-memory-only pattern) — a server restart drops every active chat, an accepted
trade-off for "single ephemeral session," not a bug.

- **`PendingAction`** (dataclass) — a proposed `invoke` or `discover` awaiting the user's
  confirm/cancel/amend: `kind`, `capability_name`, `params`, `goal`, `param_hints`.
- **`ChatSession`** (dataclass) — `session_id`, `messages` (the real, untokenized
  conversation), `target_site` (`None` until the user picks one — see `agent.py` below),
  `pending_action`, `last_run_id`, `lock` (a `threading.Lock`, `compare=False` since locks
  aren't picklable/comparable — guards this session's own mutable state across two
  overlapping requests for the *same* session; `ChatSessionManager`'s own lock only
  protects the `_sessions` dict, not a session's fields once retrieved), and `tokenizer`
  (a per-session `SensitiveValueTokenizer`, `compare=False` — see below).
- **`ChatSessionManager`** — `__init__(redaction_patterns=None)` stores the patterns (from
  `Settings.redaction_patterns`, passed in by `server/app.py`'s `create_app`);
  `create()` mints a `uuid4().hex` session id and a **fresh** `SensitiveValueTokenizer`
  instance per session (stable token↔value mapping across a whole conversation, matching
  how `DiscoveryAgent` scopes its own tokenizer to one run); `get(session_id)` is a
  thread-safe dict lookup.

### `chat/catalog.py` — one function: `build_chat_catalog(settings) -> list[dict]`

Builds the capability list `ChatAgent` reasons over — **distinct** from `server/app.py`'s
`list_capabilities()` (the `/capabilities` HTTP response shape): adds `target_base_url`
(which `ChatAgent` needs to scope search to `session.target_site`) and drops fields the
model has no use for (`output_schema`, `has_pending_draft`). Walks every capability's
versions newest-first, skipping any that fail to load (same "one bad artifact must never
take down the whole listing" reasoning as `list_capabilities`/`list_versions`), collects
`approved` candidates, and picks the `is_default` one or the highest version — the third
near-identical implementation of "which version does an unpinned caller get" in the
codebase (flagged, not deduplicated, in the backend review — finding #34).

### `chat/agent.py` — the turn-by-turn decision logic

**Tool schemas** (OpenAI-compatible function-calling format): `_PROPOSE_INVOKE_TOOL`
(`capability_name`, `params`), `_PROPOSE_DISCOVERY_TOOL` (`capability_name`, `goal`,
`param_hints`), `_RESOLVE_PENDING_TOOL` (`decision: confirm|cancel|amend`).

**Prompt builders:** `_system_prompt(session, site_catalog)` — states the target site and
the site-scoped catalog as JSON, instructs the model to call `propose_invoke`/
`propose_discovery` or just reply in text if it needs more information.
`_pending_system_note(pending)` — appended only when resolving a pending action, telling
the model exactly what it's interpreting the user's reply *as* (confirm/cancel/amend for
which specific pending action). `_build_messages(session, site_catalog, extra_system=None)`
— assembles the final message list sent to the LLM: `session.messages` holds the real
conversation for the app's own bookkeeping/display, but the copy actually sent to the
provider is tokenized first (`session.tokenizer.tokenize` on each message's `content`) —
the same redaction boundary `DiscoveryAgent` already enforces, extended to chat
(`ENHANCEMENTS.md` §17 item 4).

**Target-site handling** (deterministic, no LLM call — `session.target_site is None` is
checked *before* any model call in `turn()`):
- `_known_sites(catalog)` — every distinct `target_base_url` seen in the catalog, in
  first-seen order.
- `_target_site_prompt(sites)` — a numbered pick-list plus "a new URL - reply with it
  directly."
- `_match_site_choice(user_message, sites)` — a digit picks by list position; an
  `http(s)://`-prefixed reply is parsed and re-normalized to `scheme://netloc` (dropping
  any path); otherwise an exact string match against a known site; `None` if nothing
  matches (re-prompts).
- `_handle_target_site_selection(session, user_message, catalog)` — the handler `turn()`
  dispatches to while `target_site` is unset; sets it and replies `"Working against
  {chosen}. What would you like to do?"` on a match, or re-shows the prompt.

**Deterministic yes/no** (checked before ever asking the model, for the exact reason
stated in its own docstring — "the single most safety-critical gate in the chat surface"):
`_AFFIRMATIVE_REPLIES` / `_NEGATIVE_REPLIES` (exact-match sets — `"yes"`, `"y"`, `"confirm"`,
... / `"no"`, `"n"`, `"cancel"`, ...) and `_parse_explicit_decision(user_message)`, which
normalizes (strip, lowercase, drop trailing `.`/`!`) and checks set membership, returning
`None` for anything else (falls through to the LLM — see below). Added after a live
failure: a free-tier model (`minimax/minimax-m3:free`) not honoring the forced
`resolve_pending` tool call meant "yes" could loop forever under the pre-fix design
(`ENHANCEMENTS.md` §16 item 1).

**`_slugify(name)`** — lowercases, collapses anything non-alphanumeric to `_`, strips
leading/trailing `_`; `"capability"` if the result is empty. Used to derive a new
capability's recorded name from whatever the model proposed.

**`ChatAgent`** — `__init__(llm)`. `turn(session, user_message, catalog)` is the single
entry point (`server/app.py`'s `send_chat_message` calls this once per HTTP request,
inside `session.lock`): appends the user's message to `session.messages`, then dispatches
on session state — target-site selection first, then pending-action resolution, then a
normal turn — never more than one of the three per call.

- **`_handle_pending_resolution(session, user_message, catalog)`** — tries
  `_parse_explicit_decision` first; only on `None` does it build a *forced* single-function
  LLM call (`tool_choice` pinned to `resolve_pending`) with `_pending_system_note` as extra
  context, defaulting to `"amend"` if the model doesn't return a tool call at all (fails
  closed — never silently treats an unhonored forced call as `"confirm"`). `"confirm"`
  clears `pending_action` and returns it as `to_execute` (the caller starts the run).
  `"cancel"` clears it with no execution. `"amend"` clears it and falls through to
  `_handle_normal_turn` so the user can restate what they want.
- **`_handle_normal_turn(session, catalog)`** — one LLM call with `tool_choice="auto"`
  offering both propose tools, scoped to `site_catalog` (catalog filtered to
  `session.target_site`). Detokenizes anything the model echoes back (`result["text"]` for
  a plain reply, or `propose_invoke`'s `params` / `propose_discovery`'s `goal` for a tool
  call) before it's stored, shown, or acted on — mirrors `DiscoveryAgent`'s own
  detokenize-at-the-boundary discipline. Dispatches to `_propose_invoke`/
  `_propose_discovery`, or a generic "couldn't figure out how to help" reply if the model
  names neither tool.
- **`_propose_invoke(session, site_catalog, capability_name, params)`** — validates the
  named capability exists in the site-scoped catalog and every `required` param is
  present (**not** a full re-validation against `InputParam.type` — that check lives in
  `replay.engine.validate_required_params`, run again by `server/app.py` right before the
  confirmed run actually starts, since the catalog here only carries `name`/`type`/
  `required`, not enough for the full check). On success, composes the confirmation
  message **in Python, deterministically** — never trusting the LLM's own prose — masking
  any credential-shaped param value via `guardrail.is_sensitive_param_name` (`"••••••"`),
  and sets `session.pending_action`.
- **`_propose_discovery(session, capability_name, goal, param_hints)`** — slugifies the
  proposed name, states the exact goal it will hand to discovery (never asks first, since
  starting a *recording* is informational, not itself irreversible — only running it needs
  confirmation, which this same message asks for), and sets `session.pending_action`.

---

## How the pieces satisfy §3 end to end

| §3 requirement | Where it's implemented |
|---|---|
| 3.1 Goal-driven agent loop | `discovery/agent.py: DiscoveryAgent.run()`, using `surface.py: PlaywrightSurface` |
| 3.2 Structured artifact | `schemas.py: Artifact` + friends, produced by `discovery/compiler.py: compile_artifact()` |
| 3.3 Deterministic replay | `replay/engine.py: ReplayEngine.run()` — no LLM import anywhere in the file |
| 3.4 Safety & guardrails | `guardrail.py: Guardrail`, `tokenizer.py: SensitiveValueTokenizer`, `config.py: Settings` |
| 3.5 Evidence/observability | `evidence.py: EvidenceLogger`, screenshots via `surface.py: safe_screenshot()` |
| 3.6 Escalation & handoff | `escalation/controller.py: EscalationController`, `escalation/transport.py` |
| 3.7 Heterogeneity/multi-tenant (design only) | `surface.py`'s `Surface` interface is the extension seam; see `/REPORT.md` §"Heterogeneity & multi-tenant" for the design story (no extra code required here) |
| §8 stretch goals taken | `server/app.py` + `server/run_manager.py` (agent-facing capability interface + approval gating), `drift.py` + `cli.py`'s `--diagnose-drift-on-failure` (assisted fallback) |

For the target application these files drive (`mock_app/`, a deliberately hostile Flask bank
UI), the CLI usage, and the full evidence trail, see `/README.md` and `/REPORT.md`.

---

## Current verified state (MERIDIAN CORE)

Eight capabilities recorded and approved, covering every function in §2.1 of the Adaptation
brief. Last verified end to end against the live target:

| | Result |
|---|---|
| Base-case replays | **8/8 success** — escalation on exactly the three irreversible actions (transfer, open share, place hold) and nowhere else |
| Edge-case replays (§2.2) | **12/12 classified correctly** — six injected faults, six natural errors |
| Discovery-phase faults | **5/5 classified**, and no failing run produced an artifact |
| Test suite | 348 passing |

Side effects were checked on the target rather than taken on trust: the transfer moved exactly
$1.00 between the two named shares, the new share exists at its stated deposit, the held share
reads `HOLD`, and the member's contact details match what was passed.

### Where the remaining risk is

- **The error vocabulary is hand-authored per host.** ~10 page signatures per target, each
  requiring someone to have observed that state first. `IMPACTS.md` §5.19 records the decision
  to keep this for now and the two changes that would replace it (HTTP status codes as a
  zero-config baseline; discovery authoring patterns into the artifact).
- **Discovery quality is model-dependent**; replay is not. Replay runs no model at all, so a
  correctly recorded capability stays reliable regardless of provider. Every gate in
  "Recording-quality gates" above exists because a specific capability shipped broken.
- **`OutcomeType` has no sub-category.** A permission denial and a field-validation rejection
  are both `business_outcome` with the distinction only in prose `detail`, so a calling agent
  must string-match English to tell "retry with a different operator" from "retry with
  corrected input". A `reason_code` on `OutcomePattern` is the intended fix — deferred, not
  rejected. Note also that `OutcomeType.VALIDATION_ERROR` already exists and means something
  entirely different: *our* parameter checks ("missing required param"), never the host's.
- **`DELETE /capabilities/{name}` is a hard delete** — no soft-delete, no undo, no
  confirmation, and it removes every version at once. It cost three accidental capability
  losses in one session, each recoverable only by a full re-discovery. `retire` sits beside it
  and *is* reversible; that asymmetry is the defect.
- **`escalation_transport_failed` fires once per discovery run** and has not been
  investigated — it may be masking something.
