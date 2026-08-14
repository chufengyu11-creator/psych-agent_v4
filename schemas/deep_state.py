"""Persisted deep-state result contracts."""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from schemas.common import ContractModel, MessageId, SessionId
from schemas.state import StateDelta


class DeepStateStatus(StrEnum):
    """Lifecycle states for one completed deep-state analysis."""

    READY = "ready"
    APPLIED = "applied"
    EXPIRED = "expired"
    FAILED = "failed"


class DeepStateCandidate(ContractModel):
    """One completed deep result that may be consumed by a later turn."""

    id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: SessionId
    source_message_id: MessageId
    source_message_sequence: int = Field(ge=1)
    base_state_version: int = Field(ge=0)
    pipeline_version: str = Field(min_length=1)
    delta: StateDelta
    finished_at: datetime


__all__ = [
    "DeepStateCandidate",
    "DeepStateStatus",
]
