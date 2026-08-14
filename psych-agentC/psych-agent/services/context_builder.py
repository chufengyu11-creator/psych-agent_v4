"""Builds model context from state, memory, strategy, and messages."""

from schemas.context import ContextSection, ResponseContext
from schemas.memory import RetrievedMemories
from schemas.messages import Message
from schemas.risk import RiskResult
from schemas.state import SessionState
from schemas.strategy import StrategyPlan
from schemas.summary import RollingSummary
from services.token_budget import TokenBudgetManager

DEFAULT_SYSTEM_POLICY = (
    "You are a psychological support dialogue agent. Provide emotional support, "
    "do not diagnose, do not provide medication advice, and follow safety routing."
)


class ContextBuilder:
    """Assembles a typed ResponseContext for ResponseAgent."""

    def __init__(self, token_budget: TokenBudgetManager | None = None) -> None:
        """Create a builder with an optional token budget manager."""

        self._token_budget = token_budget or TokenBudgetManager()

    async def build(
        self,
        session_state: SessionState,
        rolling_summary: RollingSummary | None,
        recent_messages: list[Message],
        memories: RetrievedMemories,
        strategy: StrategyPlan,
        risk: RiskResult,
    ) -> ResponseContext:
        """Build context in the priority order defined by the architecture docs."""

        sections = [
            ContextSection(
                name="system_policy",
                content=DEFAULT_SYSTEM_POLICY,
                token_budget=self._token_budget.budget_for("system_policy"),
            ),
            ContextSection(
                name="session_state",
                content=session_state.model_dump_json(),
                token_budget=self._token_budget.budget_for("session_state"),
            ),
            ContextSection(
                name="strategy_plan",
                content=strategy.model_dump_json(),
                token_budget=self._token_budget.budget_for("strategy_plan"),
            ),
            ContextSection(
                name="recent_messages",
                content="\n".join(message.content for message in recent_messages),
                token_budget=self._token_budget.budget_for("recent_messages"),
            ),
        ]
        if rolling_summary is not None:
            sections.insert(
                3,
                ContextSection(
                    name="rolling_summary",
                    content=rolling_summary.model_dump_json(),
                    token_budget=self._token_budget.budget_for("rolling_summary"),
                ),
            )
        return ResponseContext(
            system_policy=DEFAULT_SYSTEM_POLICY,
            risk=risk,
            session_state=session_state,
            strategy=strategy,
            recent_messages=recent_messages,
            rolling_summary=rolling_summary,
            memories=memories,
            sections=sections,
        )
