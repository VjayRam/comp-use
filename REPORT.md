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
`comp_use/cli.py`; settings in `comp_use/config.py`. **Caveat:** `check_checkpoint`'s
`TEXT_PRESENT` type checks `page.content()` — the raw server-rendered HTML source,
not rendered/visible text. That's correct against this mock app, since every
conditional message (`insufficient_funds`, `not_found`) is a Jinja `{% if %}` that
either renders or doesn't server-side, with no client-side JS involved. It would
silently produce false positives against a different app that hides matching text
via CSS/JS instead of omitting it from the response entirely — a real constraint on
how portable `TEXT_PRESENT` checkpoints are, worth stating rather than assuming away.

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
  `validate_required_params()` (`comp_use/replay/engine.py`) is the single source
  of truth for "does this `params` dict satisfy this artifact" — both
  `ReplayEngine.run()` and `cli.py`'s `_run_replay` call the same function rather
  than each checking `required`/`name in params` separately, so `cli.py`'s
  pre-browser check (needed to skip launching Chromium on `validation_error`) and
  the engine's own check can't drift apart. **Known gap:** `InputParam.type`
  (`Literal["string", "number", "boolean"]`) is declared but never checked — a
  `"number"` param passed as a non-numeric string is accepted at validation time
  and only fails later, if at all, when it gets typed into a form field as a
  string. Every param in this build is `type="string"` in practice, so this
  hasn't bitten anything yet; flagged here rather than silently left unstated.
- `output_schema` is derived the same way, from the other end: an `extract` step
  carries `extract_as`, and `compile_artifact()` collects those names into
  `output_schema`. `open_sub_account`/`transfer_funds` both end with an `extract`
  step reading the confirmation page (`confirmation_number` / `txn_id`); replay
  returns them in `ReplayResult.outputs`, not a hardcoded `{}`.
- Locators are role/text-first with an optional CSS `fallback`.
- Each `Step` has `risk_tier` (`safe` / `risky`) and an optional per-step
  `checkpoint`. The artifact also has a final `success_checkpoint`, derived by
  `comp_use/cli.py`'s `_derive_success_checkpoint` from the final page's own
  heading (`element_visible` on whatever `<h1>`-equivalent is present) — not
  from the literal final URL, which would only match a future replay that
  happens to reproduce the exact same per-run path segments (a member ID, a
  generated confirmation number). Re-running `discover` for an existing
  capability name bumps the artifact version (`next_artifact_version()`) rather
  than overwriting `v1.json` in place.

## Determinism & error handling

Replay (`comp_use/replay/engine.py`) never calls an LLM. Same artifact + same params
attempt the same actions in the same order. Outcomes (`OutcomeType` in
`comp_use/schemas.py`):

1. **`validation_error`** — required params missing/malformed; no browser interaction.
2. **`success`** — all steps ran; `success_checkpoint` held; any `extract` steps'
   values are returned in `ReplayResult.outputs`, keyed by `extract_as`.
3. **`business_outcome`** — a legitimate, expected non-success UI result (e.g.
   "insufficient funds" on the mock transfer form). An `Artifact` carries
   `outcome_patterns: list[OutcomePattern]` — each pairs a `Checkpoint` with the
   outcome it means. Whenever a per-step or the final `success_checkpoint` fails to
   match, `ReplayEngine._match_outcome_pattern` checks these *before* falling back to
   `hard_failure`; if one matches, its outcome/detail is returned instead. This is the
   mechanism that keeps "no such member"/"insufficient funds" from being reported as a
   crash — see the evidence walkthrough below for a real run that hits it.
   **Known limitation:** unlike `input_schema`/`output_schema` (both derived
   automatically from what the discovery loop actually did),
   `outcome_patterns` is never populated by `_run_discover` — the LLM has no
   way to *decide* "this page is a business outcome, not a hard failure" from
   inside the discovery loop as built, so every `outcome_patterns` entry in
   the committed artifacts (`no_such_member`, `insufficient_funds`) was
   hand-authored after discovery, not discovered automatically. Stated here
   rather than left implicit.
