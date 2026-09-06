from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class RiskTier(str, Enum):
    SAFE = "safe"
    RISKY = "risky"


class LocatorStrategy(str, Enum):
    ROLE = "role"
    TEXT = "text"
    CSS = "css"


class Locator(BaseModel):
    strategy: LocatorStrategy
    value: dict[str, Any]
    fallback: Locator | None = None


class ValueSource(BaseModel):
    # "fixed" used to be a second valid type here, but at replay time
    # (replay/engine.py) it was never actually distinguished from value_source=None -
    # both fall through to the same "use Step.value literally" branch. It added a
    # state the LLM had to reason about (what goes in param_name/reason for a "fixed"
    # entry?) for zero behavioral difference - see EXT_TASK_FIXES.md's ValueSource
    # cleanup note. A Step's value is now exactly binary: value_source=None means
    # "literal, read Step.value"; value_source present means "replay substitutes
    # params[param_name]". Nothing else.
    type: Literal["goal_parameter"]
    param_name: str
    param_type: str | None = None
    # Human-readable note on why this field was chosen to vary per call (e.g. "the
    # member being serviced changes every invocation") - purely for a reviewer reading
    # the artifact per §3.2's "reviewable" requirement; never read by any code.
    reason: str | None = None


class CheckpointType(str, Enum):
    ELEMENT_VISIBLE = "element_visible"
    TEXT_PRESENT = "text_present"
    URL_MATCHES = "url_matches"


class Checkpoint(BaseModel):
    type: CheckpointType
    locator: Locator | None = None
    text: str | None = None
    url_pattern: str | None = None


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE_TEXT = "type_text"
    SELECT_OPTION = "select_option"
    EXTRACT = "extract"


class Step(BaseModel):
    action: ActionType
    target: str | None = None
    locator: Locator | None = None
    value_source: ValueSource | None = None
    value: str | None = None
    extract_as: str | None = None
    risk_tier: RiskTier = RiskTier.SAFE
    checkpoint: Checkpoint | None = None


class InputParam(BaseModel):
    name: str
    type: Literal["string", "number", "boolean"]
    required: bool = True
    description: str = ""
    example: Any = None


class DerivedOutputSpec(BaseModel):
    """Declares that this output is computed from another already-extracted output,
    not read directly off the page. Resolved by ReplayEngine after all steps run -
    pure text parsing + arithmetic, never an LLM call, so "replay never invokes the
    model" stays true. "sum_currency" is the only op today: parse every $X,XXX.XX
    substring out of from_output's text and sum them. Exists because some pages (e.g.
    MERIDIAN CORE's member record) list individual line items with no total of their
    own - a capability that needs a total has to compute one, not extract it."""
    from_output: str
    op: Literal["sum_currency"] = "sum_currency"


class OutputParam(BaseModel):
    name: str
    type: Literal["string", "number", "boolean"]
    description: str = ""
    derive: DerivedOutputSpec | None = None


class OutcomeType(str, Enum):
    VALIDATION_ERROR = "validation_error"
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"


class OutcomePattern(BaseModel):
    """A UI state the target app can legitimately reach that isn't the success
    checkpoint but also isn't a failure of the automation itself (e.g. "insufficient
    funds", "no such member"). Checked before falling back to hard_failure.

    `max_retries`/`retry_delay_seconds`/`recovery_action` are meaningful only when
    `outcome == RECOVERABLE` (a `business_outcome` like "no such member" is a legitimate
    answer, not something retrying fixes - ReplayEngine never retries those regardless
    of this default). `max_retries` defaults to 3: a plain re-check-and-retry has real
    value even with no `recovery_action` declared (§3.3's "wait/retry a transient load"
    is exactly "the condition may clear on its own a moment later"), so every
    RECOVERABLE pattern gets that for free rather than requiring every artifact author
    to opt in explicitly. Set `max_retries=0` on a specific pattern to opt back out and
    always report immediately - e.g. a condition you know retrying can never clear.

    HARD_FAILURE is allowed here so a target's own "something broke on our side" page
    (MERIDIAN's APPLICATION ERROR screen) can be RECOGNISED and reported in those words
    rather than surfacing as whatever locator happened to time out next - it is still a
    hard failure, just a legible one."""

    outcome: Literal[OutcomeType.BUSINESS_OUTCOME, OutcomeType.RECOVERABLE, OutcomeType.HARD_FAILURE]
    checkpoint: Checkpoint
    detail: str = ""
    # Where the live, specific reason sits on the matched page, when the target renders
    # one. MERIDIAN names the actual rule that failed ("Insufficient available balance
    # in the source share") in a list under a generic "could not be validated" banner:
    # without this the caller is told only the category, which for a rejected request is
    # the least useful half of the answer. Read at match time and appended to `detail`.
    detail_locator: Locator | None = None
    max_retries: int = 3
    retry_delay_seconds: float = 0
    recovery_action: Step | None = None


class Artifact(BaseModel):
    capability_name: str
    version: int = 1
    target: dict[str, str]
    description: str = ""
    input_schema: list[InputParam] = Field(default_factory=list)
    output_schema: list[OutputParam] = Field(default_factory=list)
    steps: list[Step]
    success_checkpoint: Checkpoint
    outcome_patterns: list[OutcomePattern] = Field(default_factory=list)
    # "retired" is distinct from "rejected": a rejected draft was never approved
    # for use, while a retired version WAS live in production and was
    # deliberately withdrawn - approve_artifact() allows re-approving a retired
    # version (but not a rejected one), which is the rollback path is_default
    # exists for. Before "retired" existed, retire_artifact() set "rejected"
    # too, making the two indistinguishable and rollback a one-way door.
    status: Literal["draft", "approved", "rejected", "retired"] = "approved"
    created_from_run_id: str
    # When multiple versions of a capability are "approved" (e.g. after re-recording
    # a capability against a site change, the old one is kept approved for rollback
    # rather than immediately retired), an unpinned invoke/replay needs a
    # deterministic pick rather than always silently picking "the highest version
    # number" - is_default lets an operator explicitly choose which one that is.
    # At most one version per capability should be True at a time; enforced by
    # set_default_version() (comp_use/cli.py), not by this schema.
    is_default: bool = False


class ReplayResult(BaseModel):
    outcome: OutcomeType
    outputs: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""
    step_index: int | None = None
    expected: str | None = None
    observed: str | None = None
    # Set only by the CLI's optional --diagnose-drift-on-failure post-mortem
    # (comp_use/drift.py) - never by ReplayEngine itself, which stays LLM-free.
    # Points at a NEW artifact version saved to disk, not yet approved/active.
    proposed_patch_version: int | None = None


class InterventionRequest(BaseModel):
    run_id: str
    capability_or_goal: str
    current_step: int | None = None
    screenshot_path: str | None = None
    reason: str
