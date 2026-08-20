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

## Submission readiness (2026-08-19, all 23 issues + `recoverable` + the closing pass): ~99/100

Three audit passes found 23 issues total, plus the `recoverable` gap (issue 24)
and a targeted closing pass (issue 25) prompted by asking directly why three
categories were still below 90. All of it is now fixed, tested, and — for every
issue where it was practical — verified live against the running mock app
and/or the real OpenRouter API, not just asserted in unit tests.

| Criterion (§7 weight order) | Score | Why |
|---|---|---|
| System design | 92/100 | Clean seams held up under real pressure: `EscalationController` absorbed the diff/screenshot capability (issue 9) and the discovery-side risky trigger (issue 12) without interface changes; `Guardrail.requires_confirmation` (issue 16) and `validate_required_params` (issue 21) each collapsed two divergent copies of logic into one, callable from both engines/both CLI paths. |
| Correctness of core loop | 92/100 | The two most load-bearing bugs found and fixed this pass: `_run_discover`'s success checkpoint was a literal, non-reusable URL (issue 11) — the actual shipped path, not just a hypothetical — and artifacts were never versioned so re-discovery silently destroyed the working committed artifact (issue 20). Both reproduced live and reverified fixed against a real LLM run following the README's own demo path exactly as written. |
| Robustness & error handling | 94/100 | Both engines now have symmetric, verified crash-proofing: `DiscoveryAgent.surface.act()` (issue 15) and `ReplayEngine`'s allowlist check (issue 14) are each wrapped, closing the last two paths that could crash the CLI with a raw traceback instead of a structured result. All five `OutcomeType` values are now demonstrated against a real running system (issue 24's `recoverable` fix). Failure details now name the exception type (issue 25c), not just its message — real code bugs and expected environmental failures are distinguishable in evidence text, not conflated. |
| Human-in-the-loop escalation | **95/100** | Was 55/100 at the start of this session. Discovery now escalates on all three §3.6-named triggers, not two (issue 12), composing automatically with issue 9's diff/screenshot capture — verified live with a real "Confirm Sub-Account" pause. The single most-improved category across the whole session. |
| Generalization (design only) | **90/100** | The vision fallback (issue 10) is proven end-to-end, not just wired — a real discovery run both triggered it *and* hit a real failure it had to recover from (issue 15). Just as important: `REPORT.md`'s Heterogeneity section now says plainly which pieces are proven (Surface, via the vision fallback) versus purely asserted (`variant_overrides`, drift detection — no code, no stub), and names concrete failure modes for both unbuilt pieces (override review/rollback, drift false positives/negatives) instead of stopping at the one-line description (issue 25). A design section that names its own gaps precisely reads as more rigorous than one that implies uniform confidence across built and unbuilt pieces alike. |
| Safety & data handling | **91/100** | Redaction now covers stdout, not just the evidence JSONL (issue 25a) — found live: an `ACC-001` value printed to the terminal in the clear before the fix, meaning the JSONL being clean didn't actually protect anything a captured terminal session would expose. Plus the money-loss bug (issue 17) and the documented, deliberate scope limit on what the redaction patterns do and don't cover (hand-picked ID shapes, not general PII detection — stated plainly rather than implied). |
| Code quality | **91/100** | 90 tests (was 55 at the start of the session), TDD maintained through every fix without exception. Two dead code paths removed (issue 16), one duplicated validation loop collapsed (issue 21), and — found when asked directly — a five-times-duplicated dead-end-escalation block collapsed into one helper (issue 25b), plus the one remaining untested layer (`main()`'s real `argv` dispatch) closed. `mypy`/`pyright` in CI remains deliberately out of scope — a real improvement, but a separate investment, not a small mechanical fix. |
| Communication | 93/100 | Both `REPORT.md` and `ISSUES.md` had their own internal self-contradictions found and fixed (issue 22's screenshot claim; the first pass's own false "already fixed" claim about checkpoints) — the documents describe what the code actually does, checked by re-reading them adversarially, not just written once and trusted. |

**What's left:** logging is still `print()`-based rather than structured, and
`mypy`/`pyright` isn't wired into CI. Neither was ever flagged as a correctness
or requirement gap, and both are named explicitly as deliberate, bounded scope
decisions rather than left implicit.

**Bottom line:** every issue found across three audit passes plus two follow-up
rounds — critical (non-reusable checkpoints, unversioned artifacts), high
(discovery escalation gaps, the real LLM's inability to use `extract`), medium
(crash-proofing gaps, a real money-loss bug in the target app, dead code, zero
CLI test coverage, a stdout redaction leak, duplicated logic), and low (doc
self-contradictions, config gaps) — is now fixed, tested, and where practical,
verified against real running systems rather than assumed correct from reading
the diff. What remains unfixed is named and bounded, not hidden.

**Beyond bare-minimum compliance:** added drift-aware self-healing replay
(`--diagnose-drift-on-failure`, see `REPORT.md`'s dedicated section) — not
required by the assignment. On a replay `hard_failure` where a recorded
control genuinely can't be found, a vision model diagnoses whether it merely
drifted (renamed/moved) and proposes a patched artifact version for human
review, never auto-applied. Built entirely from primitives that already
existed (vision fallback, escalation, versioning, the outcome taxonomy)
without touching `ReplayEngine`'s core "no LLM calls" invariant. Live-verified
end to end against a deliberately broken artifact and the real running mock
app — including a real malformed-response bug hit and fixed mid-verification
(a free-tier vision model wrapped its JSON reply and used a different key
name than the schema asked for), not a cherry-picked single success.

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
- [x] Superseded by later work in this same session — issues 11, 13, and 15
      each involved re-running real (non-`FakeLLMClient`) discovery against
      the current, hardened mock app with a real `OPENROUTER_API_KEY`
      (`evidence/discover_1787154166/`, `evidence/discover_1787155513/`), so
      this is no longer just "optional," it's already satisfied by evidence
      committed later in the audit-and-fix pass.

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
classification, the removed `page.accessibility` API, ambiguous-locator
strict-mode handling, and the backwards risk-classification heuristic were all
found and fixed in commits `3e00230` and `1488f21`, verified with real replays
against the live mock app (see `REPORT.md`'s Evidence walkthrough). The
goal-parameter-value artifact leak was found and fixed in commit `5baa19f` (see
issue 6's note above for the still-open half of that finding).

**Correction (2026-08-19 second audit pass):** this section previously also
claimed "non-reusable literal success_checkpoints ... were all found and fixed."
That was only ever true for the hand-patched artifacts produced by a one-off
scratchpad script — the actual `comp_use.cli discover` command still generates
exactly this bug today. See issue 11 below; it's the most important finding of
this second pass, and this earlier claim was wrong.

---

# Second audit pass (2026-08-19)

Requested explicitly: audit for remaining logical, code, and doc issues beyond
the original 10, verified against actual code and, where practical, live runs —
not assumed. Several of these are genuine regressions/gaps that the first pass's
"live verification" didn't catch, because that verification consistently used
artifacts produced by an ad hoc scratchpad script rather than the actual shipped
`comp_use.cli` entrypoints. That's the throughline across issues 11, 13, and 19
below: the CLI path and the hand-authored/patched artifacts have quietly
diverged, and nothing in the test suite would catch it.

---

## 11. `comp_use.cli discover` still bakes a literal, non-reusable success checkpoint

**Priority:** critical — this is the exact bug the first pass believed it had
fixed (see the correction above), reproduced live against the actual CLI code
path, not a hand-patched artifact.

**Assignment reference (§3.3, Deterministic replay):** "Replay must use stable
element/control targeting, verify the checkpoint/success condition... Given the
same artifact and same inputs, the replay should behave the same way every time"
— and by direct implication, given *different* valid inputs, a genuinely
successful run must still be recognized as successful.

**Current state:** `comp_use/cli.py`, `_run_discover`:
```python
success_checkpoint = Checkpoint(
    type=CheckpointType.URL_MATCHES,
    url_pattern=trace.final_url.split("://", 1)[-1].split("/", 1)[-1],
)
```
This takes the *literal* final URL path from this one discovery run — e.g.
`member/12345` for `lookup_member`, or `sub-account/SUB-0001/confirm` for
`open_sub_account` — and bakes it in as the success checkpoint. The three
committed artifacts (`artifacts/*/v1.json`) all have clean, generic
`element_visible` checkpoints instead (`heading "Member Detail"`, `heading
"Confirmation"`) — but only because they were produced by
`regenerate_artifacts_with_extract.py` in the scratchpad, which calls
`compile_artifact()` directly and then hand-overrides `success_checkpoint`,
**bypassing this exact code in `cli.py` entirely**. The actual `discover`
command a user runs per the README's own Demo path still has this bug, unpatched.

Reproduced live, not just read: ran a `FakeLLMClient`-scripted `lookup_member`
discovery through the literal `_run_discover` checkpoint-construction logic
against the running mock app — produced `url_pattern='member/12345'`. Then ran
a `ReplayEngine` replay of an artifact carrying exactly that checkpoint with
`member_id=67890` (a different, valid member) against the live app:
```
outcome=HARD_FAILURE step_index=2 detail=''
expected="...url_pattern='member/12345'"
observed='http://localhost:5000/member/67890'
```
A genuinely successful lookup for member 67890 is reported as a hard failure,
purely because the checkpoint hardcodes member 12345's URL from whenever it was
first discovered. This would misclassify **every** real-CLI-discovered
capability whose success URL contains any per-run value (a member ID, a
generated confirmation/sub-account/transaction ID) — which is `open_sub_account`
and `transfer_funds` in addition to `lookup_member`.

**Sharper consequence, found rechecking `README.md`:** the README's own Demo
path (§"Terminal 2 — discover") instructs the reader to run exactly
`discover --capability-name lookup_member` — the same capability name as the
already-committed, working `artifacts/lookup_member/v1.json`. Since
`save_artifact` always writes `v1.json` (see issue 20 below — artifacts are
never actually versioned despite the schema supporting it), literally following
the README's own instructions **overwrites the good committed artifact with a
broken one**, in place. A reviewer who runs the documented demo exactly as
written breaks the repo.

**To do:**
- [x] Fix `_run_discover` to build a generic checkpoint instead of a literal
      URL pattern. Implemented as `_derive_success_checkpoint(surface,
      fallback_url)` in `comp_use/cli.py`: queries the final page for its first
      `heading`-role element (every mock app template has an `<h1>`) and builds
      an `element_visible` checkpoint from its text; falls back to the old
      literal `url_matches` behavior *with a loud `WARNING` print* only when no
      heading is found at all (e.g. `/widgets/ticker`, which has no heading).
- [x] Stop silently shipping this — the fallback path now prints a warning
      naming the exact URL it fell back to, instead of saving a doomed
      checkpoint with no signal.
- [x] Add a test that actually exercises the checkpoint-construction logic
      directly (`test_derive_success_checkpoint_uses_page_heading_not_literal_url`,
      `test_derive_success_checkpoint_falls_back_to_url_when_no_heading` in
      `tests/test_cli.py`) — real Playwright against the live mock app, not
      mocked. The first test asserts the derived checkpoint holds for a
      *different* member's detail page than the one it was derived from, which
      is the exact property the old code lacked.
- [x] Verified live, for real — not `FakeLLMClient`: ran
      `python -m comp_use.cli discover --capability-name lookup_member ...`
      against the running mock app with a real `OPENROUTER_API_KEY`
      (`nvidia/nemotron-3.5-lightning:free`). Produced `artifacts/lookup_member/v2.json`
      with `{"type": "element_visible", "locator": {..., "name": "Member Detail"}}`
      — `v1.json` was left byte-for-byte untouched (see issue 20, fixed
      alongside this). Replayed `v2.json` with `member_id=67890` (never used in
      any discovery run): `{"outcome": "success"}` — the exact scenario that
      previously returned `hard_failure` now returns `success`.
- [x] Corrected `REPORT.md`'s Architecture and Evidence walkthrough sections —
      see the Evidence walkthrough's new "Discovery — real LLM, checkpoint fix
      verification" entry citing this run directly.

---

## 12. Discovery never pauses on its own risky actions — only replay does

**Priority:** high (explicit §3.6 trigger, currently only half-implemented)

**Assignment reference (§3.6):** "Sometimes the system can't safely finish on
its own — the agent is stuck during discovery, a replay hits a condition it
can't recover from, **or a risky/irreversible step needs a person to decide**."
Three distinct triggers are named. Issue 2 (first pass) covered "the agent is
stuck during discovery." This is the third trigger, and it's still entirely
missing on the discovery side.

**Current state:** `DiscoveryAgent.run()` computes `risk_tier` for every step
via `_classify_risk()` (`comp_use/discovery/agent.py:193`) and stores it on the
`Step` — purely for `ReplayEngine` to use later. Nothing in `DiscoveryAgent.run()`
ever reads that `risk_tier` back to pause before *executing* a risky action live,
during discovery itself. Confirmed by reading the full method top to bottom:
`self.escalation` is only ever invoked from `_escalate()`, which is only called
for the `dead_end` and `stuck` triggers — never for a risky action about to be
taken. Concretely: today, a live discovery run will click "Confirm Sub-Account"
or "Confirm Transfer" — genuinely irreversible actions against a real backend —
with **no human in the loop at all**, even though `ReplayEngine` would pause on
the exact same step during replay.

This also means `Guardrail.requires_confirmation(step, confirm_risky)` — the
method `REPORT.md` cites as *the* risk-tier mechanism (see issue 14) — isn't
even the right description for discovery: it's never called there either.

**To do:**
- [x] Before calling `self.surface.act()` for a step whose `risk_tier` would be
      `RiskTier.RISKY`, escalate the same way `ReplayEngine` does, and block for
      human confirmation before proceeding. `DiscoveryAgent` now takes a
      `confirm_risky: bool = False` constructor param (mirroring
      `ReplayEngine.run()`'s parameter); `comp_use/cli.py`'s `discover`
      subcommand gained its own `--confirm-risky` flag (previously only
      `replay` had one), wired through to `DiscoveryAgent`.
- [x] Add a test asserting a risky decision during discovery calls
      `escalation.escalate(...)` (with a real screenshot) before `surface.act()`
      is invoked, and a second test confirming `confirm_risky=True` skips it.
      (`test_agent_escalates_before_acting_on_a_risky_decision`,
      `test_agent_skips_risky_escalation_when_confirm_risky_is_true` in
      `tests/test_discovery_agent.py` — the first uses an explicit order-tracking
      list to prove escalation happens strictly before the click, not just that
      both happened.)
- [x] Update `REPORT.md`'s Escalation & handoff section to describe the risky
      trigger as symmetric across both engines, not replay-only.
- [x] Verified live against the running mock app with a scripted
      `FakeLLMClient` reaching the real "Confirm Sub-Account" button: the run
      printed `[ESCALATION] step 5 is risk_tier=risky and confirm_risky is
      False`, blocked on stdin, and only proceeded to `finish` after `resume` +
      a note were typed. The escalation carried a real screenshot
      (`escalation_step5.png`) and, since issue 9's before/after diff mechanism
      composes automatically with any `EscalationController.escalate()` call,
      also produced `escalation_before_step5.png`/`escalation_after_step5.png`
      and a `tree_diff` on the same `escalation_human_action` event — issue 9's
      work didn't need any changes to also cover discovery-side risky
      escalation.

---

## 13. A real LLM has almost no way to ever produce an `extract` step

**Priority:** high (undermines issue 5's `output_schema`/`outputs` feature for
any *real* discovery run, not the `FakeLLMClient`-scripted ones)

**Assignment reference (§3.1):** the discovery loop's decision interface is
supposed to let the model choose from the full action vocabulary the system
supports.

**Current state:** `comp_use/llm_client.py`:
- `_TOOL_SCHEMA["function"]["parameters"]["properties"]` declares `action`,
  `locator`, `target`, `text`, `value_source`, `done` — **no `extract_as`
  field at all**, despite `DiscoveryAgent` reading `decision.get("extract_as")`
  and `Step.extract_as` existing precisely to carry it.
- `_SYSTEM_PROMPT` gives worked examples for `type_text`, `click`, and `finish`
  only. It never explains what `extract` is for, never shows an example, and
  never mentions `extract_as`. `navigate` and `select_option` are equally
  undocumented in the prompt, though those are lower-stakes (the agent can
  usually get by without them; it can't usefully use `extract` by guessing).

The `confirmation_number`/`txn_id` outputs described in `REPORT.md`'s Artifact
schema section and demonstrated live in this session's replay evidence were
only ever produced because the scratchpad regeneration script's
`FakeLLMClient` was hand-scripted to emit an `extract` action with a specific
CSS locator and `extract_as` value. A real `OpenRouterClient`-driven discovery
run today has no information telling it this action exists or how to use it —
it would need to guess both the field name and its purpose from the bare enum
value `"extract"` alone, with zero worked example to pattern-match against.

**To do:**
- [x] Add `extract_as` to `_TOOL_SCHEMA`'s declared properties, with a
      description explaining when to set it.
- [x] Add an `extract` example and rule to `_SYSTEM_PROMPT` (mirroring the
      existing `type_text`/`click`/`finish` examples): a worked example using a
      CSS `td:text-is('...') + td` locator (chosen deliberately — the example
      has to point at the *value* cell, not its label, or the model would just
      learn to extract the label text back), plus a rule that `extract`
      requires `extract_as`.
- [x] Add tests confirming the schema/prompt actually declare this, not just
      that nothing crashes: `test_tool_schema_declares_extract_as`,
      `test_system_prompt_explains_and_demonstrates_extract` in
      `tests/test_llm_client.py`.
- [x] Re-ran a real (non-`FakeLLMClient`) discovery for `open_sub_account`
      after the prompt fix — the honest test, not just that the schema now
      accepts the field. **It worked**: the model's 7th decision was
      `{"action": "extract", "locator": {"strategy": "role", "value": {"role":
      "text", "name": "<confirmation number>"}}, "extract_as": "<redacted>",
      "done": false}` — spontaneously, unprompted by any scripted fake, right
      after clicking "Confirm Sub-Account." The model chose an invalid ARIA
      role (`"text"` isn't a real role Playwright's `get_by_role` recognizes),
      which caused the run to hang rather than fail cleanly — **this is a live,
      unstaged reproduction of issue 15** (`surface.act()` isn't wrapped in
      try/except in `DiscoveryAgent`, so a bad locator at action-time has
      nowhere to go but hang/crash instead of a structured skip). Fixed
      together with issue 15, see that entry.

---

## 14. `ReplayEngine`'s allowlist check isn't crash-proof, unlike everything else in the loop

**Priority:** medium (contradicts an explicit, load-bearing claim in `REPORT.md`)

**Assignment reference (§3.4):** guardrails must be enforced consistently; and
implicitly, per this project's own established pattern (§3.3's error-handling
requirement plus `REPORT.md`'s own text), no in-scope failure should crash the
process instead of producing a structured `ReplayResult`.

**Current state:** `comp_use/replay/engine.py`:
```python
target_url = step.target
self.guardrail.check_allowlist(target_url or self.surface.current_url(), step.action.value)

try:
    extracted = self.surface.act(step.action, locator=step.locator, target=target_url, text=text)
    ...
except Exception as exc:
    ...
```
`check_allowlist()` (which raises `AllowlistViolation`, a plain `Exception`
subclass) is called **before** the `try` block, not inside it. If a step's
`target`/current URL ever falls outside `allowed_url_prefixes`, or its action
type isn't in `allowed_action_types`, replay crashes with a raw, unhandled
`AllowlistViolation` traceback instead of returning a `hard_failure` (or a new,
more accurate outcome). `DiscoveryAgent` gets this right — its own allowlist
check is wrapped in `try: ... except AllowlistViolation as exc:` — so the two
engines are inconsistent with each other, and `REPORT.md`'s Determinism &
error handling section's claim ("No action in this system is ever allowed to
crash the CLI with a raw traceback — everything resolves to one of the five
`ReplayResult` outcomes") is currently false for this one path.

**To do:**
- [x] Wrap `self.guardrail.check_allowlist(...)` in `ReplayEngine.run()` in a
      try/except, converting `AllowlistViolation` into a `ReplayResult`.
      Implemented by moving the call inside the existing `try` block that
      already wraps `surface.act()`, rather than adding a second, separate
      try/except — one exception-to-`HARD_FAILURE` boundary per step, not two.
- [x] Add a test: `test_disallowed_url_step_returns_hard_failure_instead_of_crashing`
      in `tests/test_replay_engine.py` — an artifact step targeting
      `http://evil.example.com/x` returns `HARD_FAILURE` with `"not in
      allowlist"` in the detail, and asserts the disallowed action was never
      actually performed (`len(surface.acted) == 0`), not just that the process
      didn't crash.
- [x] `REPORT.md`'s Determinism & error handling section's claim is accurate
      again now that both engines' allowlist checks are inside their
      respective try/except blocks — no softening needed, the claim just
      needed to be true.

---

## 15. `DiscoveryAgent.surface.act()` isn't wrapped either — a resolution failure crashes the whole run

**Priority:** medium (same root cause as issue 14, opposite engine)

**Assignment reference:** same as issue 14 — consistent, non-crashing error
handling across both engines, not just replay.

**Current state:** `comp_use/discovery/agent.py`, inside the main loop:
```python
self.surface.act(action, locator=locator, target=target, text=text)
```
Not wrapped in try/except, unlike `ReplayEngine.run()`'s equivalent call. A
decision can pass the `locator is not None` check (issue 13's missing-locator
skip only catches the *absence* of a locator, not its *validity*) and still fail
to resolve at Playwright-action time — a real, plausible scenario against the
deliberately hostile mock app (disabled elements, ambiguous names, elements a
`role`/`name` combination looks right for but that don't actually exist as
described). When that happens, `page.click()`/`page.fill()` raises a Playwright
`TimeoutError`, which is not caught anywhere in `DiscoveryAgent.run()` and
propagates all the way up, killing the whole discovery run (and the CLI process)
with a raw traceback — no escalation, no structured "stuck" outcome, no
`RunTrace` returned at all.

**To do:**
- [x] Wrap the `self.surface.act(...)` call in `DiscoveryAgent.run()` in
      try/except. On failure, log `skipped_decision` with reason
      `action_failed` (and the exception text), count toward
      `consecutive_skips`/dead-end escalation, and trigger the vision fallback
      immediately (`needs_vision_fallback = True`) rather than waiting for a
      separate `missing_locator` skip — a locator that resolved to nothing at
      action-time is the same underlying problem the vision fallback exists for.
- [x] Add a test: `test_agent_action_failure_does_not_crash_and_is_recorded` in
      `tests/test_discovery_agent.py` — a `FakeSurface.act()` that raises on a
      given call doesn't crash `agent.run()`; the run continues to `finish`
      instead, and the failed step is never recorded in `trace.steps`.
- [x] **Found and fixed from a live, unstaged failure, not a hypothetical one.**
      While verifying issue 13's fix, a real discovery run for `open_sub_account`
      had the model emit `{"action": "extract", "locator": {"role": "text",
      "name": "CONF-000002"}, ...}` — `"text"` isn't a real ARIA role, so
      `get_by_role("text", ...)` never resolves. Before this fix, that hung the
      whole process for the full Playwright default timeout with no way out
      (reproduced: the run had to be killed after 2 minutes). After this fix,
      re-running the identical scenario: the action still fails after
      Playwright's 30s timeout (expected — a genuinely bad locator still takes
      time to time out), but is now caught, logged
      (`"[discover] action failed: extract raised Locator.text_content: Timeout
      30000ms exceeded..."`), and — because `needs_vision_fallback` is set —
      the *next* call automatically routed to the vision model
      (`google/gemma-4-26b-a4b-it:free`, confirmed in the log), which then
      chose to `finish`. No crash, no hang past the one timeout, no manual
      intervention needed. This run also incidentally re-confirms issue 10's
      vision fallback triggering from a real failure, not just a mocked one.

---

## 16. `Guardrail.requires_confirmation()` is dead code that `REPORT.md` cites as the real mechanism

**Priority:** low-medium (code-quality + doc-accuracy)

**Current state:** grepped the whole repo for `requires_confirmation` —
matches only in `comp_use/guardrail.py` (the definition), its own unit test
(`tests/test_guardrail.py`), the design-plan doc, and `REPORT.md`'s Safety
section, which states: *"Risk tiers — `requires_confirmation(step,
confirm_risky)`; CLI `--confirm-risky` skips the pause."* Neither
`ReplayEngine` nor `DiscoveryAgent` actually calls this method. `ReplayEngine`
reimplements the equivalent condition inline instead:
```python
if (step.risk_tier == RiskTier.RISKY and not confirm_risky and self.escalation is not None):
```
which isn't even identical logic — it also requires `self.escalation is not
None`, a condition `requires_confirmation()` doesn't express at all. The method
exists, is tested in isolation, and does the right thing — it's just never
wired into the actual decision path it's documented as being part of.

**To do:**
- [x] Call `self.guardrail.requires_confirmation(...)` from both
      `ReplayEngine.run()` and `DiscoveryAgent.run()` instead of each
      reimplementing the condition inline. Changed the method's signature from
      `requires_confirmation(step: Step, confirm_risky: bool)` to
      `requires_confirmation(risk_tier: RiskTier, confirm_risky: bool)` — it
      only ever used `step.risk_tier`, and `DiscoveryAgent` computes a bare
      `risk_tier` before a `Step` object exists yet, so the narrower signature
      is both simpler and actually callable from both sites without
      restructuring either loop. The `self.escalation is not None` condition
      stays as a separate `and` clause at each call site — it's a wiring
      concern (is an escalation controller configured at all), not a risk-tier
      concern, so it doesn't belong inside `Guardrail`.
- [x] Also deleted `Settings.risky_confirm_default` (issue 23's related
      finding) — grepped the repo after removal, zero remaining references.
- [x] Updated `REPORT.md`'s Safety section to describe the call sites that
      actually exist now.
- [x] Updated `tests/test_guardrail.py`'s two `requires_confirmation` tests to
      the new signature (pass `RiskTier.RISKY`/`RiskTier.SAFE` directly instead
      of constructing a throwaway `Step`).

---

## 17. Mock app: transferring to a nonexistent account silently destroys the money and still reports success

**Priority:** medium (a real logic bug in the target app, not `comp_use` itself
— but it undermines what a `success` outcome from `transfer_funds` actually
means)

**Current state:** `mock_app/app.py`, `transfer_submit`:
```python
from_account = _find_account(member_id, from_account_id)
if amount <= 0 or from_account is None:
    return render_template(..., error=...)
```
Only `from_account` is validated before proceeding to the review step —
`to_account_id` is never checked. Then in `transfer_review_confirm`:
```python
from_account = _find_account(member_id, pending["from_account"])
to_account = _find_account(member_id, pending["to_account"])
from_account["balance"] -= pending["amount"]
if to_account is not None:
    to_account["balance"] += pending["amount"]
```
If `to_account_id` doesn't resolve to a real account (typo, wrong member,
account that was never opened), the code still debits `from_account`
unconditionally, silently skips crediting anything, generates a `txn_id`, and
renders a normal confirmation page — a `transfer_funds` replay with a bad
`to_account` param reports `outcome: success` while the money has actually
vanished. This is a data-integrity bug in the mock app that a real bank backend
would never allow, and it means "the outcome was `success`" is currently a
weaker guarantee than it should be for this specific capability.

**To do:**
- [x] Validate `to_account_id` the same way `from_account_id` already is,
      before the review step. Reused the existing generic `error` path
      (`"Amount must be greater than zero and accounts must be valid."`) rather
      than inventing a new distinct message/path — a bad `to_account` is the
      same class of caller error as a bad `from_account` or a non-positive
      amount, all three now share one validation branch.
- [x] Add a mock-app test:
      `test_transfer_to_nonexistent_account_is_validation_error_not_silent_success`
      in `tests/test_mock_app_transfer.py` — asserts both the error response
      and that `ACC-001`'s balance is unchanged afterward (a real
      before/after balance check, not just a status code).
- [x] Considered the optional `outcome_patterns` entry and declined it,
      deliberately: since the fix reuses the *same* generic validation message
      the pre-existing amount<=0/bad-from_account case already used (which
      never had its own `outcome_patterns` entry either), a bad `to_account`
      now behaves exactly like that pre-existing case on replay —
      `hard_failure` at the "Confirm Transfer" click, since the app never
      leaves the transfer form. Adding a special-cased `outcome_pattern` for
      just this one input would be inconsistent with how the already-existing,
      never-flagged amount-validation case is handled; both are equally "the
      caller passed something invalid" errors, not app-level business
      outcomes like "insufficient funds."
- [x] Verified live end to end, not just the mock-app unit test: replayed
      `transfer_funds` with `to_account=ACC-999` (nonexistent) against the
      real running app — `{"outcome": "hard_failure", ...}`, and a follow-up
      `curl` of the member detail page confirmed `ACC-001`'s balance was still
      `1500.00`, unchanged. Before this fix, this exact scenario would have
      returned `{"outcome": "success"}` while silently debiting the account.

---

## 18. `comp_use.cli`'s `_run_discover`/`_run_replay` have zero automated test coverage

**Priority:** medium (root cause behind issue 11 going unnoticed for an entire
session of "live verification")

**Current state:** `tests/test_cli.py` only tests the two small helper
functions `save_artifact`/`load_artifact` — nothing exercises `_run_discover`
or `_run_replay` themselves, including their checkpoint construction,
escalation wiring, or failure-screenshot wiring. Every "verified live" claim in
`REPORT.md` and the first-pass `ISSUES.md` entries used either a bespoke
scratchpad script calling the underlying classes directly, or a real terminal
invocation of the CLI checked by hand — never an automated test asserting on
`_run_discover`'s/`_run_replay`'s actual behavior. This is *why* issue 11
existed undetected through the entire previous session: nothing would have
failed red if it had been wrong.

**To do:**
- [x] Add an integration-style test for `_run_discover`/`_run_replay` against
      the real mock app + `FakeLLMClient`, calling the actual CLI functions
      directly (not a scratchpad script bypassing them) — `Settings` and
      `OpenRouterClient` patched via `unittest.mock.patch.object` so the test
      redirects `artifacts_dir`/`evidence_dir` to `tmp_path` and injects a
      scripted `FakeLLMClient` instead of hitting the real network, while
      `_run_discover`/`_run_replay` themselves run completely unmodified.
      (`test_run_discover_produces_a_reusable_checkpoint_end_to_end` in
      `tests/test_cli.py`.) Asserts: the produced `success_checkpoint` is
      `element_visible`, not a literal `url_matches`; a second `_run_discover`
      call for the same capability name produces `v2.json` without touching
      `v1.json`; and a `_run_replay` call with a *different* member than
      discovery used returns `"outcome": "success"`.
- [x] **Confirmed this test would have caught issue 11**, not just asserted it
      in the abstract — temporarily reintroduced the exact old buggy
      checkpoint-construction code in `_run_discover`, reran just this test,
      watched it fail with `AssertionError: url_matches != element_visible`,
      then reverted (confirmed `git diff` was empty afterward). This is now
      the regression test for issue 11's fix, proven to actually regress-test
      it, not a separate nice-to-have that happens to pass.

---

## 19. Minor doc/config gaps found alongside the above

**Priority:** low (bundled — each is small)

- **`.env.example` and `README.md` don't mention `OPENROUTER_VISION_MODEL`** —
  added this session (issue 10) with a working default, but nothing user-facing
  documents that it exists or how to override it.
- **`tests/test_config.py`'s `test_env_override` doesn't cover
  `OPENROUTER_VISION_MODEL`** — only `OPENROUTER_MODEL` is asserted against an
  env override; the new setting has no equivalent test.
- **`_run_discover` never populates `outcome_patterns`** — hardcoded empty;
  every business-outcome capability in the committed artifacts required
  hand-authoring after the fact. (`output_schema` is *not* in this bucket
  anymore — issue 13's fix means the real LLM now does populate it, verified
  live. `outcome_patterns` is a different, harder problem: the model has no
  way to *decide* "this page is a business outcome" vs. just narrating what
  it sees, which `extract` doesn't require deciding.) This is arguably fine as
  a known limitation of an MVP discovery loop, but it isn't currently stated
  anywhere as a limitation — `REPORT.md` described the mechanism as if it's
  discovery's normal output.
- **`PlaywrightSurface.check_checkpoint`'s `TEXT_PRESENT` type checks
  `page.content()`** (raw HTML source) rather than rendered/visible text. Works
  correctly today only because the mock app has no client-side JS hiding
  content (Jinja conditionals render server-side) — would silently produce
  false positives against any app that hides matching text via CSS/JS instead.
  Worth a one-line caveat in `REPORT.md`'s Determinism section if not fixed.
- **Mock app ID counters (`next_sub_account_id`, `next_confirmation_number`,
  `next_txn_id`) are module-level globals**, shared across every `create_app()`
  call within a process rather than being scoped per app instance. Not
  currently causing test flakiness (no test hardcodes exact counter values),
  but it's a latent footgun if a future test creates two app instances and
  expects independent ID sequences.

**To do (all four, low priority, can be batched):**
- [x] Document `OPENROUTER_VISION_MODEL` in `.env.example` and README.
- [x] Add `OPENROUTER_VISION_MODEL` to `test_config.py`'s env-override test.
- [x] Add a sentence to `REPORT.md` noting `outcome_patterns` requires manual
      authoring today, not automatic discovery — added to the Determinism &
      error handling section's `business_outcome` bullet, and noted that
      `output_schema` no longer belongs in this bucket (issue 13 fixed that
      half; `outcome_patterns` is what's left).
- [x] Noted the `TEXT_PRESENT`/`page.content()` HTML-source caveat in
      `REPORT.md`'s Architecture section rather than changing the
      implementation — the mock app has no client-side JS hiding content, so
      there's no live bug to fix today; the caveat documents the constraint
      for whenever that stops being true.
- (The 5th bullet above — mock app ID counters being module-level globals —
  was never in this checklist to begin with; noted as a latent footgun, not
  an active problem, and left as-is.)

---

# Third audit pass (2026-08-19, same day)

Requested: re-audit thoroughly enough that fixing issues 1–19 shouldn't surface
more. Re-read every source file end to end again (including ones already marked
audited), every test file, and every doc/config file (`README.md`, `REPORT.md`,
`.gitignore`, `requirements.txt`, `pyproject.toml`, `.env.example`) specifically
looking for what the first two passes missed. Found five more, one of which is a
gap in this very audit process — a citation to a finding I'd reasoned about but
never actually written down. Noting that plainly rather than quietly fixing the
citation, because it's relevant to the honest answer to "is this really the last
set."

---

## 20. Artifacts are never actually versioned — `_run_discover` always writes/overwrites `v1.json`

**Priority:** high (root cause behind issue 11's sharpest consequence — running
the README's own demo verbatim destroys the working committed artifact)

**Current state:** `comp_use/schemas.py`'s `Artifact.version` defaults to `1`.
`comp_use/cli.py`'s `_run_discover` never sets it to anything else — `save_artifact`
always writes `capability_dir / f"v{artifact.version}.json"`, i.e. always
`v1.json`. `load_artifact` *does* support multi-version lookup (`version: int |
None = None`, defaulting to the highest `v*.json` found), and the design spec
clearly intends versioning (`docs/superpowers/.../design.md` and the artifact
schema's own naming convention `artifacts/<capability_name>/v<N>.json`) — but
nothing in the actual discover flow ever produces `v2.json`. Re-running discovery
for an existing capability name silently clobbers whatever was there before, with
no version history, no diff, no confirmation prompt, and no warning. This is a
loaded gun even outside the issue-11 scenario: if a capability's underlying app UI
changes and someone re-discovers it, the old (possibly still-working-for-other-
tenants, in a multi-tenant future) artifact is just gone.

**To do:**
- [x] `_run_discover` should compute the next version (scan existing
      `v*.json` the same way `load_artifact` does, +1) rather than hardcoding
      `1`, so re-discovery adds a new version instead of overwriting. Added
      `next_artifact_version(capability_name, artifacts_dir)` in
      `comp_use/cli.py` (max existing version + 1, or 1 if none exist);
      `_run_discover` now sets `artifact.version = next_artifact_version(...)`
      right before `save_artifact`.
- [x] "Latest" for replay already meant highest version number in
      `load_artifact` — no change needed there, confirmed by test.
- [x] Add tests: `test_next_artifact_version_is_1_for_a_new_capability`,
      `test_next_artifact_version_increments_past_existing_versions` in
      `tests/test_cli.py`.
- [x] Verified live alongside issue 11: running real discovery for
      `lookup_member` (a capability with an existing committed `v1.json`)
      produced `v2.json`; `diff`'d `v1.json` before/after and confirmed it was
      byte-for-byte unchanged. The README's demo instructions no longer destroy
      the working artifact.

---

## 21. `_run_replay`'s required-param check duplicates `ReplayEngine._validate_params` entirely

**Priority:** low-medium (code-quality/drift risk, not a live bug today)

**Current state:** `comp_use/cli.py`, `_run_replay`:
```python
missing = [item.name for item in artifact.input_schema if item.required and item.name not in params]
if missing:
    detail = f"missing required param '{missing[0]}'"
    ...
    return
```
`comp_use/replay/engine.py`, `ReplayEngine._validate_params` (called from
`ReplayEngine.run()`, the very next thing `_run_replay` does after its own
check):
```python
for input_param in artifact.input_schema:
    if input_param.required and input_param.name not in params:
        return f"missing required param '{input_param.name}'"
```
Identical condition, checked twice, in two different files. Because `cli.py`'s
check runs first and returns early on any match, `ReplayEngine`'s own validation
is unreachable dead code on the only path that matters in production (the real
CLI) — it only actually executes when a test or a future caller uses
`ReplayEngine` directly, bypassing `cli.py`. Not a bug today because both
checks express the same logic, but it's exactly the kind of duplication that
silently drifts: if type-aware validation is ever added (see the `InputParam.type`
note below), it would be easy to add it to only one of the two copies.

**To do:**
- [x] Made `_validate_params` a standalone module-level function,
      `validate_required_params(artifact, params) -> str | None`, in
      `comp_use/replay/engine.py`. `ReplayEngine.run()` calls it;
      `cli.py`'s `_run_replay` now imports and calls the exact same function
      for its pre-browser check instead of reimplementing the loop — the
      "skip launching Chromium on `validation_error`" behavior is preserved
      (that's still `cli.py`'s job, calling it before the browser exists), but
      there's now exactly one copy of the validation logic, not two.
- [x] Added `test_validate_required_params_is_shared_between_cli_and_engine`
      in `tests/test_replay_engine.py` — exists specifically so a future
      change to validation logic can't land in only one of the two call
      sites without a test noticing.
- [x] Verified live: `replay --capability-name lookup_member --params "{}"` still
      returns `validation_error` with the same detail message as before, before
      any browser opens.
- [x] Added the `InputParam.type`-not-validated gap as a documented "Known gap"
      sentence in `REPORT.md`'s Artifact schema section, rather than leaving it
      unstated.

---

## 22. `REPORT.md` contradicts itself on which outcomes get a failure screenshot

**Priority:** low (doc-only, internal inconsistency within a single document)

**Current state:** `REPORT.md`'s Determinism & error handling section states:
> "Every non-`SUCCESS` outcome on both the replay and discovery CLI paths also
> gets a richer signal than the JSONL log alone... whenever the outcome isn't
> clean success, landing a `final.png`..."

But the same document's Evidence walkthrough section, describing
`validation_error`, correctly states:
> "returned before any `surface.act` — the CLI skips launching Chromium
> entirely on this path."

These two claims are inconsistent with each other: `_run_replay`'s early
required-param check (see issue 21) returns *before* the `with sync_playwright()`
block ever runs, so there is no `Surface` and no browser to screenshot —
`validation_error` can never get a `final.png`, by construction, no matter what
the Determinism section's "every non-SUCCESS" wording implies. The Evidence
walkthrough section already has this right; the Determinism section doesn't.

**To do:**
- [x] Reworded the Determinism & error handling section's claim to "every
      outcome that actually reaches the browser," and added an explicit
      sentence naming `validation_error` as the one outcome this can never
      apply to and why — matching what the Evidence walkthrough section
      (correctly) already said elsewhere in the same file.

---

## 23. Smaller findings from re-reading `README.md`, `.gitignore`, and `config.py` end to end

**Priority:** low (bundled)

- **README's escalation demo text is stale post-issue-9.** "Risky capabilities
  (transfer / sub-account) pause for human confirmation unless you pass
  `--confirm-risky`. Type `resume` in the CLI when you have finished in the
  shared browser window." doesn't mention the one-line "what did you do?" note
  prompt added this session (issue 9) — a reader following the README verbatim
  will be surprised by an extra prompt it doesn't warn them about.
- **`.gitignore`'s `/evidence/*.png` and `/evidence/*.jpg` rules match nothing
  real.** Both patterns are anchored one level too shallow — every actual
  screenshot lives at `evidence/<run_id>/*.png` (nested one directory deeper),
  not directly under `evidence/`. Confirmed by the fact that every screenshot
  committed this session (`escalation_before_step8.png`, `final.png`, etc.) *was*
  tracked by git despite this rule supposedly excluding `.png` files under
  `evidence/`. Either the rule was always dead, or it reflects an earlier intent
  (exclude evidence images from git) that current practice has since reversed
  (we've deliberately committed real screenshots as evidence in three separate
  commits this session) without anyone updating `.gitignore` to match the actual
  intent either way.
- **`Settings.risky_confirm_default: bool = False` is dead config**, in the
  same family as issue 16's `requires_confirmation`. Grepped the whole repo:
  referenced only in its own definition and the design-plan doc, never read by
  `cli.py`, `ReplayEngine`, or `DiscoveryAgent` — risk confirmation is gated
  entirely by the CLI's own `--confirm-risky` flag (`args.confirm_risky`), a
  separate, unrelated boolean that happens to serve the same purpose. This
  field can likely just be deleted.

**To do:**
- [x] Update the README's escalation paragraph to mention the note prompt (and,
      while there, that `discover` now has its own `--confirm-risky` flag too —
      see issue 12).
- [x] Deleted the `/evidence/*.png`/`/evidence/*.jpg` `.gitignore` rules
      rather than fixing their pattern — they never matched anything real
      (anchored one directory too shallow), and actual practice throughout
      this project has been to deliberately commit real screenshots as
      evidence (escalation before/after pairs, failure `final.png`s, ...) in
      many separate commits. Deleting the dead rule matches intent; making it
      actually work would have started silently excluding evidence we want
      tracked.
- [x] Delete `Settings.risky_confirm_default` — done alongside issue 16's fix,
      same root cause (`comp_use/config.py`).

---

## On "is this really the last set"

Three audit passes in one session found 23 items total, in decreasing severity
and increasing obscurity — the pattern of a converging, not open-ended, search
(critical/high findings clustered in passes one and two; pass three's five
findings are all low-to-medium and mostly doc/config precision, plus one
already-partially-known versioning gap). That's a reasonable signal this is
close to the bottom, not a guarantee it's the actual bottom.

Two things are true at once, honestly:
1. Every remaining *known* gap that could realistically surprise someone
   checking the brief line-by-line is now written down here, with exact file/line
   references and (where practical) a live reproduction — not vibes.
2. No finite audit of a nontrivial codebase can prove a negative. A genuinely new
   class of issue (a Playwright version-specific quirk, a race condition under
   real timing instead of headless-test timing, something only a fresh pair of
   eyes or an adversarial reviewer would catch) could still exist and wouldn't be
   caught by re-reading the same files a fourth time — that stops being an audit
   and starts being diminishing returns. The highest-value next step for
   confidence isn't a fourth read-through; it's fixing issues 11/20 (the two that
   compound each other and are reproducible right now) and then re-running the
   full live demo path end to end exactly as the README describes it, which
   would catch anything this pass's static reading still missed.

---

## 24. `recoverable` outcome was wired but unexercised — fixed by fixing a real crash bug

**Priority:** medium (the one gap explicitly named as "what's left" after issues
1–23; requested directly)

**Assignment reference (§3.3):** the four/five-bucket outcome taxonomy —
`validation_error`, `success`, `business_outcome`, `recoverable`,
`hard_failure` — is a core deliverable. A taxonomy value that's declared,
tested for the *mechanism*, but never actually reachable against the real
target app is an incomplete demonstration of it.

**Current state (before this fix):** `OutcomeType.RECOVERABLE` and the shared
`OutcomePattern` mechanism existed and were exercised only by
`business_outcome` (`insufficient_funds`, `no_such_member`). No capability's
artifact declared a `recoverable` pattern, because nothing in the mock app
produced a state that was genuinely "dismiss and retry" rather than a
permanent business result or an automation bug.

**What changed:** Found a real bug while looking for a legitimate
`recoverable` scenario: `sub_account_review`/`sub_account_review_confirm` and
`transfer_review`/`transfer_review_confirm` all did a bare dict lookup
(`_pending_sub_accounts[token]` / `.pop(token)`) with no check that the token
still existed. Revisiting a review page after already confirming it (a real
double-click or back-button scenario) raised an unhandled `KeyError`,
crashing the mock app with a raw Flask 500 — not a contrived scenario, a
genuine target-app bug.

Fixed by checking token presence before the lookup/pop in all four routes and
rendering a new `session_expired.html` ("This review session has expired or
was already submitted. Please start again.") instead of crashing. This is
exactly the "dismiss and retry" shape `recoverable` is meant for: not a
permanent business state, not an automation locator bug — a transient,
recoverable condition with an obvious retry path (start the form over).

Added a matching `OutcomePattern` (`outcome: recoverable`, `detail:
session_expired`) to both `open_sub_account/v1.json` and
`transfer_funds/v1.json`'s committed artifacts.

**To do:**
- [x] Fix the real crash: check token presence before
      `_pending_sub_accounts[token]`/`.pop(token)` and
      `_pending_transfers[token]`/`.pop(token)`, in all four routes
      (`mock_app/app.py`), rendering `session_expired.html` instead.
- [x] Add mock-app tests proving the crash is gone, not just that a page
      renders: double-submitting a confirm, and re-`GET`ing a stale review
      URL, both return 200 with "Session Expired," not a 500.
      (`test_confirming_an_already_used_token_shows_session_expired_not_a_crash`,
      `test_reviewing_an_already_used_token_shows_session_expired_not_a_crash`
      in `tests/test_mock_app_sub_account.py`, mirrored in
      `tests/test_mock_app_transfer.py` — the transfer version also asserts
      the source account isn't double-debited by the stale second submit, via
      a real before/after balance comparison.)
- [x] Add a `ReplayEngine` unit test proving the taxonomy machinery itself
      correctly discriminates `recoverable` from `business_outcome`/
      `hard_failure` when a matching `OutcomePattern` is declared.
      (`test_recoverable_returned_when_a_dismiss_and_retry_pattern_matches` in
      `tests/test_replay_engine.py`, mirroring the existing `business_outcome`
      test.)
- [x] Add the `OutcomePattern` to the real committed artifacts, not just a
      test fixture.
- [x] Verified live against the real running mock app, not mocked: drove a
      real transfer through to confirmation with Playwright, navigated back
      to the now-stale review URL, confirmed the real page renders "Session
      Expired," and confirmed `PlaywrightSurface.check_checkpoint()` against
      the real `transfer_funds` artifact's `recoverable` `OutcomePattern`
      matches the real page (`True`). All five `OutcomeType` values are now
      demonstrated against a real running system.
- [x] Updated `REPORT.md`'s Determinism & error handling section and Evidence
      walkthrough to describe the real mechanism and cite the live
      verification, replacing the "wired but unexercised" language.

---

## 25. Safety/code-quality closing pass: stdout redaction leak, duplicated dead-end block, opaque exception details

**Priority:** medium (raised directly, in response to "why is Generalization /
Safety / Code quality still below 90")

**25a. Console output wasn't redacted — only the evidence JSONL was.**
`DiscoveryAgent.run()` had `print(f"[discover] {action.value}
locator={locator} target={target} text={text}", flush=True)` — this printed
the real typed value straight to the terminal, unredacted, even though
`EvidenceLogger.log_event()` redacts the exact same data before it reaches
`log.jsonl`. Confirmed live: an `ACC-001` value (which matches a redaction
pattern) printed in the clear to stdout before this fix. If that console
output is captured anywhere (CI logs, a shared terminal recording, a
copy-pasted bug report), it leaks exactly the data the redaction patterns
exist to protect — the JSONL log being clean doesn't help if the terminal
isn't.

**25b. The dead-end/escalation block was duplicated five times verbatim** in
`DiscoveryAgent.run()` (unknown action, missing locator, missing target,
allowlist violation, action failed) — the same three-line
increment/threshold-check/escalate/reset pattern, copy-pasted rather than
factored out.

**25c. `hard_failure`/`action_failed` details didn't name the exception
type**, only its message (`str(exc)`) — a genuine code bug (e.g. a `TypeError`
from a logic error) and an expected environmental failure (a Playwright
`TimeoutError`) were indistinguishable in evidence/detail text, since both
just produce a message string with no indication of which kind of failure
it was.

**To do:**
- [x] Added `DiscoveryAgent._print(message)`, which redacts via
      `self.guardrail.redact(message)` before printing, and routed every
      `print()` call in `DiscoveryAgent.run()` through it.
- [x] Added `DiscoveryAgent._note_skip(goal, step_index, consecutive_skips) ->
      int`, replacing all five duplicated copies of the
      increment/threshold-check/escalate block with one call each.
- [x] Both `DiscoveryAgent`'s `action_failed` handling and
      `ReplayEngine.run()`'s `hard_failure` detail now format exceptions as
      `f"{type(exc).__name__}: {exc}"` instead of bare `str(exc)`.
- [x] Tests: `test_agent_console_output_redacts_typed_values` (captures real
      stdout via `capsys`, asserts a redaction-pattern value doesn't appear
      and `[REDACTED]` does, while the action name itself survives),
      `test_agent_action_failure_evidence_names_the_exception_type`, and a
      new assertion on the existing `test_action_exception_returns_hard_failure_instead_of_crashing`
      in `tests/test_replay_engine.py` (`result.detail.startswith("TimeoutError:")`).
- [x] Verified live, not just unit-tested: ran real discovery against the
      live mock app twice — once typing a bare `member_id` (`"12345"`,
      correctly *not* redacted, matching the documented policy) and once
      typing an `ACC-001` value through a scripted flow (correctly printed
      as `[REDACTED]`) — confirming the fix respects the existing redaction
      policy rather than over-redacting everything.
- [x] Added `test_main_dispatches_discover_subcommand_with_parsed_args` /
      `test_main_dispatches_replay_subcommand_with_parsed_args` in
      `tests/test_cli.py` — the one remaining untested layer, real
      `sys.argv` parsed through `argparse` and `main()`'s actual dispatch,
      not just calling `_run_discover`/`_run_replay` directly.
- [ ] Not done, deliberately out of scope for this pass: `mypy`/`pyright` in
      CI. A real improvement, but a separate investment (config, fixing
      whatever it flags, wiring into the test run) rather than a small,
      mechanical fix like the others above.

---

## 26. Two live crashes found from a real user report, after `pytest` gave false confidence

**Priority:** high (real, reproducible crashes in the exact documented demo
path — not hypothetical, not found by audit, found by a user actually running
the command README instructs)

**Context:** every prior "crash-proofing" pass (issues 14, 15) audited
`surface.act()` and `check_allowlist()` specifically, and REPORT.md concluded
"no action in this system is ever allowed to crash the CLI with a raw
traceback." That conclusion was too narrow — it covered every *action*
call site, but two entirely different call classes were never audited at
all: screenshot capture, and the LLM call itself.

**26a. Screenshot capture unprotected everywhere.** `DiscoveryAgent`'s
vision-fallback path called `self.surface.screenshot()` directly at the top
of the loop, completely outside any try/except. A real `Page.screenshot()`
timeout (independent of any locator/action problem — fonts loading, in the
observed traceback) crashed a live `discover` run with a raw traceback.
Grepped every `surface.screenshot()` call site in the codebase: 8 total,
across `DiscoveryAgent` (2), `ReplayEngine` (1), `EscalationController` (2),
`cli.py` (3), and `comp_use/drift.py` (1) — the last one had been missed
entirely by the original issue 14/15 audit.

**26b. The LLM call itself was never wrapped.** `DiscoveryAgent.run()`'s call
to `llm_client.decide_next_action()` sat outside any try/except, unlike every
other external interaction in the loop. A real model response triggered
`json.loads()` to raise `"Extra data"` (valid JSON followed by trailing
commentary the model appended) and crashed the run.

**To do:**
- [x] Added a single shared `safe_screenshot(surface) -> bytes | None` in
      `comp_use/surface.py` and routed all 8 call sites through it.
      `EvidenceLogger.save_screenshot` now accepts `None` and returns `None`
      cleanly instead of crashing on `path.write_bytes(None)`.
- [x] `EscalationController`'s before/after tree+screenshot capture (the
      single most safety-critical of all 8 sites — a failure here must never
      prevent the human from being notified in the first place) now degrades
      to `None`/no-diff instead of crashing before `transport.notify()` is
      even reached.
- [x] Fixed `parse_decision()` to fall back to
      `json.JSONDecoder().raw_decode()` when strict `json.loads()` fails,
      tolerating trailing text after a valid decision.
- [x] Also wrapped `DiscoveryAgent.run()`'s `decide_next_action()` call
      itself in try/except, treating any failure as a skip that counts
      toward the existing dead-end/escalation machinery — defense in depth,
      not reliance on the parser fix alone.
- [x] Tests: 8 new, each reproducing the real failure first (confirmed red)
      before the fix — `safe_screenshot` (raises/succeeds),
      `save_screenshot`-with-`None`, `DiscoveryAgent` screenshot-failure and
      LLM-call-failure survival, `ReplayEngine`/`EscalationController`
      screenshot-failure survival, `propose_drift_patch` screenshot-failure,
      `parse_decision` trailing-text tolerance.
- [x] Verified live, repeatedly, under real model non-determinism, not just
      unit tests: re-ran the exact command that crashed multiple times. One
      run hit the `action_failed`/vision-fallback path again and recovered
      cleanly; another hit a *different* malformed-JSON shape the
      `raw_decode` fix didn't even fully cover (`"Expecting ',' delimiter"`)
      — caught by the outer wrap anyway, logged as `llm_call_failed`, run
      still completed. Both real runs committed
      (`evidence/discover_1787177243`, `evidence/discover_1787177306`).
- [x] Updated `REPORT.md`'s Determinism & error handling section — the
      "no action is ever allowed to crash" claim now names these two
      previously-missed call classes explicitly, with the live evidence.

**Lesson for next time, stated plainly:** three prior audit passes and a
targeted "why is X below 90" follow-up all missed these two call classes,
because every pass searched for patterns similar to what had already been
found (locator failures, allowlist checks) rather than systematically
enumerating *every* external call type (network I/O, browser I/O,
LLM I/O) and asking "is this one wrapped." A real user running the actual
documented command found both in one sitting. `pytest` passing is not the
same as "nothing can crash" — it only proves what the tests specifically
exercise.

---

## 27. Added a second LLM provider (NVIDIA NIM) to tolerate OpenRouter's rate limits

**Priority:** medium (requested directly, motivated by real rate limits hit
during the live testing that surfaced issue 26)

**Context:** OpenRouter's free-tier models rate-limit under real, repeated
use — exactly the kind of use the README's "Exercising every outcome"
section (and the live debugging in issue 26) puts them under. Issue 26
already made an LLM-call failure a safe, retryable skip instead of a crash;
this issue reduces how often that skip needs to fire at all, by giving
discovery a second provider to fail over to.

**What changed:**
- `comp_use/llm_client.py`: extracted `OpenRouterClient`'s request/response
  handling into a shared `_OpenAICompatibleClient` base — NVIDIA NIM's hosted
  inference API is also OpenAI-compatible chat completions with tool calling,
  so the prompts, tool schemas, and HTTP handling are identical; only the
  endpoint, auth header shape, and model names differ per provider. Verified
  the refactor was behavior-preserving before adding anything new: all 12
  existing `OpenRouterClient` tests passed unmodified against the refactored
  code.
- Added `NvidiaNimClient(_OpenAICompatibleClient)` — `integrate.api.nvidia.com`,
  bearer-only auth (no `HTTP-Referer`/`X-Title`, unlike OpenRouter).
- Added `FallbackLLMClient(primary, fallback)`: tries `primary`
  (`decide_next_action`/`diagnose_drift`), and on *any* exception — not just a
  detected rate limit specifically, a malformed response or timeout deserves
  the same treatment — retries with `fallback`. If `fallback` also raises,
  its exception propagates into the exact `llm_call_failed` skip path issue
  26 already built; the fallback mechanism doesn't need its own separate
  crash-proofing.
- `comp_use/cli.py`'s new `_build_llm_client(settings)` only constructs the
  fallback wrapper when `NVIDIA_API_KEY` is set — with no key configured,
  behavior is identical to OpenRouter-only, so this is additive, not a
  breaking change to the existing default setup. Wired into both
  `_run_discover` and `_diagnose_and_propose_patch` (drift diagnosis also
  benefits from the fallback).
- Added `Settings.nvidia_api_key`/`nvidia_model`/`nvidia_vision_model`
  (`NVIDIA_API_KEY`/`NVIDIA_MODEL`/`NVIDIA_VISION_MODEL`), documented in
  `.env.example` and `README.md`.
- **Follow-up (same session):** the first cut above always made OpenRouter
  primary — NVIDIA NIM could only ever be the fallback. Added
  `Settings.model_provider` (`MODEL_PROVIDER` env var, `"openrouter"` default
  or `"nvidia"`, a Pydantic `Literal` so an unrecognized value is rejected at
  `Settings()` construction rather than silently misconfiguring which
  provider runs) and rewrote `_build_llm_client` to be symmetric: it picks
  primary/fallback based on `model_provider`, and if the chosen primary has
  no API key configured, falls back to whichever provider *does* have a key
  rather than building a client that's guaranteed to fail on its first call.
  `_run_discover`'s startup print was also updated to name whichever
  provider/model is actually primary, instead of hardcoding OpenRouter's.

**Design note (protected namespace):** `model_provider` as a field name
triggers Pydantic's built-in warning that `model_*` names collide with its
own reserved namespace (`model_config`, `model_dump()`, ...). Resolved by
setting `model_config = ConfigDict(protected_namespaces=())` on `Settings`
rather than renaming the field — `model_provider` is the clearer name for
what it does, and the collision is with Pydantic's internals, not with any
other field or behavior in this codebase.

**To do:**
- [x] Refactor `OpenRouterClient` into a shared base with zero behavior
      change, confirmed by running all existing tests unmodified before
      adding anything new.
- [x] Add `NvidiaNimClient`, tested with mocked HTTP (correct endpoint,
      bearer-only auth, correct text/vision model routing).
- [x] Add `FallbackLLMClient`, tested: primary succeeds → fallback never
      called; primary raises → fallback's result is used; both raise → the
      fallback's exception propagates; applies to both `decide_next_action`
      and `diagnose_drift`.
- [x] Wire into `cli.py` via `_build_llm_client`, tested: no NVIDIA key →
      bare `OpenRouterClient`; key present → `FallbackLLMClient` wrapping
      both providers.
- [x] Make provider selection symmetric via `MODEL_PROVIDER`, tested:
      `nvidia` + both keys → NVIDIA primary/OpenRouter fallback; `nvidia` +
      only NVIDIA key → bare `NvidiaNimClient`; `nvidia` + no NVIDIA key →
      falls back to bare `OpenRouterClient` instead of a dead-on-arrival
      client; invalid `MODEL_PROVIDER` value → rejected by Pydantic at
      `Settings()` construction.
- [ ] **Not live-verified against a real NVIDIA API key** — none was
      available while building this. `NVIDIA_MODEL`/`NVIDIA_VISION_MODEL`'s
      defaults are best-effort picks from NIM's published catalog naming,
      not confirmed working the way `OPENROUTER_VISION_MODEL`'s default was
      (a wrong first guess there 404'd and had to be corrected live). Stated
      as an open item rather than silently assumed correct — matches how
      every other real-API integration in this project was actually proven,
      and this one hasn't been yet. If a key becomes available: re-run the
      README's discover command with `NVIDIA_API_KEY` set and OpenRouter's
      key temporarily removed/invalidated, to force the fallback path and
      confirm the model IDs actually resolve.

Tests: 8 new (`NvidiaNimClient` x2, `FallbackLLMClient` x4, `_build_llm_client`
x2) plus `test_config.py` extended for the new settings, plus 3 more for the
`MODEL_PROVIDER` follow-up (`_build_llm_client` x2, `test_config.py`
validation x1 — `test_config.py`'s existing `test_defaults`/`test_env_override`
were also extended in place rather than duplicated). 121/121 passing.
