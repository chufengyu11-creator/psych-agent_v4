"""SQLAlchemy integration test for automatic post-turn rolling summaries."""

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from agents.feedback_evaluator import FakeFeedbackEvaluator
from agents.output_guard import FakeOutputGuard
from agents.response_agent import FakeResponseAgent
from agents.risk_agent import FakeRiskAgent
from agents.rolling_summarizer import FakeRollingSummarizer
from agents.state_tracker import FakeStateTracker
from agents.strategy_planner import FakeStrategyPlanner
from orchestrator.post_turn_pipeline import InlinePostTurnTaskQueue
from orchestrator.turn_orchestrator import TurnOrchestrator
from schemas.common import SessionId, UserId
from services.context_builder import ContextBuilder
from services.memory_retriever import MemoryRetriever
from services.state_reducer import StateReducer
from storage.models.base import Base
from storage.models.registry import load_all_models
from storage.models.session import SessionModel
from storage.models.summary import RollingSummaryVersionModel
from storage.repositories.intervention_repository import SqlAlchemyInterventionRepository
from storage.repositories.message_repository import SqlAlchemyMessageRepository
from storage.repositories.session_repository import SqlAlchemySessionRepository
from storage.repositories.state_repository import SqlAlchemyStateRepository
from storage.repositories.summary_repository import SqlAlchemySummaryRepository
from storage.repositories.user_repository import SqlAlchemyUserRepository
from workers.post_turn_worker import PostTurnWorker
from workers.summary_worker import SummaryWorker


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide a clean in-memory SQLite database."""

    load_all_models()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


async def test_turn_orchestrator_retains_latest_turn_in_post_turn_summary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The first turn stays raw and the second turn summarizes only the first."""

    user_id = UserId("sql-post-turn-user")
    session_id = SessionId("sql-post-turn-session")
    async with session_factory() as session:
        await SqlAlchemyUserRepository(session).ensure_user(user_id)
        await SqlAlchemySessionRepository(session).ensure_session(session_id, user_id)
        messages = SqlAlchemyMessageRepository(session)
        states = SqlAlchemyStateRepository(session)
        summaries = SqlAlchemySummaryRepository(session)
        interventions = SqlAlchemyInterventionRepository(session)
        queue = InlinePostTurnTaskQueue(
            PostTurnWorker(
                SummaryWorker(
                    message_repository=messages,
                    state_repository=states,
                    summary_repository=summaries,
                    intervention_repository=interventions,
                    summarizer=FakeRollingSummarizer(),
                )
            )
        )
        orchestrator = TurnOrchestrator(
            risk_agent=FakeRiskAgent(),
            state_tracker=FakeStateTracker(),
            feedback_evaluator=FakeFeedbackEvaluator(),
            strategy_planner=FakeStrategyPlanner(),
            response_agent=FakeResponseAgent(),
            output_guard=FakeOutputGuard(),
            state_reducer=StateReducer(),
            context_builder=ContextBuilder(),
            memory_retriever=MemoryRetriever(),
            message_repository=messages,
            state_repository=states,
            summary_repository=summaries,
            intervention_repository=interventions,
            task_queue=queue,
        )

        first_turn = await orchestrator.handle_turn(
            user_id=user_id,
            session_id=session_id,
            text="I need one concrete next step.",
        )
        assert await summaries.get_current(session_id) is None
        assert len(queue.results) == 1
        assert queue.results[0].summary_updated is False

        await orchestrator.handle_turn(
            user_id=user_id,
            session_id=session_id,
            text="I am still thinking about the situation.",
        )
        await session.commit()

    async with session_factory() as session:
        rows = list((await session.execute(select(RollingSummaryVersionModel))).scalars())
        stored_session = await session.get(SessionModel, str(session_id))
        current = await SqlAlchemySummaryRepository(session).get_current(session_id)

    assert len(queue.results) == 2
    assert queue.results[1].summary_updated is True
    assert queue.results[1].summary_version == 1
    assert len(rows) == 1
    assert current is not None
    assert current.summary_version == 1
    assert current.covered_to == first_turn.message_id
    assert stored_session is not None
    assert stored_session.current_summary_version == 1
