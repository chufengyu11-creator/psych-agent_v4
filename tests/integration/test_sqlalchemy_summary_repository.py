"""Integration tests for SQLAlchemy rolling-summary persistence."""

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from schemas.common import SessionId, UserId
from schemas.summary import RollingSummary
from storage.models.base import Base
from storage.models.registry import load_all_models
from storage.models.session import SessionModel
from storage.models.summary import RollingSummaryVersionModel
from storage.repositories.message_repository import SqlAlchemyMessageRepository
from storage.repositories.session_repository import SqlAlchemySessionRepository
from storage.repositories.summary_repository import SqlAlchemySummaryRepository
from storage.repositories.user_repository import SqlAlchemyUserRepository


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide a clean in-memory SQLite database for one test."""

    load_all_models()
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def test_sqlalchemy_summary_repository_saves_and_reads_current_summary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A rolling summary version should persist and update the session counter."""

    session_id = SessionId("summary-session-1")
    user_id = UserId("summary-user-1")
    async with session_factory() as session:
        await SqlAlchemyUserRepository(session).ensure_user(user_id)
        await SqlAlchemySessionRepository(session).ensure_session(user_id, session_id)
        message_repository = SqlAlchemyMessageRepository(session)
        first = await message_repository.create_user_message(
            user_id,
            session_id,
            "first user message",
        )
        second = await message_repository.create_assistant_message(
            user_id,
            session_id,
            "assistant response",
        )
        repository = SqlAlchemySummaryRepository(session)
        summary = RollingSummary(
            session_id=session_id,
            summary_version=1,
            covered_from=first.id,
            covered_to=second.id,
            current_problem="work communication stress",
            source_message_ids=[first.id, second.id],
        )

        await repository.save_version(user_id, summary)
        await session.commit()

    async with session_factory() as session:
        repository = SqlAlchemySummaryRepository(session)
        current = await repository.get_current(user_id, session_id)
        session_row = await session.scalar(
            select(SessionModel).where(
                SessionModel.user_id == str(user_id),
                SessionModel.session_id == str(session_id),
            )
        )
        rows = list(
            (
                await session.execute(select(RollingSummaryVersionModel))
            ).scalars()
        )

    assert current == summary
    assert session_row is not None
    assert session_row.current_summary_version == 1
    assert len(rows) == 1
    assert rows[0].summary_json["current_problem"] == "work communication stress"
    assert rows[0].covered_from_message_id == str(first.id)
    assert rows[0].covered_to_message_id == str(second.id)
