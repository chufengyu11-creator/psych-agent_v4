"""Integration test for the SQLAlchemy session-close memory workflow."""

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from agents.session_finalizer import FakeSessionFinalizer
from schemas.common import SessionId, UserId
from schemas.events import SessionCloseRequestedEvent
from services.memory_policy import MemoryPolicy
from storage.models.base import Base
from storage.models.memory import LongTermMemoryModel
from storage.models.registry import load_all_models
from storage.models.session import SESSION_STATUS_CLOSED, SessionModel
from storage.repositories.intervention_repository import SqlAlchemyInterventionRepository
from storage.repositories.memory_repository import SqlAlchemyMemoryRepository
from storage.repositories.message_repository import SqlAlchemyMessageRepository
from storage.repositories.session_repository import SqlAlchemySessionRepository
from storage.repositories.state_repository import SqlAlchemyStateRepository
from storage.repositories.summary_repository import SqlAlchemySummaryRepository
from storage.repositories.user_repository import SqlAlchemyUserRepository
from workers.session_close_worker import SessionCloseMemoryWorker


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


async def test_worker_persists_memory_sources_and_closes_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Policy-approved memory should persist before the session is closed."""

    session_id = SessionId("close-worker-integration")
    user_id = UserId("close-worker-integration-user")
    async with session_factory() as session:
        await SqlAlchemyUserRepository(session).ensure_user(user_id)
        sessions = SqlAlchemySessionRepository(session)
        await sessions.ensure_session(user_id, session_id)
        messages = SqlAlchemyMessageRepository(session)
        source = await messages.create_user_message(
            user_id,
            session_id,
            "我希望每次一个小步骤，不要一次太多建议。",
        )
        result = await SessionCloseMemoryWorker(
            message_repository=messages,
            state_repository=SqlAlchemyStateRepository(session),
            summary_repository=SqlAlchemySummaryRepository(session),
            intervention_repository=SqlAlchemyInterventionRepository(session),
            memory_repository=SqlAlchemyMemoryRepository(session),
            session_repository=sessions,
            finalizer=FakeSessionFinalizer(),
            memory_policy=MemoryPolicy(),
        ).handle_session_close_requested(
            SessionCloseRequestedEvent(session_id=session_id, user_id=user_id)
        )
        await session.commit()

    async with session_factory() as session:
        session_row = await session.scalar(
            select(SessionModel).where(
                SessionModel.user_id == str(user_id),
                SessionModel.session_id == str(session_id),
            )
        )
        memories = list((await session.execute(select(LongTermMemoryModel))).scalars())

    assert result.candidate_memory_count == 1
    assert result.memory_write_count == 1
    assert session_row is not None
    assert session_row.status == SESSION_STATUS_CLOSED
    assert len(memories) == 1
    assert memories[0].source_message_ids == [str(source.id)]
