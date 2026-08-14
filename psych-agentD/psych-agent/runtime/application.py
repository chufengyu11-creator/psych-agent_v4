"""Application-owned runtime resources and their safe lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agents.output_guard import FakeOutputGuard
from agents.response_agent import FakeResponseAgent
from app.config import AppRuntimeMode, Settings
from llm.local_client import OpenAICompatibleHTTPClient
from llm.structured_client import (
    StructuredLLMClient,
    StructuredLLMClientProtocol,
)
from orchestrator.post_turn_pipeline import TaskQueue
from orchestrator.turn_orchestrator import (
    FeedbackAnalyzer,
    RiskAnalyzer,
    StateDeltaExtractor,
    StrategySelector,
)
from runtime.redis_client import RedisResource
from schemas.common import SessionId, UserId
from schemas.messages import ChatTurnResult


class TurnHandler(Protocol):
    """Minimal turn contract shared by in-memory and SQLAlchemy runtimes."""

    async def handle_turn(
        self,
        user_id: UserId,
        session_id: SessionId,
        text: str,
    ) -> ChatTurnResult:
        """Process one user turn."""


class RuntimeConfigurationError(RuntimeError):
    """Raised when a selected runtime cannot be built safely."""


@dataclass(repr=False)
class ApplicationRuntime:
    """Process-scoped resources shared safely across API requests."""

    settings: Settings
    mode: AppRuntimeMode
    orchestrator: TurnHandler
    redis_client: RedisResource | None = None
    redis_required: bool = False
    engine: AsyncEngine | None = None
    session_factory: async_sessionmaker[AsyncSession] | None = None
    http_client: OpenAICompatibleHTTPClient | None = None
    structured_client: StructuredLLMClient | None = None
    agent_structured_client: StructuredLLMClientProtocol | None = None
    risk_agent: RiskAnalyzer | None = None
    state_tracker: StateDeltaExtractor | None = None
    strategy_planner: StrategySelector | None = None
    response_agent: FakeResponseAgent | None = None
    output_guard: FakeOutputGuard | None = None
    feedback_evaluator: FeedbackAnalyzer | None = None
    task_queue: TaskQueue | None = None
    session_closer: object | None = None
    _closed: bool = field(default=False, init=False)

    @property
    def closed(self) -> bool:
        """Return whether application shutdown has already run."""

        return self._closed

    async def aclose(self) -> None:
        """Close model and database resources once, in dependency order."""

        if self._closed:
            return
        self._closed = True
        try:
            if self.redis_client is not None:
                await self.redis_client.aclose()
        finally:
            try:
                if self.http_client is not None:
                    await self.http_client.aclose()
            finally:
                if self.engine is not None:
                    await self.engine.dispose()

    def __repr__(self) -> str:
        """Return resource state without settings, URLs, or secrets."""

        return (
            f"{type(self).__name__}(mode={self.mode!r}, "
            f"has_redis_client={self.redis_client is not None}, "
            f"has_engine={self.engine is not None}, "
            f"has_http_client={self.http_client is not None}, "
            f"closed={self._closed})"
        )


__all__ = [
    "ApplicationRuntime",
    "RuntimeConfigurationError",
    "TurnHandler",
]
