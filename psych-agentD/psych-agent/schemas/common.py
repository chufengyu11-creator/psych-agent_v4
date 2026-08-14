"""Shared primitive schema types used across the dialogue agent.

This file intentionally stays small. Other schema modules import these building
blocks so cross-team contracts do not each invent their own timestamp,
identifier, or confidence field conventions.
"""

from datetime import UTC, datetime
from typing import NewType

from pydantic import BaseModel, ConfigDict, Field

UserId = NewType("UserId", str)
SessionId = NewType("SessionId", str)
MessageId = NewType("MessageId", str)
InterventionId = NewType("InterventionId", str)
MemoryId = NewType("MemoryId", str)


class ContractModel(BaseModel):
    """Base model for public contracts.

    Extra fields are forbidden so producer/consumer mismatches fail during
    contract tests instead of being silently ignored.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def utc_now() -> datetime:
    """Return the current UTC timestamp for schema default factories."""

    return datetime.now(UTC)


class SourceReference(ContractModel):
    """Reference to the raw message evidence behind derived state or memory."""

    message_id: MessageId
    quote: str | None = Field(default=None)


class ConfidenceScore(ContractModel):
    """Normalized confidence emitted by model-backed components."""

    value: float = Field(ge=0.0, le=1.0)
