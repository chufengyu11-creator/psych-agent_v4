"""Contracts for the model context assembled for response generation."""

from pydantic import Field

from schemas.common import ContractModel
from schemas.knowledge import RetrievedKnowledge
from schemas.memory import MemoryQueryIntent, RetrievedMemories
from schemas.messages import Message
from schemas.risk import RiskResult
from schemas.state import SessionState
from schemas.strategy import StrategyPlan
from schemas.summary import RollingSummary


class ContextSection(ContractModel):
    """A named context block with token-budget metadata."""

    name: str
    content: str
    token_budget: int


class ResponseContext(ContractModel):
    """The complete input passed to ResponseAgent."""

    system_policy: str
    risk: RiskResult
    session_state: SessionState
    strategy: StrategyPlan
    recent_messages: list[Message]
    rolling_summary: RollingSummary | None = None
    memories: RetrievedMemories = Field(default_factory=RetrievedMemories)
    knowledge: RetrievedKnowledge = Field(default_factory=RetrievedKnowledge)
    current_user_message: str = ""
    nonverbal_observations: dict[str, object] | None = None
    memory_query_intent: MemoryQueryIntent = MemoryQueryIntent.NONE
    sections: list[ContextSection] = Field(default_factory=list)
