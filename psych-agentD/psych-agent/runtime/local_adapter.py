"""Local Python adapter for embedding the agent in local deployments."""

from orchestrator.turn_orchestrator import TurnOrchestrator
from runtime.factory import build_in_memory_orchestrator
from schemas.common import SessionId, UserId
from schemas.messages import ChatTurnResult


class LocalPsychAgent:
    """Small local entrypoint that wraps TurnOrchestrator.

    Local deployments can import this class directly instead of starting FastAPI.
    It does not own business logic; it only adapts plain strings into typed IDs
    and delegates to the shared orchestrator contract.
    """

    def __init__(self, orchestrator: TurnOrchestrator | None = None) -> None:
        """Create a local adapter with an optional injected orchestrator."""

        self._orchestrator = orchestrator or build_in_memory_orchestrator()

    async def handle_message(self, user_id: str, session_id: str, text: str) -> ChatTurnResult:
        """Process one local message and return the typed turn result."""

        return await self._orchestrator.handle_turn(
            user_id=UserId(user_id),
            session_id=SessionId(session_id),
            text=text,
        )
