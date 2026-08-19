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
    type: Literal["goal_parameter", "fixed"]
    param_name: str | None = None
    param_type: str | None = None
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


class OutputParam(BaseModel):
    name: str
    type: Literal["string", "number", "boolean"]
    description: str = ""


class OutcomeType(str, Enum):
    VALIDATION_ERROR = "validation_error"
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"


class OutcomePattern(BaseModel):
    """A UI state the target app can legitimately reach that isn't the success
    checkpoint but also isn't a failure of the automation itself (e.g. "insufficient
    funds", "no such member"). Checked before falling back to hard_failure."""

    outcome: Literal[OutcomeType.BUSINESS_OUTCOME, OutcomeType.RECOVERABLE]
    checkpoint: Checkpoint
    detail: str = ""


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
    created_from_run_id: str


class ReplayResult(BaseModel):
    outcome: OutcomeType
    outputs: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""
    step_index: int | None = None
    expected: str | None = None
    observed: str | None = None


class InterventionRequest(BaseModel):
    run_id: str
    capability_or_goal: str
    current_step: int | None = None
    screenshot_path: str | None = None
    reason: str
