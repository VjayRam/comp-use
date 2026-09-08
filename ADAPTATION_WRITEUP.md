# MERIDIAN CORE Adaptation — Write-up

Full engineering detail lives in `CODEMAP.md` (per-file walkthrough) and
`ENHANCEMENTS.md` (every fix and why). This is the short version.

## 1. What adapting took, and what actually had to change

The core loop — discover → record a typed capability artifact → replay
deterministically, with guardrails, evidence, and escalation — pointed at
MERIDIAN CORE (`web-sample.interface-hiring.com`) with **zero changes to
`DiscoveryAgent`'s or `ReplayEngine`'s control flow**. Every one of the 7
required functions (plus sign-on, plus a last-name-search variant of member
inquiry) is a `target`/allowlist configuration change and a new discovery
run, not new code.

What genuinely did need code changes — all in the perception/classification
layer, not the loop itself:

- **Risk classification was keyword-blind to this target.** The take-home's
  heuristic only matched `"confirm"`/`"delete"`; MERIDIAN's own commit buttons
  ("Post Transfer," "Open Share," "Apply Hold") matched none of them.
  Broadened with a second, structural signal — any action taken on a
  `.../review`-style URL is risky regardless of the button's exact label —
  deliberately over-conservative rather than risk missing a real commit.
- **A second perception channel for unlabeled legacy inputs.** MERIDIAN's
  sign-on and update-info forms have no `<label>` association at all; the
  accessibility tree alone left the model only a field's *current value* as a
  handle — backwards for a field the capability exists to change. Added a
  `name`/`id`/`type` hint block to `PlaywrightSurface.observe()`, deliberately
  never the value itself, so it stays inside the same tokenization boundary.
- **The outcome-pattern library** (`discovery/outcome_library.py`) — the
  mechanism for classifying business outcomes vs. recoverable conditions vs.
  hard failures already existed and worked; nothing populated it. This is the
  one piece that's genuinely target-specific content, not a generic engine
  fix — see §3.
- **A per-run allowlist union for discovery's own `start_url`**, so pointing
  discovery at a second site doesn't require restarting the server with
  different global config.

New layer, not "the core": a capability API (FastAPI), a chat agent, a
dashboard, a Postgres store, and a Docker-sandboxed browser for live
streaming — all built as callers *of* the existing engine, never inside it.

## 2. Capabilities as an API — the contract

`GET /capabilities` returns a catalog: name, description, typed
`input_schema`/`output_schema`, and whether a newer draft is pending.
`POST /capabilities/{name}/invoke` takes `{"params": {...typed args...}}`,
starts a background replay, and returns immediately with a `run_id`;
`GET /runs/{run_id}` polls status (`running` → `escalated` → `done`/`error`)
and the final structured result. `POST /capabilities/{name}/discover` is the
same async shape for recording a new one. Version `approve`/`reject`/`retire`
gate what an unpinned `invoke` picks up — a fresh discovery is a draft,
never live until reviewed.

The chat agent is a pure client of this exact surface — it never imports
`DiscoveryAgent`/`ReplayEngine` directly. It maps a request to an existing
capability (confirm → invoke) or proposes recording a new one (confirm →
discover), always asking which site to target first and scoping capability
search to it. This matters for §5: whatever guardrails apply to a direct API
call apply identically to a chat-triggered one, because it's the same call.

## 3. Driving this legacy UI reliably; exceptional-state handling

The per-transaction hidden token, the review→post confirmation flow, and the
supervisor-gated action all fell out of the existing schema/replay engine
with no special-casing — a hidden token is just another field read off the
form snapshot; supervisor gating is just a business-outcome page like any
other. The one place that needed real content, not code, was the outcome
taxonomy: `outcome_library.py` ships a host-keyed set of `OutcomePattern`s
for the four exceptional states confirmed live against MERIDIAN CORE (natural
and via `?inject=`) — member-not-found, permission-denied, a maintenance
interstitial (with a real `recovery_action` that dismisses it and retries),
and session-timeout (reported as `RECOVERABLE` with no auto-retry, since real
recovery needs a multi-step re-authenticate-and-resume flow a single
`recovery_action` step can't express — see §5's cuts). `compile_artifact()`
attaches the whole library to every capability recorded against this host
automatically, rather than requiring each one hand-fixed after separately
observing that state. `validation` (400) and `server` (500) are the two
remaining injected kinds: `server` needs no pattern at all — the fail-closed
default (`HARD_FAILURE`) is already the correct answer for it — and
`validation` is the one state not yet reachable live in testing (MERIDIAN's
own `timeout` fault-injection mode is a one-way trap with no reset short of a
redeploy, and it was hit before `validation` could be tried).

Demoed live, both directions: a teller (`teller1`) attempting a
supervisor-only Place Account Hold cleanly reports `business_outcome` /
"supervisor override required" instead of timing out; a supervisor (`super1`)
completing the same flow escalates at the risky commit step, and — after a
human confirms via the dashboard's noVNC feed — completes with a real
confirmation number.

## 4. How safety, evidence, and escalation survive the new surface

