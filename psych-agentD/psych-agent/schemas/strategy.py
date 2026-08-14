"""Dialogue strategy planning contracts."""

from enum import StrEnum

from pydantic import Field

from schemas.common import ContractModel
from schemas.feedback import FeedbackResult
from schemas.memory import RetrievedMemories
from schemas.risk import RiskResult
from schemas.state import ConversationPhase, SessionState


class StrategyType(StrEnum):
    """Allowed first-version dialogue strategies."""

    REFLECTIVE_LISTENING = "reflective_listening"
    CLARIFICATION = "clarification"
    EMOTIONAL_EXPLORATION = "emotional_exploration"
    SUMMARIZATION = "summarization"
    PSYCHOEDUCATION = "psychoeducation"
    COLLABORATIVE_PROBLEM_SOLVING = "collaborative_problem_solving"
    ACTION_PLANNING = "action_planning"
    PROGRESS_CHECK = "progress_check"
    SAFETY_CHECK = "safety_check"
    SESSION_CLOSING = "session_closing"


class StrategyPlannerInput(ContractModel):
    """Input consumed by StrategyPlanner."""

    session_state: SessionState
    risk: RiskResult
    feedback: FeedbackResult | None = None
    memories: RetrievedMemories = Field(default_factory=RetrievedMemories)


class StrategyPlan(ContractModel):
    """The selected strategy for the next assistant response."""

    conversation_phase: ConversationPhase
    primary_strategy: StrategyType
    objective: str
    reason: str
    avoid: list[str] = Field(default_factory=list)
    expected_signals: list[str] = Field(default_factory=list)
    switch_conditions: list[str] = Field(default_factory=list)