4. **`recoverable`** — same `outcome_patterns` mechanism, for a transient
   condition that isn't a permanent business state (unlike `business_outcome`)
   and isn't an automation bug (unlike `hard_failure`) — a dismiss-and-retry
   condition. `open_sub_account` and `transfer_funds` both declare one:
   double-submitting a review/confirm step (a stale token — the review page
   revisited, or Confirm clicked twice — a genuine double-click/back-button
   scenario) previously crashed the mock app with a raw `KeyError`/500;
   `mock_app/app.py` now renders `session_expired.html` ("This review session
   has expired or was already submitted. Please start again.") instead, and
   the artifacts' `outcome_patterns` classify that page as `recoverable`,
   detail `session_expired`. Verified live: drove a real transfer to
   confirmation, navigated back to the now-stale review URL, and confirmed
   both that the real page renders "Session Expired" and that
   `PlaywrightSurface.check_checkpoint()` against the real
   `transfer_funds` artifact's `recoverable` `OutcomePattern` matches it.
5. **`hard_failure`** — checkpoint miss, or an action that raised (missing element,
   timeout), with no matching outcome pattern; result includes `step_index`,
   `expected`/`detail`, `observed`.

Every outcome that actually reaches the browser also gets a richer signal than
the JSONL log alone, when it isn't `SUCCESS`: `comp_use/cli.py`'s `_run_replay`
and `_run_discover` call `evidence.save_screenshot(surface.screenshot(), "final")`
before closing the browser whenever the outcome isn't clean success, landing a
`final.png` next to `log.jsonl` in the run's evidence directory (per §3.5's "at
least one richer signal on failure"). `validation_error` is the one outcome this
can never apply to — `_run_replay`'s required-param check
(`validate_required_params`, see Artifact schema above) runs and returns *before*
`sync_playwright()` is ever entered, by design (see the Evidence walkthrough's
`validation_error` entry — "the CLI skips launching Chromium entirely on this
path"), so there is no `Surface` yet to screenshot. A successful run doesn't get
one either — the
signal is for debugging a failure, not documenting every run.

Outcome patterns are checked **before every step is attempted**, not only after a
checkpoint miss — the app can diverge onto a business-outcome page mid-sequence
(e.g. "insufficient funds" appears after step 7 of `transfer_funds`, but step 8 is
still "click Confirm Transfer," a control that page never renders). Blindly attempting
step 8 timed out with a raw Playwright exception the first time this was tested for
real; `ReplayEngine.run` now re-checks `outcome_patterns` at the top of every loop
iteration and also wraps `surface.act` in try/except, re-checking outcome patterns
before falling back to `hard_failure` with the exception text as `detail`. That
detail is `f"{type(exc).__name__}: {exc}"`, not a bare message — a broad
`except Exception` is necessary for crash-proofing, but it also means a genuine
code bug and an expected environmental failure (a Playwright `TimeoutError`)
would otherwise produce indistinguishable failure text; naming the exception
type is the mitigation for that trade-off. `DiscoveryAgent`'s equivalent
`action_failed` path does the same.

`DiscoveryAgent.run()`'s own `surface.act()` call is wrapped the same way — a
locator that passed the "is a locator present" check but doesn't actually resolve
at Playwright-action time (a real ARIA role that doesn't match anything, an
element that's disabled, ...) is caught, logged as a `skipped_decision` with
reason `action_failed`, and counted toward the dead-end/vision-fallback machinery,
instead of crashing the whole run. Found this wasn't the case, and fixed it, from
a real failure: a live discovery run had the model choose an invalid ARIA role
(`"text"`, not a real role) for an `extract` locator, which hung the process for
the full Playwright timeout with no recovery path before this fix. After the fix,
the identical scenario still takes the one timeout to fail (expected — a bad
locator genuinely takes time to time out) but is caught, logged, and
automatically retried with the vision fallback on the next call, which resolved
cleanly. `ReplayEngine.run()`'s own `Guardrail.check_allowlist()` call is inside
the same try/except as `surface.act()` too, for the same reason — an
`AllowlistViolation` (a step's target URL or action type falling outside the
configured allowlist) is now a `HARD_FAILURE` `ReplayResult`, not a raised
exception. With both engines' allowlist and action-execution calls inside their
own try/except, "no action in this system is ever allowed to crash the CLI with
a raw traceback" is true end to end, not just for the paths that happened to be
tested first.

**Two more crash paths found from a real user report, after a genuinely clean
`pytest` run gave false confidence.** Screenshot capture itself was never
protected anywhere — `DiscoveryAgent`'s vision-fallback call to
`self.surface.screenshot()` sat completely outside any try/except, and a real
`Page.screenshot()` timeout (independent of any locator/action problem) crashed
a live `discover` run. Auditing every `surface.screenshot()` call site found 8,
one of which (`comp_use/drift.py`'s own diagnosis capture) had been missed
entirely in the original audit that produced the fixes above. Added a single
shared `safe_screenshot(surface) -> bytes | None` (`comp_use/surface.py`) and
routed every call site through it — including `EscalationController`'s
before/after capture, arguably the single most safety-critical of all of them,
since a screenshot failure there must never be allowed to prevent the human
from being notified in the first place.

Separately, `DiscoveryAgent.run()`'s call to `llm_client.decide_next_action()`
was never wrapped at all, unlike every other external interaction in the loop.
A real model response triggered `json.loads()` to raise `"Extra data"` (valid
JSON with trailing commentary appended) and crashed the run. Fixed
`parse_decision()` to fall back to `json.JSONDecoder().raw_decode()`, but also
wrapped the call itself in try/except — defense in depth, not reliance on the
parser fix alone. Verified live, repeatedly: re-running the exact failing
command hit the vision-fallback `action_failed` path again (recovered cleanly)
and, separately, a *different* malformed-JSON shape the `raw_decode` fix didn't
even cover (`"Expecting ',' delimiter"`) — caught by the outer wrap regardless,
logged as `llm_call_failed`, and the run still finished. Both real runs are
committed (`evidence/discover_1787177243`, `evidence/discover_1787177306`).

## Drift-aware self-healing replay (`--diagnose-drift-on-failure`, optional)

Beyond what the assignment requires: an opt-in extension that turns a `hard_failure`
into a diagnostic opportunity instead of a dead end, built entirely from primitives
that already existed — the vision fallback, `EscalationController`, artifact
versioning, and the outcome taxonomy — rather than a new subsystem.

**The core invariant is preserved, not bent.** `comp_use/replay/engine.py`
still never imports `LLMClient` and still never calls one — `ReplayEngine.run()`
produces the exact same, fully deterministic `ReplayResult` it always did, and
that outcome (`HARD_FAILURE` here) is never altered by anything below. The
diagnosis is a separate, new module, `comp_use/drift.py`, invoked only by
`cli.py`'s `_run_replay` — the orchestration layer that already talks to an LLM
for `discover` — strictly *after* `engine.run()` has returned its final result.
It's a best-effort post-mortem on an already-failed run, not a decision the
replay itself makes.

**How it works.** On `HARD_FAILURE` where the failure was specifically the
recorded action's own locator not resolving (`comp_use/cli.py`'s
`_is_action_locator_failure` — deliberately *not* triggered for a checkpoint
mismatch, since patching a step's action locator wouldn't address the page
looking different after a successful action):
1. Take a screenshot of the real, current page state.
2. Ask a vision model (`OpenRouterClient.diagnose_drift`, a dedicated tool
   schema/prompt distinct from `decide_next_action`) whether a control serving
   the same purpose is still visible, just renamed/moved — or whether it's
   genuinely gone.
3. If found, build a **new**, unsaved `Artifact` with only that one step's
   locator patched (`comp_use/drift.py`'s `propose_drift_patch`) and save it as
   the next version via the same `next_artifact_version()` issue 20 built —
   never overwriting `v1`.
4. Escalate to a human via the same `EscalationController` replay already
   uses, naming the proposed version and file path. The patch is never applied
   to the run that just failed, and never becomes "the" artifact without a
   human explicitly reviewing it — `load_artifact()`'s existing
   highest-version-wins default means the patch only takes effect once a human
   has looked at it and left it as the latest version (or explicitly rejected
   it by not promoting it / deleting it).

**Live evidence — including the honest failure modes hit along the way, not
just the eventual success.** Verified against a deliberately broken artifact
(`transfer_funds_drift_live_check`, a real `transfer_funds`-shaped artifact
with one locator typo'd — `"Cofirm Transfer"` instead of the real page's
`"Confirm Transfer"`) and the real running mock app:

- `evidence/replay_1787173876/` — first real attempt: the model (a small
  free-tier vision model, `google/gemma-4-26b-a4b-it:free`) correctly
  recognized the typo semantically but replied with a malformed tool-call
  shape (`{"diagnose_drift": {...}}`, wrapped, using `"reason"` instead of the
  schema's `"reasoning"`, and no `locator` field at all) — logged honestly as
  `{"found": false, "reasoning": "model returned an invalid locator"}` rather
  than silently discarded or misreported as a clean "not found."
- Debugged directly (not guessed): a standalone call to
  `OpenRouterClient.diagnose_drift` reproduced the exact malformed response,
  confirming this was a real free-tier-model quirk, not a fabricated scenario.
  Fixed with `_normalize_drift_diagnosis()` (tolerates the wrapping-key and
  `reason`/`reasoning` divergence) plus a stricter worked-example prompt.
- `evidence/replay_1787174182/` — same broken artifact, after the fix:
  `{"found": true, "proposed_version": 2, "proposed_locator": {"role":
  "button", "name": "Confirm Transfer"}, "reasoning": "The expected control
  'Cofirm Transfer' has a typo in the expectation, but the 'Confirm Transfer'
  button is clearly visible on the page."}` — saved as `v2.json`,
  `v1.json` left untouched, and a real escalation raised
  (`evidence/replay_1787174182/drift_diagnosis.png` is a real screenshot of
  the actual page).
- Closing the loop: replaying the same capability again (no flags needed —
  `load_artifact()` picks up the new highest version automatically) —
  `evidence/replay_1787174265/` — `{"outcome": "success"}`. The proposed patch
  wasn't just plausible-looking; it actually works.

This is the concrete, tested slice of what the Heterogeneity section below
still describes as a design-only "Drift" bullet at the fleet/multi-tenant
scale — single-artifact, single-locator, human-gated, but genuinely built and
genuinely proven against a real model's real (and, honestly, initially
malformed) output, not just described.

## Heterogeneity & multi-tenant

Designed, not built as extra runtimes (spec §9). The seams that would change:

- **Surface** — swap `PlaywrightSurface` for a desktop accessibility API; artifact
  `locator.strategy` can grow `native_id` without touching replay/guardrails.
- **Multi-tenant** — locator values are the tenant-specific part; a future
  `variant_overrides` map would be a sparse diff on a base artifact.
- **Drift** — sampled replay of checkpoints per tenant would flag a stale variant,
  and (see above) a single-artifact, human-gated slice of the *repair* half of
  this — not the fleet-wide sampling/flagging half — is actually built.

Nothing in this repo executes more than one mock tenant.

**What's actually proven here vs. what's asserted.** Of the three bullets above,
**Surface** and **Drift** now each have real, live-verified evidence behind
them — the vision fallback is a tested instance of "perception needs a fallback
when the primary channel isn't enough" (the load-bearing idea behind a desktop
`Surface` too), and the drift-aware patch proposal above is a tested instance
of "a failed replay can diagnose and propose its own fix, gated on a human"
(the load-bearing idea behind fleet-wide drift *sampling*, even though the
sampling/scheduling half of that idea isn't built). **Multi-tenant** is the one
bullet with no code behind it at all — no second tenant has ever been executed,
no `variant_overrides` field exists on `Artifact` today. Stating that
distinction plainly here, rather than letting the two real proof points imply
equal confidence in the third.

**Failure modes for the still-unbuilt piece, since a design that hand-waves past
failure is worse than one that's honest about not being built:**
- **`variant_overrides` review and rollback.** A sparse per-tenant diff on a
  base artifact is easy to describe and easy to get wrong in practice — a bad
  override (a locator that happens to work today but is brittle) would only
  surface as a production replay failure for that one tenant, not at
  authoring time, since nothing here validates an override against a live app
  before it ships. The mechanism this repo *does* have for that exact
  problem — checkpoint verification catching a broken locator before it's
  trusted — would need to run against each tenant's live app per override,
  not just the base artifact, which is real added infrastructure, not free.
  Rollback would mean reverting to the base artifact (or a prior override
  version) for that tenant only; nothing here designs what "known-good"
  means across a fleet of per-tenant overrides at scale.
- **Fleet-wide drift *sampling and scheduling* false positives/negatives.**
  The *repair* half is built and proven above (given a known failure, propose
  and gate a fix); the *detection-at-scale* half — deciding which tenants to
  sample, how often, and whether a given mismatch is worth surfacing — is not.
  A checkpoint that still resolves but now means something different (a
  relabeled button, a moved confirmation number) would pass a naive
  checkpoint-presence check while actually being drifted, a false negative.
  Conversely, a transient app outage or maintenance window during a sampled
  replay would look identical to genuine drift, a false positive that would
  incorrectly flag a healthy artifact for re-discovery. Neither is designed
  for here; both would need to be before fleet-wide drift *detection* is more
  than a diagram label, even though drift *repair*, once detected, no longer is.

**Vision fallback — the concrete slice of "apps without a usable DOM tree" that is
actually built and testable.** A desktop `Surface` (real OS-level automation, no
accessibility tree at all) isn't built — designing for it is the deliverable, per
above. But the same underlying gap — the LLM can't turn what it sees into a usable
locator — already happens *within* the web `Surface` too: a hostile app (see
Architecture) can render an interactive control the accessibility tree doesn't
expose cleanly enough for the model to name it. `DiscoveryAgent` now handles this
directly: when a decision is skipped for `missing_locator` (the model wanted to act
but couldn't produce a valid locator from the tree alone), the *next*
`decide_next_action` call for that same page state is retried with a screenshot
attached, routed to a separate vision-capable model
(`Settings.openrouter_vision_model`, distinct from the text-only
`Settings.openrouter_model` — most free-tier text models silently ignore or reject
image content, so this has to be a deliberate model switch, not just an extra field
on the same request). `OpenRouterClient.decide_next_action` builds a multi-modal
`image_url` content block (`data:image/png;base64,...`) instead of a plain string
when a screenshot is supplied. This is exactly the mechanism a desktop `Surface`
would reuse as its *only* perception channel, rather than a fallback — proven here
against a real OpenRouter vision model (`google/gemma-4-26b-a4b-it:free`), not just
mocked. Verified live: a direct call with a screenshot and a deliberately
under-described tree (`"button Search (no accessible name match found)"`) returned
a correctly-formed `click` decision with a real `role`/`name` locator, and the
request payload's `model` field switched to the vision model automatically.

## Escalation & handoff

`EscalationController` (`comp_use/escalation/controller.py`) holds `control` in
`{agent, human, none}`. `escalate()` sets `human`, logs the `InterventionRequest`,
notifies the transport, blocks on `wait_for_resume()`, then returns to `agent`. Every
`InterventionRequest` carries a real `screenshot_path` — both callers capture
`surface.screenshot()` via `EvidenceLogger.save_screenshot()` before escalating, so
the operator's context (per §3.6: "the current state or screenshot") is a file on
disk, not a dropped field.

`LocalSharedBrowserTransport` (`comp_use/escalation/transport.py`) is the built
transport: print the request, wait for the operator to type `resume` while they use
the **same headed Playwright window**. Both engines wire this in symmetrically when
a step is `risky` and `confirm_risky` is false — `discover` and `replay` each have
their own `--confirm-risky` flag on the CLI.

Discovery escalates on three triggers, not two, matching all three named in §3.6:
(1) a step whose `risk_tier` is `RISKY` — checked *before* `self.surface.act()` is
called, not after, so the irreversible action never happens un-confirmed; (2)
`max_steps` exhausted without a `finish` decision — the agent is plainly stuck; (3)
three consecutive skipped/invalid decisions in a row (missing locator, missing
target, allowlist violation, or an unrecognized action) — a dead-end signal that
fires *mid-run*, not just at the end, so a human can unblock the agent and let it
keep going rather than only being told after the whole run failed. All three are
wired through `comp_use/discovery/agent.py`'s `DiscoveryAgent._escalate`, called
from `comp_use/cli.py`'s `_run_discover`.

**What changed during the handoff is captured, not just that it happened.**
`EscalationController.escalate()` takes an optional `surface` reference (wired in
`comp_use/cli.py` for both `_run_discover` and `_run_replay` — the controller is now
constructed *after* the `Surface`, inside the `with sync_playwright()` block, so a
real reference is available). When present, it captures the accessibility tree and a
screenshot both immediately before notifying the operator and immediately after
`wait_for_resume()` returns, saving both screenshots via `EvidenceLogger` and logging
a `difflib.unified_diff` of the two trees. All three — the human's note, the tree
diff, and both screenshot paths — land on the same `escalation_human_action` event,
so a reviewer gets "what the human said they did" and "what actually changed"
side by side, not just one or the other. Verified live against the running mock app:
escalating on `transfer_funds`'s risky "Confirm Transfer" step produced
`escalation_before_step8.png` / `escalation_after_step8.png` plus a tree diff in the
same run's `log.jsonl` (empty in that run, correctly — the human only typed `resume`
without touching the browser, so nothing had changed yet; the automated click happens
*after* the diff is captured, once control returns to the agent).



`ServerStreamingTransport` (CDP screencast / click proxy) is designed only — see
Cuts.

**What the human did is recorded, not just that they resumed.**
`ControlTransport.wait_for_resume()` returns a string; `LocalSharedBrowserTransport`
prompts the operator for a one-line free-text note right after they type `resume`,
and `EscalationController.escalate()` logs it via a dedicated
`escalation_human_action` evidence event (`run_id` + `note`) — separate from the
existing `escalation_resumed` event, which only ever meant "control returned," not
"here's what happened." A full replay of the human's individual clicks (a true
co-browsing recorder) is intentionally out of scope — the brief's own §3.6 carve-out
allows this ("a full real-time co-browsing operator console is out of scope") — but
*some* record of what the human did is an explicit requirement and is now present.

## Safety

- **Allowlist** — `Settings.allowed_url_prefixes` defaults to
  `http://localhost:5000`; `allowed_action_types` is the five UI actions. Enforced in
  `Guardrail.check_allowlist` on discovery and replay.
- **Risk tiers** — `Guardrail.requires_confirmation(risk_tier, confirm_risky)` is
  the single source of truth for "does this step need a human," called from both
  `ReplayEngine.run()` and `DiscoveryAgent.run()` (each also `and`s in its own
  `self.escalation is not None` check — whether an escalation controller is
  wired up at all is a caller concern, not a risk-tier concern, so it stays out
  of `Guardrail`). Each engine has its own `--confirm-risky` CLI flag to skip
  the pause. `DiscoveryAgent._classify_risk` flags a click
  risky when its control name contains "confirm" or "delete" — i.e. the control that
  actually commits an irreversible change, not the link/button that merely navigates
  toward it. (An earlier version matched "transfer"/"sub-account" against the
  navigation link instead of the commit button — backwards, since the link is fully
  reversible and the commit isn't. Adding the confirm-interstitial step surfaced
  this and it's now fixed.)
- **Redaction** — one function, `Guardrail.redact`, applied at every point real
  data reaches an output a human or another system might see: before JSONL
  persistence (`comp_use/evidence.py` walks strings in the event payload), and
  — since finding this session that it wasn't — before every console `print()`
  in `DiscoveryAgent.run()` (routed through `self._print()`). The evidence log
  being clean didn't help if the terminal wasn't: an `ACC-001` value printed to
  stdout unredacted before this fix, confirmed live. Patterns: 9–12 digit IDs,
  `$1,234.56`-style amounts, and the mock bank's own structured identifiers
  (`ACC-\d+`, `SUB-\d+`, `TXN-\d+`, `CONF-\d+`). **Deliberate scope decision:**
  the bare `member_id` (`"12345"`) is *not* redacted. The assignment's own
  example goal is *"look up member 12345"*, used in the open, in prompts —
  redacting every short numeric string would both fight the brief's own
  example and turn goal text and evidence logs unreadable for a value that
  isn't, by itself, an account number or a credential. Account/sub-account/
  transaction/confirmation numbers *are* redacted, since those are the values
  that actually look like real financial-system identifiers. **Known scope
  limit, stated rather than assumed away:** these are hand-picked patterns
  matching this mock app's specific ID shapes, not a general PII detector — a
  name, an SSN in a different format, or an email address would pass through
  unredacted, and nothing in the design claims otherwise.

Hosted OpenRouter still means redacted UI text leaves the machine on **discover**.
Point `LLMClient` at a local model for production locality; `FakeLLMClient` is the
test double.

## Assumptions & the decisions that followed

Per §9's ground rule ("you own everything you submit and must be able to explain
and defend any part of it in detail"), every place the brief left something to our
judgment, gathered in one place rather than scattered across sections. Each is
phrased as *assumption → decision*, so the reasoning is checkable even where it
isn't spelled out again elsewhere.

**Scope & control flow**
- *Assumption:* the upstream agent-facing product decides intent (discover vs.
  replay, which capability) — this system executes reliably, it doesn't infer
  (§1: "the agent-facing product decides what to do; this system is how it
  reliably and safely does it"). *Decision:* the CLI takes an explicit
  `--capability-name` for both `discover` and `replay`; no fuzzy-goal-to-capability
  router was built (see Architecture's own callout on this).
- *Assumption:* a CLI is "the interface" the assignment expects — no separate HTTP
  API or web UI is implied by anything in the brief. *Decision:* no API/UI layer
  built; `comp_use.cli` is the only entrypoint.
- *Assumption:* §3.7's heterogeneity/multi-tenant story is genuinely design-only,
  not "build a second runtime to prove the design." *Decision:* `variant_overrides`
  and drift *sampling* remain pure design (see Heterogeneity & multi-tenant); the
  drift-*repair* half was built later as an explicit stretch, not because the
  design-only framing changed.

**Perception**
- *Assumption:* §3.1's "DOM/accessibility tree, screenshot, or both — your call"
  permits an accessibility-tree-primary design with screenshot only as a targeted
  fallback, not an always-on second input. *Decision:* `DiscoveryAgent` sends a
  screenshot only after a `missing_locator` skip or an `action_failed` — never on
  every step (cost/latency tradeoff, stated explicitly in Cuts).
- *Assumption:* most free-tier OpenRouter text models don't reliably accept image
  content, so vision calls need a distinct, deliberate model switch, not just an
  extra field on the same request. *Decision:* separate
  `Settings.openrouter_vision_model`, chosen only after a first candidate
  (`qwen/qwen2.5-vl-32b-instruct:free`) 404'd and had to be replaced with one
  confirmed live (`google/gemma-4-26b-a4b-it:free`).
- *Assumption:* `TEXT_PRESENT` checking raw `page.content()` (not rendered/visible
  text) is acceptable because the mock app has no client-side JS hiding content —
  every conditional message is a server-rendered Jinja `{% if %}`. *Decision:* kept
  the simpler implementation; documented the portability limit in Architecture
  rather than building rendered-text checking for a case that doesn't exist here.
- *Assumption:* exact-match locator resolution (`exact=True` on
  `get_by_role(name=...)`) is the correct default once the hostile markup showed
  Playwright's substring-match default causing real ambiguity (a decoy "Advanced
  Search" button matching a locator meant for "Search"). *Decision:* `_resolve()`
  always passes `exact=True`.

**Outcome taxonomy**
- *Assumption:* `recoverable` means a transient, dismiss-and-retry condition that
  is neither a permanent business state (`business_outcome`) nor an automation bug
  (`hard_failure`). *Decision:* classified a double-submitted/stale review token
  (a real double-click or back-button scenario) as `recoverable`, after finding and
  fixing the mock app's own crash on that exact case — not before.
- *Assumption:* success checkpoints must generalize across *different* valid
  inputs, not just replay the exact run they were recorded from. *Decision:*
  `_derive_success_checkpoint` builds an `element_visible` checkpoint from the
  final page's own heading, falling back to a literal URL match (with a loud
  warning) only when no heading exists at all.
- *Assumption:* teaching a real LLM to decide "this page is a business outcome" is
  a materially harder problem than teaching it to extract a value it can literally
  see — the first requires judgment about what's *expected* vs. *broken*, the
  second doesn't. *Decision:* fixed the real LLM's ability to use `extract`/
  `extract_as` (issue 13), but left `outcome_patterns` as a stated, deliberate
  manual-authoring limitation rather than attempting the harder problem.

**Risk & escalation**
- *Assumption:* risk should attach to the control that actually commits an
  irreversible action, not any step that merely navigates toward it.
  *Decision:* `_RISKY_TARGET_HINTS = ("confirm", "delete")`, matched against the
  step actually being clicked — an earlier version matched against the
  navigation link instead and had it backwards.
- *Assumption:* §3.6's three escalation triggers (stuck, risky step, replay can't
  recover) apply symmetrically to discovery and replay — a risky step is risky
  regardless of whether it's the first-ever run or a thousandth replay.
  *Decision:* gave `DiscoveryAgent` its own `confirm_risky`/`--confirm-risky`,
  mirroring `ReplayEngine`, rather than only gating replay.
- *Assumption:* §3.6's "record what the human did" doesn't require a full
  co-browsing click-by-click recorder — the brief's own carve-out allows that
  ("a full real-time co-browsing operator console is out of scope") — but *some*
  record is still mandatory. *Decision:* a one-line free-text note plus a
  before/after accessibility-tree diff and paired screenshots; not a full action
  replay.
- *Assumption:* three consecutive skipped/invalid decisions is a reasonable
  dead-end threshold — enough tries to not escalate on a single fluke, few enough
  to not loop for a long time before bringing in a human. *Decision:*
  `_DEAD_END_THRESHOLD = 3`, a fixed constant, not configurable or tuned against
  real failure-rate data (none exists at this scale).

**Safety**
- *Assumption:* redacting the assignment's own example values (a bare `member_id`
  like `"12345"`) would fight the brief's own usage and make goal text unreadable,
  while structured identifiers (`ACC-`, `SUB-`, `TXN-`, `CONF-`) genuinely look
  like real financial-system data. *Decision:* `member_id` stays unredacted by
  design; the structured ID formats are.
- *Assumption:* redaction has to cover every place real data reaches an output a
  human might see, not just the persisted evidence log. *Decision:* found live
  that console `print()` output leaked real values the JSONL log already
  redacted; fixed by routing all `DiscoveryAgent` console output through
  `Guardrail.redact()` too.
- *Assumption:* a general-purpose PII detector is out of scope for a mock app with
  known, narrow data shapes — the goal is protecting *this app's* identifiers, not
  building a redaction product. *Decision:* hand-picked regex patterns only;
  stated as a scope limit in Safety rather than implied to be more general.
- *Assumption:* the mock app's own behavior needs to be trustworthy for "success"
  to mean anything, even though the mock app isn't itself the system under test.
  *Decision:* fixed the mock app's real bug (debiting an account without
  validating the destination existed) rather than treating it as out of scope
  because it lives in `mock_app/`, not `comp_use/`.

**Self-healing (the added, non-required feature)**
- *Assumption:* "deterministic replay, no further LLM calls" (§3.3) is a property
  of the replay *decision loop*, not a ban on any LLM-assisted tooling adjacent to
  a failed run. *Decision:* kept `ReplayEngine` fully LLM-free (verified: it still
  imports nothing from `llm_client`) and put drift diagnosis in a separate module,
  invoked only by the CLI, strictly after the deterministic result already exists.
- *Assumption:* patching a step's own action locator only makes sense when *that
  locator* is what failed to resolve — not when the step succeeded but a
  downstream checkpoint didn't match (a different failure shape entirely).
  *Decision:* `_is_action_locator_failure` gates diagnosis to genuine
  action-locator failures only.
- *Assumption:* consistent with every other risky/uncertain decision in this
  system, a proposed self-healing patch must never take effect without a human
  explicitly reviewing it. *Decision:* patches are always saved as a *new*
  artifact version and always trigger an escalation; nothing auto-applies.

## Cuts

Unchanged from spec §11:

- `ServerStreamingTransport` — designed, not built.
- Production artifact/evidence storage (DB + object store + approval) — not built;
  files on disk only.
- Desktop `Surface` and true multi-tenant execution — not built.
- On-prem LLM — documented, not built; OpenRouter is the live discover path.
- Agent-facing NL-to-params product in front of replay — out of scope; replay takes
  JSON params.
- Screenshot as the *primary* perception channel — not built, and not planned. The
  accessibility tree is primary throughout; screenshot is wired only as a targeted
  fallback (see Heterogeneity & multi-tenant below), not an always-on second input to
  every decision. A full desktop `Surface` (OS-level clicks + a vision model as the
  *only* perception channel, no accessibility tree at all) is still not built — see
  Heterogeneity & multi-tenant.

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
- `evidence/discover_1787149758_open_sub_account/log.jsonl` →
  `artifacts/open_sub_account/v1.json` (includes the "Confirm Sub-Account" review
  step and a trailing `extract` step reading `confirmation_number` off the
  confirmation page — supersedes the earlier
  `evidence/discover_1787105310_open_sub_account/` run, kept for history, which
  predates the `extract`/`output_schema` support added in this pass)
- `evidence/discover_1787149762_transfer_funds/log.jsonl` →
  `artifacts/transfer_funds/v1.json` (includes the "Confirm Transfer" review step
  and a trailing `extract` step reading `txn_id` — supersedes
  `evidence/discover_1787105313_transfer_funds/`, kept for history, same reason)

All three succeeded (`trace.succeeded=True`) end to end through the label-based
locators, the decoy panel, and the review/confirm interstitial. The `extract` step's
first attempt (against a plain `td:has-text('Confirmation Number') + td` CSS
locator) hit a real strict-mode violation caused by the hostile markup itself: the
substring-based `:has-text()` also matched the outer 70%-width layout `<td>` that
wraps the entire page content (since that ancestor's full text also contains
"Confirmation Number"), so `+ td` resolved to the sidebar decoy `<td>` as a second
match. Switched to `:text-is()`, which only matches an element's own exact text, not
a descendant's — fixed, and a genuine example of the hostile app catching a locator
bug rather than a contrived one.

### Replay — success

- `lookup_member`, `member_id=67890` (a different member than any discovery used —
  confirms the artifact generalizes): `evidence/replay_1787105466/log.jsonl` →
  `{"outcome": "success"}`
- `open_sub_account`, `deposit_amount=250`: `evidence/replay_1787149826/log.jsonl`
  → `{"outcome": "success", "outputs": {"confirmation_number": "CONF-000001"}}` —
  the `extract` step's output actually reaches `ReplayResult.outputs`, not `{}`.
- `transfer_funds`, `amount=50`: `evidence/replay_1787149809/log.jsonl` →
  `{"outcome": "success", "outputs": {"txn_id": "TXN-000001"}}`

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
  the business outcomes above. This run predates the `final.png`-on-failure wiring
  below; a fresh `hard_failure` replay of the same capability/params
  (`evidence/replay_1787150562/`) now produces both `log.jsonl` and `final.png`.

### Replay — `validation_error` (no browser)

- `python -m comp_use.cli replay --capability-name lookup_member --params "{}"` →
  `{"outcome": "validation_error", "detail": "missing required param 'member_id'"}`,
  returned before any `surface.act` — the CLI skips launching Chromium entirely on
  this path.

### Replay — `recoverable`

- `transfer_funds`: a real Playwright session drove a transfer through to
  confirmation, then navigated back to the (now-stale, already-consumed)
  review URL — `mock_app/app.py` rendered `"Session Expired"` instead of
  crashing, and `PlaywrightSurface.check_checkpoint()` against the real
  artifact's `recoverable` `OutcomePattern` (`detail: "session_expired"`)
  matched the real page. All five `OutcomeType` values reachable by replay are
  now demonstrated against a real running system — none left theoretical.

### Discovery — real LLM (non-`FakeLLMClient`), committed

`evidence/discover_1787097059/log.jsonl` is a genuine OpenRouter (free-tier model)
discovery run, committed to git, satisfying the assignment's one non-negotiable
requirement ("the discovery run has to be real" — §4). It's distinguishable from the
`FakeLLMClient` runs above by its literal JSON-string-encoded tool arguments (a real
model's tool-call shape, e.g. `"locator": "{\"strategy\": \"role\", ...}"` and
`"target": "None"` as a literal string) rather than `FakeLLMClient`'s clean scripted
dicts. It ran `lookup_member` end to end (`type_text` → `click` → `finish`,
`done: true`) against the mock app.

To re-run **live** OpenRouter discovery yourself, set `OPENROUTER_API_KEY` and follow
the README Demo path.

### Discovery — real LLM, checkpoint-fix verification (2026-08-19)

An earlier version of `_run_discover` baked the literal final URL of a single
discovery run into `success_checkpoint` (e.g. `member/12345`), which meant a
replay with any *different* valid input — the exact thing artifacts are supposed
to generalize across — was misreported as `hard_failure`. Fixed by deriving the
checkpoint from the final page's own heading instead (`_derive_success_checkpoint`
in `comp_use/cli.py`). Verified with a real (non-`FakeLLMClient`) run, following
the README's Demo path exactly as written:

```
python -m comp_use.cli discover --goal "Look up member 12345 and view their account balances" \
  --start-url "http://localhost:5000/member/search" --capability-name lookup_member
```

against a capability name that already had a committed `v1.json`. Result:
`artifacts/lookup_member/v2.json` (model: `nvidia/nemotron-3.5-lightning:free`)
with `success_checkpoint: {"type": "element_visible", "locator": {"role":
"heading", "name": "Member Detail"}}` — `v1.json` left byte-for-byte unchanged
(artifact versioning fix, same pass). Replaying `v2.json` with `member_id=67890`
(never used in any discovery run for this capability):
`evidence/discover_1787154166/log.jsonl` → artifact →
`{"outcome": "success"}` — the exact scenario that previously returned
`hard_failure` now returns `success`.