- **Guardrail.** The allowlist and risk-tier confirmation gate live inside
  `ReplayEngine`/`DiscoveryAgent`, called identically whether the caller is
  the CLI, the HTTP API, or the chat agent. Chat's own "should I run this?"
  confirmation is a *separate* layer — confirming in chat never sets
  `confirm_risky=True`, so a risky step inside an already-confirmed run still
  escalates through the same `EscalationController` exactly as before. The
  chat confirmation gate itself is now deterministic (an exact "yes"/"no" is
  parsed in code before ever asking the model), so a flaky free-tier LLM
  can't turn an ambiguous reply into an executed irreversible action.
- **Evidence.** The same `EvidenceLogger` writes for a chat-triggered run as
  a CLI-triggered one — one `run_id`, minted once, threaded through every
  layer, so the dashboard's events panel and a run's evidence directory are
  never out of sync with what the frontend is polling.
- **Escalation.** The exact same handoff (`EscalationController.escalate()`
  → `wait_for_resume()` → resume with a note) now surfaces through a shared
  `RunPanel` component used by both the dashboard and the chat's embedded
  run view — same live noVNC feed, same screenshot, same resume box. A
  separate *voluntary* "take control" affordance (an operator just wants to
  drive the browser, nothing's stuck) reuses this identical machinery rather
  than inventing a second path.
- **Known, documented gaps, not hidden ones:** chat messages are now
  tokenized before reaching the LLM provider (dollar amounts / structured
  financial identifiers — the same shapes `DiscoveryAgent` already redacts),
  but screenshots are still persisted unredacted and served from an
  unauthenticated `/evidence` mount — real exposure surfaces for a
  production system, acceptable for a demo target with no real PII, called
  out explicitly rather than left implicit.

## 5. Known trade-offs: cost, latency, reliability

Six decisions where the design deliberately bought one property with another, and
where the cost is real enough to state rather than gloss. Each is a live property
of the code today, not a hypothetical.

**1. Token cost grows with the square of the run's length.** Every discovery turn
resends the full history, so an *n*-step run costs O(n²) tokens rather than O(n).
Nothing caps it at the source; the only bound is `max_discovery_steps`. In practice
a median run is 13 turns and ~71k tokens, and the floor is high for a different
reason — the system prompt and tool schema together are ~3,390 tokens re-sent on
every single call, which is roughly 62% of a median run spent re-transmitting an
identical prefix. Prompt caching is the obvious fix and is not wired up.

**2. The default retry has no delay.** `OutcomePattern.retry_delay_seconds`
defaults to `0`, so a `RECOVERABLE` condition is re-checked immediately unless an
artifact author sets a delay explicitly. That helps against timing flakiness and
does nothing for the case the assignment's own "wait and retry" language describes
— a slow load or a transient outage that needs seconds, not microseconds.

**3. `RunManager`'s record dict never shrinks on its own.** A `RunRecord` stays in
memory until something explicitly deletes it, so the process grows with *cumulative*
request volume rather than with concurrent load. That is the more insidious of the
two memory-growth findings: concurrency is visible and self-limiting, while a slow
leak tied to lifetime traffic only shows up in a long-lived deployment.

**4. Schema defaults can retroactively change an approved artifact's behaviour.**
Artifacts are stored as data and re-validated on load, so a new field shipping with
a non-inert default applies to versions approved before that field existed. The
`draft`/`approved` gate therefore guarantees "a human reviewed this recording",
which is subtly weaker than "this exact behaviour was reviewed". The mitigation is
discipline rather than mechanism: new schema fields default to inert.

**5. `FallbackLLMClient` has no circuit breaker.** Every turn tries the primary
provider and fails over on any error — so a provider that is comprehensively down
is re-tried on every single call for the length of the run, each attempt paying its
full timeout. Reliability is bought with unconditional cost rather than adaptive
cost. The client-side rate limiter reduces how often this compounds into a 429, but
does not change the shape.

**6. Deterministic replay costs a hand-authored error vocabulary.** Keeping the LLM
out of the replay path is what makes outcome classification reproducible — the same
page always classifies the same way. The price is that every new target needs its
error states observed by a human and written into `outcome_library.py` before
replay can recognise them. Status-code classification and discovery-authored
patterns are the way out, and both are deliberately deferred until a second target
exists: with one target you cannot tell which parts of the vocabulary are genuinely
site-specific and which are general.

## 6. What's cut, and what's next

- **Session/timeout recovery.** `OutcomePattern.recovery_action` is one UI
  step; a real session-expiry recovery is re-authenticate-then-resume the
  original capability's remaining steps. This needs the session itself
  modeled as a typed, resumable artifact — real design work, not a quick
  patch, and the reason `timeout` is reported-but-not-retried today.
- **Auth.** No API key or user model on the capability server/dashboard/chat
  — anyone reachable can invoke, discover, or approve. Deliberate for a
  same-machine demo; the first thing a real deployment needs.
- **Screenshot redaction and an authenticated evidence route** — flagged in
  §4, not yet built.
- **A generic "operator" concept across the chat/dashboard UI.** Every
  capability takes `operator_id`/`password`/`branch` as explicit params
  today (fixed from an earlier state where 5 of 7 capabilities hardcoded
  them); a real product would want one login step per session instead of
  re-entering credentials per invocation — a session-scoped identity, not a
  per-call parameter, is the natural next step once the session-model work
  above exists.
