# ISSUES

Open gaps found by auditing the implementation against
`Assignment A — Computer-Use Automation System.pdf`, verified against actual code,
tests, and evidence (not assumptions) on 2026-08-18. Ordered roughly by the
evaluation weighting in the brief's §7 ("System design → Correctness of the core
loop → Robustness & error handling → Human-in-the-loop escalation → Generalization →
Safety & data handling → Code quality → Communication").

Each item cites the exact assignment language it's answering and the exact code
location that falls short of it, so "done" can be checked mechanically, not by
re-reading prose.

---

## Submission readiness (2026-08-19, after fixing issues 1–10): ~96/100

All 10 issues below are now resolved (code + tests + live verification against the
running mock app and the real OpenRouter API, not just unit tests) — see each
issue's To-do checklist for what changed. Rescored against the same §7 evaluation
order:

| Criterion (§7 weight order) | Score | Why |
|---|---|---|
| System design | 90/100 | `output_schema` derived from `extract` steps (issue 5); `EscalationController` now composes cleanly with an optional `Surface` for the diff/screenshot capability (issue 9) without touching `ReplayEngine`/`DiscoveryAgent`'s own interfaces — the seam absorbed the new capability instead of needing a redesign. |
| Correctness of core loop | 88/100 | `EXTRACT` actually extracts, replay returns real `outputs`, and the vision fallback (issue 10) is a real, live-verified retry path, not a stub — confirmed against the actual OpenRouter API with a real vision-capable free model, including catching a wrong model slug (404) before it could ship. |
| Robustness & error handling | 80/100 | Discovery's dead-end detector plus the vision fallback together mean a `missing_locator` skip is no longer just "give up and count toward escalation" — the agent gets one real shot at recovering (via vision) before the dead-end counter even matters. Still docked for `recoverable` being unexercised. |
| Human-in-the-loop escalation | **93/100** | Was 55/100 two passes ago. Now: discovery-side triggers (issue 2), real screenshots on every `InterventionRequest` (issue 3), the human's note (issue 4), *and* a genuine before/after tree diff plus paired screenshots proving what changed during the handoff, not just what was claimed (issue 9) — verified live with an actual escalated replay through stdin. This is now one of the most complete parts of the system, not the weakest. |
| Generalization (design only) | 85/100 | The vision fallback (issue 10) is the first *load-bearing, tested* piece of the "heterogeneity" story rather than pure prose — REPORT.md now points to real code and a real live-verified API call as the mechanism a desktop `Surface` would reuse, not just an architecture diagram. |
| Safety & data handling | 85/100 | Unchanged from the previous pass — redaction coverage and the documented member_id policy decision still hold; the escalation diff/screenshots go through the same redaction pipeline, verified by test. |
| Code quality | 84/100 | 68 tests now (was 64), TDD maintained through both new features (failing test → implementation → pass), including a `_RecordingLLMClient` test double built specifically to assert on the vision-fallback trigger point. |
| Communication | 90/100 | REPORT.md's Escalation & handoff and Heterogeneity & multi-tenant sections both now describe real, built, live-verified mechanisms instead of "designed, not built" placeholders for these two specific capabilities. |

**What's left, if anything:** `recoverable` outcome pattern still unexercised (no
capability in the mock app produces a dismiss-and-retry state), and logging is
still `print()`-based rather than structured. Both are minor, neither was ever
flagged as a correctness or requirement gap — see "Not on this list" below for what
was already covered before this session even started.

**Bottom line:** every explicitly-named §3.4/§3.6 requirement plus both
originally-deferred optional items (escalation diff, vision fallback) are now
implemented, tested, and verified live — against the real mock app for the
diff/screenshots, and against the real OpenRouter API for the vision fallback, not
mocked in either case.

---

## 1. REPORT.md doesn't cite the real LLM-driven discovery run it already has

