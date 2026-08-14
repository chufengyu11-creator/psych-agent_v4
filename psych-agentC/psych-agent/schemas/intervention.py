"""Intervention ledger contracts."""

from enum import StrEnum

from pydantic import Field

from schemas.common import ContractModel, InterventionId, MessageId, SessionId


class InterventionStatus(StrEnum):
    """Lifecycle status for an intervention record."""

    PENDING = "pending"
    EVALUATED = "evaluated"
    CANCELLED = "cancelled"


class InterventionRecord(ContractModel):
    """Stores one assistant strategy and later feedback about its effect."""

    intervention_id: InterventionId
    session_id: SessionId
    assistant_message_id: MessageId
    strategy: str
    objective: str
    expected_signals: list[str] = Field(default_factory=list)
    status: InterventionStatus
    observed_response: str | None = None
    explicit_feedback: str | None = None
    strategy_fit: str | None = None
    objective_progress: str | None = None
    recommended_adjustment: str | None = None

