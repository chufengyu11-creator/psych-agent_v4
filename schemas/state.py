"""Session-state and state-delta contracts.

StateTracker emits a StateDelta. StateReducer is the only component that turns
that delta into a new SessionState version.
"""

from enum import StrEnum

from pydantic import Field

from schemas.common import ContractModel, MessageId, SessionId, SourceReference
from schemas.feedback import FeedbackResult
from schemas.messages import Message
from schemas.risk import RiskLevel


class StateOperation(StrEnum):
    """Allowed operations for state updates."""

    ADD = "add"
    UPDATE = "update"
    REMOVE = "remove"
    RESOLVE_CONFLICT = "resolve_conflict"


class ConversationPhase(StrEnum):
    """High-level phase for the current session."""

    OPENING = "opening"
    EXPLORATION = "exploration"
    GOAL_ALIGNMENT = "goal_alignment"
    INTERVENTION = "intervention"
    PROGRESS_CHECK = "progress_check"
    CLOSING = "closing"
    SAFETY_CHECK = "safety_check"
    CRISIS_PROTOCOL = "crisis_protocol"
    HUMAN_ESCALATION = "human_escalation"


class StateItem(ContractModel):
    """A state fact with source evidence and conflict metadata."""

    value: str
    source: SourceReference
    active: bool = True
    conflict_with: list[str] = Field(default_factory=list)


class TopicUpdate(ContractModel):
    """A delta operation for an active session topic."""

    operation: StateOperation
    topic: str = Field(min_length=1)
    source_message_id: MessageId


class GoalUpdate(ContractModel):
    """A delta operation for the session goal."""

    operation: StateOperation
    goal: str = Field(min_length=1)
    source_message_id: MessageId


class EmotionReport(ContractModel):
    """An emotion explicitly reported by the user."""

    label: str = Field(min_length=1)
    source_message_id: MessageId


class UserCorrection(ContractModel):
    """A user correction that should override stale state."""

    correction: str = Field(min_length=1)
    replaces: str | None = None
    source_message_id: MessageId


class StrategyPreference(ContractModel):
    """A user preference about dialogue style or intervention type."""

    operation: StateOperation
    value: str = Field(min_length=1)
    source_message_id: MessageId


class Hypothesis(ContractModel):
    """A model hypothesis that must not be treated as user-stated fact."""

    value: str = Field(min_length=1)
    source_message_id: MessageId
    confidence: float = Field(ge=0.0, le=1.0)


class RiskState(ContractModel):
    """Current risk state stored inside SessionState."""

    level: RiskLevel = RiskLevel.LOW
    categories: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)


class SessionState(ContractModel):
    """Versioned state for one counselling-support session."""

    session_id: SessionId
    version: int = Field(ge=0, default=0)
    phase: ConversationPhase = ConversationPhase.OPENING
    current_turn_goal: str | None = None
    session_goal: str | None = None
    active_topics: list[StateItem] = Field(default_factory=list)
    reported_emotions: list[StateItem] = Field(default_factory=list)
    user_preferences: list[StateItem] = Field(default_factory=list)
    open_questions: list[StateItem] = Field(default_factory=list)
    pending_action_plan: str | None = None
    risk_state: RiskState = Field(default_factory=RiskState)


class StateDelta(ContractModel):
    """Incremental state changes extracted from a single user turn."""

    explicit_user_request: str | None = None
    current_turn_goal: str | None = None
    topic_updates: list[TopicUpdate] = Field(default_factory=list)
    goal_updates: list[GoalUpdate] = Field(default_factory=list)
    reported_emotions: list[EmotionReport] = Field(default_factory=list)
    user_corrections: list[UserCorrection] = Field(default_factory=list)
    strategy_preferences: list[StrategyPreference] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)


class StateTrackerInput(ContractModel):
    """Input consumed by StateTracker to extract a StateDelta."""

    current_message: Message
    previous_state: SessionState
    recent_messages: list[Message] = Field(default_factory=list)
    feedback: FeedbackResult | None = None

