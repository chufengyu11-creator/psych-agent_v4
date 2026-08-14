"""Contracts for evaluating user response to the previous intervention."""

from enum import StrEnum

from pydantic import Field

from schemas.common import ContractModel
from schemas.intervention import InterventionRecord
from schemas.messages import Message


class FeedbackLabel(StrEnum):
    """Coarse explicit feedback label inferred from the user response."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    ABSENT = "absent"


class ObjectiveProgress(StrEnum):
    """Whether the previous strategy objective appears to have advanced."""

    ACHIEVED = "achieved"
    PARTIAL = "partial"
    NOT_ACHIEVED = "not_achieved"
    UNKNOWN = "unknown"


class StrategyFit(StrEnum):
    """How well the previous strategy fit the latest user response."""

    GOOD = "good"
    MIXED = "mixed"
    POOR = "poor"
    UNKNOWN = "unknown"


class FeedbackInput(ContractModel):
    """Input consumed by FeedbackEvaluator."""

    previous_intervention: InterventionRecord
    assistant_message: Message
    next_user_message: Message


class FeedbackResult(ContractModel):
    """Structured feedback about the previous intervention."""

    observed_response: str
    explicit_feedback: FeedbackLabel
    objective_progress: ObjectiveProgress
    strategy_fit: StrategyFit
    recommended_adjustment: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)

