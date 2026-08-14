"""Risk detection contracts for routing a user turn."""

from enum import StrEnum

from pydantic import Field

from schemas.common import ContractModel
from schemas.messages import Message


class RiskLevel(StrEnum):
    """Coarse risk level assigned to the current turn."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    IMMINENT = "imminent"


class RiskRoute(StrEnum):
    """Allowed routing decisions after risk analysis."""

    NORMAL = "normal_dialogue"
    CLARIFICATION = "safety_clarification"
    CRISIS = "crisis_protocol"
    HUMAN = "human_escalation"


class RiskInput(ContractModel):
    """Input consumed by a risk analyzer."""

    current_message: Message
    recent_messages: list[Message] = Field(default_factory=list)
    current_risk_level: RiskLevel = RiskLevel.LOW


class RiskResult(ContractModel):
    """Structured output produced by a risk analyzer."""

    risk_level: RiskLevel
    categories: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    route: RiskRoute
    reason_codes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)

