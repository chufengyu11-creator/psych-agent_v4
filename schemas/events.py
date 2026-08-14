"""Internal event contracts used by workers and local runtime integration."""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from schemas.common import ContractModel, MessageId, SessionId, UserId, utc_now


class EventType(StrEnum):
    """Internal event names for asynchronous or deferred work."""

    POST_TURN = "post_turn"
    ROLLING_SUMMARY_REQUESTED = "rolling_summary_requested"
    SESSION_CLOSE_REQUESTED = "session_close_requested"
    MEMORY_CANDIDATE_CREATED = "memory_candidate_created"


class BaseEvent(ContractModel):
    """Common fields for internal events."""

    event_type: EventType
    session_id: SessionId
    created_at: datetime = Field(default_factory=utc_now)


class PostTurnEvent(BaseEvent):
    """Event emitted after an assistant turn is persisted."""

    event_type: EventType = EventType.POST_TURN
    user_id: UserId
    user_message_id: MessageId
    assistant_message_id: MessageId
    state_version: int = Field(ge=0)


class RollingSummaryRequestedEvent(BaseEvent):
    """Event requesting a rolling summary update."""

    event_type: EventType = EventType.ROLLING_SUMMARY_REQUESTED
    user_id: UserId
    trigger: str
    after_message_id: MessageId | None = None


class SessionCloseRequestedEvent(BaseEvent):
    """Event requesting session finalization."""

    event_type: EventType = EventType.SESSION_CLOSE_REQUESTED
    user_id: UserId
    reason: str | None = None


class MemoryCandidateCreatedEvent(BaseEvent):
    """Event emitted after candidate memories are generated."""

    event_type: EventType = EventType.MEMORY_CANDIDATE_CREATED
    user_id: UserId
    candidate_count: int = Field(ge=0)
    source_message_ids: list[MessageId] = Field(default_factory=list)
