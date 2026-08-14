"""Database-backed turn runner that opens one transaction per user turn."""

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.feedback_evaluator import FakeFeedbackEvaluator
from agents.output_guard import FakeOutputGuard
from agents.response_agent import FakeResponseAgent
from agents.risk_agent import FakeRiskAgent
from agents.state_tracker import FakeStateTracker
from agents.strategy_planner import FakeStrategyPlanner
from orchestrator.post_turn_pipeline import NoopTaskQueue, TaskQueue
from orchestrator.turn_orchestrator import (
    FeedbackAnalyzer,
    RiskAnalyzer,
    StateDeltaExtractor,
    StrategySelector,
    TurnOrchestrator,
)
from schemas.common import SessionId, UserId
from schemas.messages import ChatTurnResult
from services.context_builder import ContextBuilder
from services.memory_retriever import MemoryRetriever
from services.state_reducer import StateReducer
from storage.database import transactional_session
from storage.repositories.intervention_repository import SqlAlchemyInterventionRepository
from storage.repositories.message_repository import SqlAlchemyMessageRepository
from storage.repositories.session_repository import SqlAlchemySessionRepository
from storage.repositories.state_repository import SqlAlchemyStateRepository
from storage.repositories.summary_repository import SqlAlchemySummaryRepository
from storage.repositories.user_repository import SqlAlchemyUserRepository


class TaskQueueFactory(Protocol):
    """Build a task queue bound to the current transaction session."""

    def __call__(self, session: AsyncSession) -> TaskQueue:
        """Return a queue that can observe writes in the supplied session."""


class SqlAlchemyTurnOrchestrator:
    """Runs TurnOrchestrator with SQLAlchemy repositories in one transaction."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_queue: TaskQueue | None = None,
        *,
        task_queue_factory: TaskQueueFactory | None = None,
        risk_agent: RiskAnalyzer | None = None,
        state_tracker: StateDeltaExtractor | None = None,
        strategy_planner: StrategySelector | None = None,
        feedback_evaluator: FeedbackAnalyzer | None = None,
        response_agent: FakeResponseAgent | None = None,
        output_guard: FakeOutputGuard | None = None,
    ) -> None:
        """Store the factory used to create one session per turn."""

        if task_queue is not None and task_queue_factory is not None:
            raise ValueError("task_queue and task_queue_factory are mutually exclusive")
        self._session_factory = session_factory
        self._task_queue = task_queue if task_queue is not None else NoopTaskQueue()
        self._task_queue_factory = task_queue_factory
        self._risk_agent = risk_agent or FakeRiskAgent()
        self._state_tracker = state_tracker or FakeStateTracker()
        self._strategy_planner = strategy_planner or FakeStrategyPlanner()
        self._feedback_evaluator = feedback_evaluator or FakeFeedbackEvaluator()
        self._response_agent = response_agent or FakeResponseAgent()
        self._output_guard = output_guard or FakeOutputGuard()

    @property
    def risk_agent(self) -> RiskAnalyzer:
        """Return the configured risk dependency for diagnostics and tests."""

        return self._risk_agent

    @property
    def state_tracker(self) -> StateDeltaExtractor:
        """Return the configured state dependency for diagnostics and tests."""

        return self._state_tracker

    @property
    def strategy_planner(self) -> StrategySelector:
        """Return the configured strategy dependency for diagnostics and tests."""

        return self._strategy_planner

    @property
    def feedback_evaluator(self) -> FeedbackAnalyzer:
        """Return the configured feedback evaluator."""

        return self._feedback_evaluator

    @property
    def response_agent(self) -> FakeResponseAgent:
        """Return the retained deterministic response agent."""

        return self._response_agent

    @property
    def output_guard(self) -> FakeOutputGuard:
        """Return the retained deterministic output guard."""

        return self._output_guard

    @property
    def task_queue(self) -> TaskQueue:
        """Return the configured post-turn queue boundary."""

        return self._task_queue

    async def handle_turn(
        self,
        user_id: UserId,
        session_id: SessionId,
        text: str,
    ) -> ChatTurnResult:
        """Ensure user/session rows and process one turn inside a transaction."""

        async with transactional_session(self._session_factory) as session:
            await SqlAlchemyUserRepository(session).ensure_user(user_id)
            await SqlAlchemySessionRepository(session).ensure_session(
                session_id,
                user_id,
            )
            orchestrator = self._build_turn_orchestrator(session)
            return await orchestrator.handle_turn(
                user_id=user_id,
                session_id=session_id,
                text=text,
            )

    def _build_turn_orchestrator(self, session: AsyncSession) -> TurnOrchestrator:
        """Build the inner orchestrator with repositories bound to this session."""

        return TurnOrchestrator(
            risk_agent=self._risk_agent,
            state_tracker=self._state_tracker,
            feedback_evaluator=self._feedback_evaluator,
            strategy_planner=self._strategy_planner,
            response_agent=self._response_agent,
            output_guard=self._output_guard,
            state_reducer=StateReducer(),
            context_builder=ContextBuilder(),
            memory_retriever=MemoryRetriever(),
            message_repository=SqlAlchemyMessageRepository(session),
            state_repository=SqlAlchemyStateRepository(session),
            summary_repository=SqlAlchemySummaryRepository(session),
            intervention_repository=SqlAlchemyInterventionRepository(session),
            task_queue=self._task_queue_for(session),
        )

    def _task_queue_for(self, session: AsyncSession) -> TaskQueue:
        """Return a per-transaction queue or the configured static queue."""

        if self._task_queue_factory is not None:
            return self._task_queue_factory(session)
        return self._task_queue


__all__ = ["SqlAlchemyTurnOrchestrator", "TaskQueueFactory"]
