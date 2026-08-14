"""Integration tests for the transactional SQLAlchemy session closer."""

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from runtime.factory import build_sqlalchemy_session_closer
from schemas.common import SessionId, UserId
from storage.models.base import Base
from storage.models.memory import LongTermMemoryModel
from storage.models.registry import load_all_models
from storage.models.session import SESSION_STATUS_CLOSED, SessionModel
from storage.models.summary import RollingSummaryVersionModel
from storage.models.user import UserModel
from storage.repositories.message_repository import SqlAlchemyMessageRepository
from storage.repositories.session_repository import SqlAlchemySessionRepository
from storage.repositories.user_repository import SqlAlchemyUserRepository


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


async def _create_session_with_preference(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UserId,
    session_id: SessionId,
    *,
    memory_enabled: bool = True,
) -> None:
    """Persist an owned session with an explicit user interaction preference."""

    async with session_factory() as session:
        await SqlAlchemyUserRepository(session).ensure_user(user_id)
        user = await session.get(UserModel, str(user_id))
        assert user is not None
        user.memory_enabled = memory_enabled
        await SqlAlchemySessionRepository(session).ensure_session(user_id, session_id)
        messages = SqlAlchemyMessageRepository(session)
        await messages.create_user_message(
            user_id,
            session_id,
            "我希望每次一个小步骤，不要一次太多建议。",
        )
        await messages.create_assistant_message(
            user_id,
            session_id,
            "我们就从一个小步骤开始。",
        )
        await session.commit()


async def test_session_closer_summarizes_writes_memory_and_closes_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Default close runtime should complete the full transactional workflow."""

    user_id = UserId("closer-user")
    session_id = SessionId("closer-session")
    await _create_session_with_preference(session_factory, user_id, session_id)

    result = await build_sqlalchemy_session_closer(session_factory).close_session(
        user_id=user_id,
        session_id=session_id,
        reason="integration-test",
    )

    async with session_factory() as session:
        stored_session = await session.scalar(
            select(SessionModel).where(
                SessionModel.user_id == str(user_id),
                SessionModel.session_id == str(session_id),
            )
        )
        memories = list((await session.execute(select(LongTermMemoryModel))).scalars())
        summaries = list(
            (await session.execute(select(RollingSummaryVersionModel))).scalars()
        )

    assert result.candidate_memory_count == 1
    assert result.memory_write_count == 1
    assert stored_session is not None
    assert stored_session.status == SESSION_STATUS_CLOSED
    assert len(memories) == 1
    assert memories[0].source_message_ids
    assert len(summaries) >= 1


async def test_session_closer_can_skip_summary_without_skipping_memory_or_close(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Disabling summary should leave memory and close behavior intact."""

    user_id = UserId("closer-no-summary-user")
    session_id = SessionId("closer-no-summary-session")
    await _create_session_with_preference(session_factory, user_id, session_id)

    result = await build_sqlalchemy_session_closer(
        session_factory,
        ensure_summary=False,
    ).close_session(user_id=user_id, session_id=session_id)

    async with session_factory() as session:
        stored_session = await session.scalar(
            select(SessionModel).where(
                SessionModel.user_id == str(user_id),
                SessionModel.session_id == str(session_id),
            )
        )
        memories = list((await session.execute(select(LongTermMemoryModel))).scalars())
        summaries = list(
            (await session.execute(select(RollingSummaryVersionModel))).scalars()
        )

    assert result.memory_write_count == 1
    assert stored_session is not None
    assert stored_session.status == SESSION_STATUS_CLOSED
    assert len(memories) == 1
    assert summaries == []


async def test_session_closer_respects_disabled_user_memory(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Default-disabled user memory should prevent long-term memory writes."""

    user_id = UserId("closer-memory-disabled-user")
    session_id = SessionId("closer-memory-disabled-session")
    await _create_session_with_preference(
        session_factory,
        user_id,
        session_id,
        memory_enabled=False,
    )

    result = await build_sqlalchemy_session_closer(session_factory).close_session(
        user_id=user_id,
        session_id=session_id,
        reason="integration-test",
    )

    async with session_factory() as session:
        stored_session = await session.scalar(
            select(SessionModel).where(
                SessionModel.user_id == str(user_id),
                SessionModel.session_id == str(session_id),
            )
        )
        memories = list((await session.execute(select(LongTermMemoryModel))).scalars())

    assert result.candidate_memory_count == 1
    assert result.memory_write_count == 0
    assert stored_session is not None
    assert stored_session.status == SESSION_STATUS_CLOSED
    assert memories == []
