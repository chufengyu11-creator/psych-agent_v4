"""Response generation and output-guard contracts."""

from enum import StrEnum

from pydantic import Field

from schemas.common import ContractModel
from schemas.context import ResponseContext
from schemas.risk import RiskResult


class DraftResponse(ContractModel):
    """Candidate assistant response before final safety review."""

    text: str
    asked_question: bool = False
    contains_action_suggestion: bool = False
    referenced_memory_ids: list[str] = Field(default_factory=list)
    referenced_knowledge_ids: list[str] = Field(default_factory=list)


class GuardDecision(StrEnum):
    """Allowed output-guard decisions."""

    ALLOW = "allow"
    REWRITE = "rewrite"
    BLOCK = "block"
    ROUTE_TO_SAFETY = "route_to_safety"


class GuardInput(ContractModel):
    """Input consumed by OutputGuard."""

    draft: DraftResponse
    context: ResponseContext
    risk: RiskResult


class GuardResult(ContractModel):
    """Output produced by OutputGuard."""

    decision: GuardDecision
    violations: list[str] = Field(default_factory=list)
    rewritten_response: str | None = None

    def final_text(self, draft: DraftResponse) -> str:
        """Return the text that should be sent to the user."""

        if self.decision == GuardDecision.REWRITE and self.rewritten_response is not None:
            return self.rewritten_response
        if self.decision == GuardDecision.BLOCK:
            return "抱歉，这条回复没有通过安全检查。我们可以先回到当下最重要的安全和支持需求。"
        if self.decision == GuardDecision.ROUTE_TO_SAFETY:
            return "我想先确认你的安全状况。你现在是否有立即伤害自己或他人的风险？"
        return draft.text

