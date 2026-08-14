"""Integration test for SummaryWorker with SQLAlchemy repositories."""

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from agents.rolling_summarizer import FakeRollingSummarizer
from schemas.common import SessionId, UserId
from schemas.events import RollingSummaryRequestedEvent
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


async def test_summary_worker_persists_and_advances_session_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Worker output should be readable and linked from the session row."""

    session_id = SessionId("summary-worker-integration")
    user_id = UserId("summary-worker-user")
    async with session_factory() as session:
        await SqlAlchemyUserRepository(session).ensure_user(user_id)
        await SqlAlchemySessionRepository(session).ensure_session(session_id, user_id)
        messages = SqlAlchemyMessageRepository(session)
        first = await messages.create_user_message(session_id, "I need a small next step.")
        last = await messages.create_assistant_message(session_id, "Let's choose one.")
        summaries = SqlAlchemySummaryRepository(session)
        worker = SummaryWorker(
            message_repository=messages,
            state_repository=SqlAlchemyStateRepository(session),
            summary_repository=summaries,
            intervention_repository=SqlAlchemyInterventionRepository(session),
            summarizer=FakeRollingSummarizer(),
        )

        result = await worker.handle_summary_requested(
            RollingSummaryRequestedEvent(
                session_id=session_id,
                trigger="integration-test",
                after_message_id=last.id,
            )
        )
        await session.commit()

    async with session_factory() as session:
        current = await SqlAlchemySummaryRepository(session).get_current(session_id)
        session_row = await session.get(SessionModel, str(session_id))
        rows = list((await session.execute(select(RollingSummaryVersionModel))).scalars())

    assert result is not None
    assert current == result
    assert first.id in result.source_message_ids
    assert session_row is not None
    assert session_row.current_summary_version == 1
    assert len(rows) == 1
