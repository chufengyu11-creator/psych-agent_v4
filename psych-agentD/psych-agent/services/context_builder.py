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
        """Build context sections in stable priority order.

        The builder is read-only: it does not mutate state, messages, memories,
        summaries, or persistence. It only packages already-produced artifacts
        into the shape consumed by ResponseAgent and OutputGuard.
        """

        sections = [
            self._section("system_policy", DEFAULT_SYSTEM_POLICY),
            self._section("risk_state", risk.model_dump_json()),
            self._section("session_state", session_state.model_dump_json()),
            self._section("strategy_plan", strategy.model_dump_json()),
        ]
        if rolling_summary is not None:
            sections.append(
                self._section("rolling_summary", rolling_summary.model_dump_json())
            )
        sections.extend(
            [
                self._section(
                    "recent_messages",
                    self._format_recent_messages(recent_messages),
                ),
                self._section("long_term_memory", memories.model_dump_json()),
            ]
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

    def _section(self, name: str, content: str) -> ContextSection:
        """Create one named context section with its configured token budget."""

        return ContextSection(
            name=name,
            content=content,
            token_budget=self._token_budget.budget_for(name),
        )

    def _format_recent_messages(self, recent_messages: list[Message]) -> str:
        """Format recent messages as compact role-prefixed transcript text."""

        return "\n".join(
            f"{message.sequence_number}. {message.role.value}: {message.content}"
            for message in recent_messages
        )

