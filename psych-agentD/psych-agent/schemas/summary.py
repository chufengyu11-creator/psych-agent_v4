"""Rolling, final session summary, and summarizer contracts."""

from pydantic import Field

from schemas.common import ContractModel, MessageId, SessionId
from schemas.intervention import InterventionRecord
from schemas.memory import MemoryCandidate
from schemas.messages import Message
from schemas.state import SessionState


class RollingSummary(ContractModel):
    """Compressed summary of older messages in the current session."""

    session_id: SessionId
    summary_version: int = Field(ge=1)
    covered_from: MessageId
    covered_to: MessageId
    current_problem: str | None = None
    session_goal: str | None = None
    important_user_statements: list[str] = Field(default_factory=list)
    strategies_attempted: list[str] = Field(default_factory=list)
    strategy_responses: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    source_message_ids: list[MessageId] = Field(default_factory=list)


class RollingSummarizerInput(ContractModel):
    """Input consumed by RollingSummarizer to update a session summary."""

    session_id: SessionId
    previous_summary: RollingSummary | None = None
    uncovered_messages: list[Message]
    current_state: SessionState
    interventions: list[InterventionRecord] = Field(default_factory=list)


class StrategyOutcome(ContractModel):
    """Session-level outcome for one attempted strategy."""

    strategy: str
    outcome: str
    source_message_ids: list[MessageId] = Field(default_factory=list)


class ActionItem(ContractModel):
    """A user-facing action item extracted at session close."""

    content: str
    source_message_ids: list[MessageId] = Field(default_factory=list)
    completed: bool = False


class SessionFinalizerInput(ContractModel):
    """Input consumed by SessionFinalizer when a session closes."""

    session_id: SessionId
    messages: list[Message]
    final_state: SessionState
    interventions: list[InterventionRecord] = Field(default_factory=list)
    rolling_summary: RollingSummary | None = None


class SessionFinalizerResult(ContractModel):
    """Structured output produced when closing a session."""

    session_id: SessionId
    session_summary: str
    goal_updates: list[str] = Field(default_factory=list)
    unfinished_topics: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    candidate_memories: list[MemoryCandidate] = Field(default_factory=list)
    strategy_outcomes: list[StrategyOutcome] = Field(default_factory=list)
    risk_events: list[str] = Field(default_factory=list)
    source_message_ids: list[MessageId] = Field(default_factory=list)