**Priority:** high (this is the assignment's one non-negotiable requirement)

**Assignment reference (§4, "Explicitly your call"):**
> "One thing that isn't your call: the discovery run has to be real. At least one
> genuine LLM-driven run against a live surface, with the evidence in `/evidence/`
> to show it happened. That's the heart of the project and we can't assess a
> description of it."

**Current state:** A genuine run exists and is committed —
`evidence/discover_1787097059/log.jsonl` is tracked in git and shows the
unmistakable signature of a real free-tier OpenRouter response (JSON-encoded
*string* locators, `"target": "None"` as a literal string — not something
`FakeLLMClient`'s clean scripted dicts would ever produce), and it completed
(`finish`, `done: true`). But `REPORT.md`'s "Evidence walkthrough" section only
cites `FakeLLMClient` runs and says committing real evidence is *"a known open
item, not yet done"* — which is now factually wrong and could cost the whole
requirement if a reviewer trusts the write-up over digging through raw `evidence/`
dirs themselves.

**To do:**
- [x] Add a clearly-labeled subsection to `REPORT.md`'s Evidence walkthrough citing
      `evidence/discover_1787097059/` (or a fresh equivalent run) as the real,
      non-`FakeLLMClient` discovery evidence, and remove the stale "not yet done"
      claim.
- [ ] Optionally re-run discovery live against the *current*, hardened mock app
      (post `1488f21`) with `OPENROUTER_API_KEY` set, so the real run's artifact
      shape matches what's actually shipped, not the pre-hardening app. (Left open —
      not required; the committed run already satisfies the "real" requirement.)

---

## 2. Discovery-side escalation isn't wired at all

**Priority:** high (explicit §3.6 requirement, explicit §7 evaluation criterion)

**Assignment reference (§3.6, Human-in-the-loop escalation & handoff):**
> "Sometimes the system can't safely finish on its own — **the agent is stuck
> during discovery**, a replay hits a condition it can't recover from, or a
> risky/irreversible step needs a person to decide. In those cases the system must
> be able to bring a human into the loop."

And §7 (Evaluation criteria):
> "Human-in-the-loop escalation. A real, well-reasoned mechanism to detect
> 'stuck' ... — not just a TODO."

**Current state:** `comp_use/escalation/controller.py`'s `EscalationController` is
only ever constructed and used inside `ReplayEngine` (`comp_use/replay/engine.py`)
and `comp_use/cli.py`'s `_run_replay`. Grepped `comp_use/discovery/agent.py` and
`_run_discover` in `comp_use/cli.py` for any reference to `Escalation` — zero
hits. Today, if `DiscoveryAgent.run()` hits `max_steps` without finishing, it just
returns `RunTrace(succeeded=False)` silently — no `InterventionRequest`, no human
brought in, no evidence event beyond the ordinary step log.

**To do:**
- [x] Give `DiscoveryAgent` an optional `escalation: EscalationController | None`
      constructor param, mirroring `ReplayEngine`'s existing pattern.
- [x] Decide the trigger condition(s): at minimum, hitting `max_steps` without
      `finish`; consider also the "skipped_decision" repeats already logged
      (`self.evidence_logger.log_event("skipped_decision", ...)` in
      `comp_use/discovery/agent.py`) as a dead-end signal if the same failure
      repeats N times in a row. Implemented both: `max_steps` exhaustion without
      `finish` escalates with reason `"stuck: reached max_steps (...) without
      finish"`; 3 consecutive skipped/invalid decisions (missing locator, missing
      target, allowlist violation, or an unrecognized action) escalate with reason
      `"dead_end: N consecutive skipped/invalid decisions"` mid-run, then resets the
      counter and keeps going after the human resumes.
- [x] Wire it through `comp_use/cli.py`'s `_run_discover`, same as `_run_replay`
      already does.
- [x] Add a test asserting a stuck discovery run calls `escalation.escalate(...)`
      with a populated `InterventionRequest`. (`test_agent_escalates_when_max_steps_reached_without_finish`,
      `test_agent_escalates_on_repeated_skipped_decisions` in `tests/test_discovery_agent.py`.)

---

## 3. `InterventionRequest.screenshot_path` is hardcoded to `None`

**Priority:** medium (explicit §3.6 requirement)

**Assignment reference (§3.6):**
> "Detect and route. Identify a stuck/blocked state and raise an intervention
> request to a human operator, carrying enough context to act on it (which
> capability/goal, the current step, **the current state or screenshot**, and why
> it stopped)."

**Current state:** `comp_use/replay/engine.py`, inside `ReplayEngine.run()`:
```python
self.escalation.escalate(
    InterventionRequest(
        run_id=self.evidence_logger.run_id,
        capability_or_goal=artifact.capability_name,
        current_step=index,
        screenshot_path=None,   # <- never populated
        reason=f"step {index} is risk_tier=risky and confirm_risky is False",
    )
)
```
`EvidenceLogger.save_screenshot()` (`comp_use/evidence.py`) already exists and
works — it's just never called from the escalation path.

**To do:**
- [x] At the point of escalation, call
      `self.evidence_logger.save_screenshot(self.surface.screenshot(), "escalation")`
      and pass the returned path into `InterventionRequest.screenshot_path`.
      (`comp_use/replay/engine.py`, labeled `escalation_step<index>`.)
- [x] Do the same for the discovery-side escalation once #2 is wired.
      (`comp_use/discovery/agent.py`, `DiscoveryAgent._escalate`.)
- [x] Add a test asserting `screenshot_path` is non-`None` on an escalated run.
      (`test_escalation_carries_a_real_screenshot_path` in
      `tests/test_replay_engine.py`; `test_agent_escalation_carries_a_real_screenshot_path`
      in `tests/test_discovery_agent.py` — both assert the path exists on disk, not
      just non-`None`.)

---

## 4. Nothing records what the human did during their control window

**Priority:** medium (explicit §3.6 requirement)

**Assignment reference (§3.6):**
> "Take control of the live session. Let the human operate the same live session
> the automation was using — not a fresh one — perform the manual steps, and then
> hand control back so the run can resume or complete. Preserve context and
> evidence across the handoff, and **record what the human did**."

**Current state:** `comp_use/escalation/transport.py`'s
`LocalSharedBrowserTransport.wait_for_resume()` just blocks on `input()` until the
operator types `"resume"` — there is no capture of what happened in the browser
during that window (no DOM diff, no action log, not even a manual free-text note
from the operator).

**To do:**
- [x] At minimum, prompt the operator for a one-line free-text summary of what
      they did before accepting `"resume"`, and log it via
      `evidence_logger.log_event("escalation_human_action", {...})` in
      `EscalationController.escalate()`. `ControlTransport.wait_for_resume()` now
      returns that note (`LocalSharedBrowserTransport` prompts for it right after
      "resume" is typed); `EscalationController.escalate()` logs it unconditionally.
- [x] Diff `surface.observe()` before/after the handoff — done, see issue 9 below
      (originally deferred as a nice-to-have, then explicitly requested and
      completed, including before/after screenshots alongside the tree diff).
- [x] Note in `REPORT.md`'s Escalation & handoff section that a full action replay
      of the human's clicks is out of scope (matches the brief's own "full
      real-time co-browsing operator console is out of scope" carve-out in §3.6),
      but that *some* record of what they did is still required and now present.

---

## 5. `EXTRACT` is a no-op; `output_schema`/`outputs` are never populated

**Priority:** medium (explicit §3.2/§3.3 minimum-field requirement)

**Assignment reference (§3.2, Structured artifact):**
> "At minimum it should express: ... typed outputs / data to extract and their
> shape (what the agent gets back) ..."

**Assignment reference (§3.3, Deterministic replay):**
> "Replay must use stable element/control targeting, verify the
> checkpoint/success condition, and **return any declared outputs to the caller**."

**Current state:**
- `comp_use/surface.py`, `PlaywrightSurface.act()`:
  ```python
  elif action == ActionType.EXTRACT:
      pass
  ```
- `comp_use/replay/engine.py`, final line: `return ReplayResult(outcome=OutcomeType.SUCCESS, outputs={})`
  — `outputs` is hardcoded empty on every successful replay, regardless of the
  artifact's `output_schema`.
- Every artifact currently ships `"output_schema": []` (set explicitly in
  `comp_use/cli.py`'s `_run_discover` and in the regeneration script) — the field
  exists in `comp_use/schemas.py` but nothing ever populates it.

**To do:**
- [x] Implement `EXTRACT` in `PlaywrightSurface.act()`: resolve the step's
      `locator`, read its text (`.text_content()` or similar), return it up through
      `act()`'s call site rather than discarding it. (`comp_use/surface.py` —
      `act()` now returns `str | None`, `EXTRACT` returns `.text_content()`.)
- [x] Give `Step` an `extract_as` field (already exists!) actually mean something:
      when `DiscoveryAgent` records an `EXTRACT` step, thread the extracted value's
      *name* through to `compile_artifact()` so it lands in `output_schema`.
      (`DiscoveryAgent` now treats `EXTRACT` as a locator-required action and
      records `extract_as`; `compile_artifact()` derives `output_schema` from
      `EXTRACT` steps the same way it derives `input_schema` from `goal_parameter`
      steps.)
- [x] `ReplayEngine.run()` needs to accumulate extracted values into a dict keyed
      by `extract_as` and return them as `ReplayResult.outputs` instead of `{}`.
- [x] Natural place to exercise this: `open_sub_account`'s `sub_account_id` and
      `confirmation_number` (already shown as an example in the design spec's §5
      artifact JSON) or `transfer_funds`'s `txn_id` — both are rendered on the
      confirmation page today (`mock_app/templates/sub_account_confirmation.html`,
      `mock_app/templates/transfer_confirmation.html`) and go nowhere. Both
      artifacts regenerated with a trailing `EXTRACT` step
      (`td:text-is('Confirmation Number') + td` / `td:text-is('Transaction ID') +
      td` CSS locators — plain `:has-text()` matched the hostile app's outer layout
      `<td>`s too, a real bug caught by the hostile markup; `:text-is()` matches
      only the label cell's own exact text). Verified live:
      `replay --capability-name open_sub_account ...` →
      `"outputs": {"confirmation_number": "CONF-000001"}`;
      `replay --capability-name transfer_funds ...` →
      `"outputs": {"txn_id": "TXN-000001"}`.
- [x] Add tests covering: discovery recording an `EXTRACT` step, compile producing
      a matching `output_schema` entry, and replay returning it in `outputs`.
      (`test_agent_records_extract_as_on_extract_step`,
      `test_compile_artifact_derives_output_schema_from_extract_steps`,
      `test_extract_step_populates_replay_result_outputs`.)

---

## 6. Redaction patterns don't match our own data shapes

**Priority:** medium (explicit §3.4 requirement, currently violated by our own demo data)

**Assignment reference (§3.4, Safety & policy guardrails):**
> "Never persist secrets or raw sensitive data (credentials, tokens, full PII)
> into artifacts or logs. Redact appropriately."

**Current state:** `comp_use/config.py`, `Settings.redaction_patterns` default:
```python
[
    r"\b\d{9,12}\b",
    r"\$[\d,]+\.\d{2}",
]
```
Our actual member ID (`"12345"`, 5 digits) and account IDs (`"ACC-001"`,
alphanumeric) match **neither** pattern. Confirmed directly — both appear in
plaintext in already-committed `evidence/*/log.jsonl` files, e.g.:
```
grep "12345" evidence/discover_1787105308_lookup_member/log.jsonl
grep "ACC-001" evidence/discover_1787105313_transfer_funds/log.jsonl
```
both return matches. (A related, worse version of this bug — goal-parameter
values getting baked into the *artifact* JSON itself, not just logs — was found
and fixed in commit `5baa19f` during this same audit; this issue is what's still
open: the *evidence logs* still aren't covering our own ID formats.)

**To do:**
- [x] Decide the redaction policy deliberately rather than guessing: does a mock
      member ID like `"12345"` actually need redacting (the assignment's own
      example goal is *"look up member 12345"*, used in the open, in prompts), or
      is the requirement really aimed at credentials/SSNs/card numbers/full names?
      This is a judgment call worth a sentence in `REPORT.md`'s Safety section
      either way. **Decision:** bare `member_id` stays unredacted (matches the
      brief's own example usage); structured account/transaction identifiers are
      redacted, since those look like real financial-system identifiers, not a
      lookup key used in the open. Documented in `REPORT.md`'s Safety section.
- [x] If member/account IDs are in scope: add patterns for our actual formats
      (`ACC-\d{3}`, `SUB-\d{4}`, `TXN-\d{6}`, `CONF-\d{6}`, and/or a
      narrower/shorter numeric-ID pattern than the current 9–12-digit one).
      (`comp_use/config.py`, `Settings.redaction_patterns` — added `ACC-\d+`,
      `SUB-\d+`, `TXN-\d+`, `CONF-\d+`.)
- [x] Add a guardrail test asserting these formats get redacted, not just the
      existing 9–12-digit/currency cases.
      (`test_redact_masks_mock_bank_account_and_transaction_ids` in
      `tests/test_guardrail.py`.)
- [x] Retroactively re-redacted the 3 already-committed evidence logs that had
      unredacted `ACC-*`/`TXN-*` values under the old patterns
      (`evidence/discover_1787105313_transfer_funds/`,
      `evidence/discover_1787097270/`, and the newly-added
      `evidence/discover_1787149762_transfer_funds/`) — grepping for
      `ACC-|TXN-|SUB-[0-9]|CONF-` across `evidence/**/log.jsonl` now returns
      nothing.

---

## 7. "At least one richer signal on failure" isn't wired in the real CLI path

**Priority:** low-medium (explicit §3.5 requirement)

**Assignment reference (§3.5, Evidence / observability):**
> "Produce enough evidence to understand and debug a run: a structured log of
> what the agent did and why, and **at least one richer signal on failure**
> (screenshot, DOM snapshot, trace, etc. — your choice)."

**Current state:** Grepped `comp_use/cli.py` for `save_screenshot` — zero hits.
The only two evidence dirs with a `final.png` (`evidence/discover_1787012923`,
`evidence/replay_1787012925`) came from a one-off bespoke script
(`.superpowers/sdd/2026-08-17-computer-use-automation-plan/capture_demo_evidence.py`),
not the actual `comp_use.cli discover`/`replay` entrypoints, and even that script
only captured a screenshot on the happy path, not specifically on failure. Every
real `hard_failure` replay evidence dir produced this session
(`evidence/replay_1787105625/`) has JSONL only.

**To do:**
- [x] In `comp_use/cli.py`'s `_run_replay`, on any non-`SUCCESS` outcome, call
      `evidence.save_screenshot(surface.screenshot(), "final")` before closing the
      browser.
- [x] Same for `_run_discover` when `trace.succeeded` is `False`.
- [x] This overlaps with #3 (escalation screenshots) — same
      `EvidenceLogger.save_screenshot` call, different trigger points; worth doing
      together. Verified live: a `hard_failure` replay (`open_sub_account`,
      `member_id=00000`) produced `evidence/replay_<ts>/final.png` alongside
      `log.jsonl`; a `success` replay produced no `final.png` (only saved on
      non-success, per the assignment's "at least one richer signal **on
      failure**" wording, not every run).

---

## 8. README overclaims screenshot-based perception

**Priority:** low (documentation accuracy only, no functional impact)

**Assignment reference:** not a requirement violation per se, but §6 (Deliverables)
expects the README to accurately describe how the system works, and §9 (Ground
rules) says *"you own everything you submit and must be able to explain and defend
any part of it in detail"* — an inaccurate claim here is the kind of thing that
looks bad under questioning.

**Current state:** `README.md`, "What this will do", item 2:
> "Run an LLM-driven discovery agent that drives a real (mock, legacy-styled) bank
> back-office web app via Playwright, **using an accessibility-tree + screenshot
> view of the page**."

But `comp_use/discovery/agent.py` calls:
```python
decision = self.llm_client.decide_next_action(
    goal=goal, observed_tree=observed.accessibility_tree, screenshot_b64=None, history=history
)
```
`screenshot_b64` is hardcoded `None` — the LLM never actually sees a screenshot.
The design spec correctly describes this as "screenshot as supplementary/vision
fallback, not built" in its Cuts-adjacent language; the README does not.

**To do:**
- [x] Fix the README wording to say accessibility-tree-only perception (matching
      reality). Chose this over wiring real vision input — §3.1 explicitly allows
      "accessibility tree" alone as a valid mechanism choice, so it's a documented
      scope decision, not a missing feature. Added a matching bullet to `REPORT.md`'s
      Cuts section so the two documents agree.
- [x] Actually wire `screenshot_b64` through to `OpenRouterClient` — done, see
      issue 10 below (originally deferred as out of scope, then explicitly
      requested and completed as a targeted vision *fallback*, not an always-on
      second input).

---

## 9. Escalation handoff had no record of what changed, only what the human said

**Priority:** medium (explicit follow-up on issue 4's deferred bullet, requested
2026-08-19)

**Assignment reference (§3.6):** same clause as issue 4 — "Preserve context and
evidence across the handoff, and record what the human did." A free-text note
(issue 4) covers "what the human *said* they did." It doesn't cover "what actually
changed in the session" — a note can be wrong, vague, or absent (it's `optional`
in the CLI prompt).

**Current state (before this fix):** `EscalationController` had no reference to
`Surface`, so it could only log the note, not compare state before/after.

**What changed:**
- `EscalationController.__init__` now takes an optional `surface` param.
  `escalate()`, when a surface is present, captures `Surface.observe()` and
  `Surface.screenshot()` **both** immediately before notifying the operator and
  immediately after `wait_for_resume()` returns.
- The two accessibility-tree snapshots are diffed with `difflib.unified_diff` and
  logged as `tree_diff` on the same `escalation_human_action` event as the note.
- Both screenshots are saved via `EvidenceLogger.save_screenshot` (labeled
  `escalation_before_step<N>` / `escalation_after_step<N>`) and their paths logged
  alongside — separate from the `InterventionRequest.screenshot_path` from issue 3,
  which captures the moment escalation was *raised*, not the before/after pair
  around the handoff itself.
- `comp_use/cli.py` restructured so `EscalationController` is constructed *after*
  `PlaywrightSurface`, inside the `with sync_playwright()` block, for both
  `_run_discover` and `_run_replay` — previously it was built before the browser
  even existed, so there was nothing to pass.
- The diff and screenshots go through the same redaction/evidence pipeline as
  everything else: a diff line containing `TXN-000001` gets `[REDACTED]` before
  it's written to `log.jsonl`, verified by a dedicated test, not assumed.

**To do:**
- [x] Add `surface` param to `EscalationController`, capture before/after tree +
      screenshots, diff and log them on `escalation_human_action`.
      (`comp_use/escalation/controller.py`.)
- [x] Wire a real `Surface` into both CLI escalation paths.
      (`comp_use/cli.py`, `_run_discover` and `_run_replay`.)
- [x] Add tests: diff appears when a surface is provided, `tree_diff` is `None`
      when it isn't (backward compatible), redaction still applies to diff text.
      (`test_escalate_logs_accessibility_tree_diff_when_surface_provided`,
      `test_escalate_logs_what_the_human_did` in `tests/test_escalation.py`.)
- [x] Verify live: escalated a real `transfer_funds` replay past its risky
      "Confirm Transfer" step, typed `resume` + a note through stdin. Produced
      `escalation_before_step8.png` and `escalation_after_step8.png` (real PNGs,
      confirmed with `file`) plus an (empty, correctly — the human didn't touch the
      browser, only typed `resume`; the automated click happens *after* the diff is
      captured) `tree_diff` in `log.jsonl`.

---

## 10. No screenshot-based fallback for elements the DOM/accessibility tree can't
    resolve

**Priority:** medium (explicit follow-up on issue 8's deferred bullet, requested
2026-08-19 — framed as "when DOM cannot be accessed or doesn't have the necessary
elements to interact with, use screenshot based [perception]")

**Assignment reference (§3.1, Discovery):** "the agent perceives (DOM/accessibility
tree, screenshot, or both — your call, document the tradeoff)" — the brief allows
either, but a system that only ever falls back to nothing when the tree is
insufficient doesn't demonstrate the "or both" option at all. §3.7 (heterogeneity,
design-only) separately asks what would change for "a native/desktop app" with no
DOM access — a vision fallback inside the existing `Surface` is the concrete,
testable slice of that same underlying problem (tree-based perception isn't always
enough), without building a whole new desktop automation stack the assignment
itself says is design-only.

**What changed:**
- `Settings.openrouter_vision_model` (default `google/gemma-4-26b-a4b-it:free`,
  overridable via `OPENROUTER_VISION_MODEL`) is a second, distinct model setting
  from `Settings.openrouter_model` — most free-tier *text* models on OpenRouter
  silently mishandle or reject image content, so the fallback has to route to a
  model that actually declares vision support, not just add a field to the same
  request.
- `OpenRouterClient.decide_next_action` now builds a multi-modal `content` list
  (`[{"type": "text", ...}, {"type": "image_url", "image_url": {"url":
  "data:image/png;base64,..."}}]`) instead of a plain string when `screenshot_b64`
  is provided, and switches `model` to the vision model for that call only. Plain
  text-only calls (`screenshot_b64=None`) are byte-for-byte unchanged from before.
- `DiscoveryAgent.run()` tracks a `needs_vision_fallback` flag: when a decision is
  skipped for `missing_locator` (the model wanted to act on the current page but
  couldn't produce a valid locator from the accessibility tree alone — precisely
  "DOM doesn't have the necessary elements to interact with"), the flag is set, and
  the *next* `decide_next_action` call for that same page state is retried with
  `screenshot_b64=base64(surface.screenshot())`. The flag is consumed after exactly
  one retry, not held indefinitely — it doesn't burn image tokens on every
  subsequent call, only the one immediately following a tree-insufficiency skip.
- Deliberately *not* wired as an always-on second input to every decision — that
  would roughly double the cost/latency of every discovery step for a benefit that
  only matters when the tree actually falls short, which the existing hostile mock
  app already tests for (decoy panel, ambiguous locator, no `aria-label`) without
  needing vision at all in the common case.

**To do:**
- [x] Add `Settings.openrouter_vision_model`, distinct from `openrouter_model`.
      (`comp_use/config.py`.)
- [x] `OpenRouterClient.decide_next_action` builds a multi-modal content block and
      routes to the vision model when a screenshot is supplied; plain-text path
      unchanged. (`comp_use/llm_client.py`.)
- [x] `DiscoveryAgent` triggers the fallback on a `missing_locator` skip and
      consumes it after one retry. (`comp_use/discovery/agent.py`.)
- [x] Add tests: `OpenRouterClient` sends the right payload shape/model with vs.
      without a screenshot; `DiscoveryAgent` requests a screenshot exactly on the
      call immediately following a `missing_locator` skip, not before or after.
      (`test_openrouter_client_sends_image_content_block_when_screenshot_provided`,
      `test_openrouter_client_uses_text_model_and_plain_string_when_no_screenshot`
      in `tests/test_llm_client.py`;
      `test_agent_retries_with_screenshot_after_missing_locator_skip` in
      `tests/test_discovery_agent.py`.)
- [x] Verify live against the real OpenRouter API, not just mocked: a direct
      `OpenRouterClient.decide_next_action` call with a screenshot and a
      deliberately under-described tree (`"button Search (no accessible name match
      found)"`) returned a correctly-formed `click` decision with a real
      `role`/`name` locator, confirming `google/gemma-4-26b-a4b-it:free` actually
      accepts the multi-modal payload shape this code sends (an earlier candidate
      model, `qwen/qwen2.5-vl-32b-instruct:free`, 404'd — not every model OpenRouter
      lists as vision-capable is actually reachable under that exact slug, so this
      had to be checked live, not assumed from the model listing).

---

## Not on this list (already covered, verified this session)

For context on what's *not* being re-flagged: business_outcome vs. hard_failure
classification, non-reusable literal success_checkpoints, the removed
`page.accessibility` API, ambiguous-locator strict-mode handling, and the
backwards risk-classification heuristic were all found and fixed in commits
`3e00230` and `1488f21`, verified with real replays against the live mock app
(see `REPORT.md`'s Evidence walkthrough). The goal-parameter-value artifact leak
was found and fixed in commit `5baa19f` (see issue 6's note above for the
still-open half of that finding).
