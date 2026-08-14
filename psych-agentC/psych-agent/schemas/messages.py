"""Message and turn-result contracts.

Messages are append-only records. Derived artifacts such as state, summaries,
and memories should point back to message IDs when they make important claims.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from schemas.common import ContractModel, MessageId, SessionId, utc_now


class MessageRole(StrEnum):
    """Allowed participants in the persisted conversation log."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class Message(ContractModel):
    """A single raw conversation message."""

    id: MessageId
    session_id: SessionId
    role: MessageRole
    content: str = Field(min_length=1)
    sequence_number: int = Field(ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    model_name: str | None = None
    parent_message_id: MessageId | None = None


class ChatTurnRequest(ContractModel):
    """API-level request for processing one user turn."""

    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ChatTurnResult(ContractModel):
    """API-level response returned after the orchestrator finishes a turn."""

    message_id: MessageId
    response: str
    session_id: SessionId
    status: str = "ok"
    state_version: int = Field(ge=0)


