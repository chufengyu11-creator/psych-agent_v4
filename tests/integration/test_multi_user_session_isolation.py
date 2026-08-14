"""Integration coverage for user-scoped session persistence."""

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from schemas.common import SessionId, UserId
from schemas.memory import (
    MemoryCandidate,
    MemoryOperation,
    MemoryPolicyDecision,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from storage.models.base import Base
from storage.models.memory import LongTermMemoryModel
from storage.models.message import MessageModel
from storage.models.registry import load_all_models
from storage.models.session import SessionModel
from storage.repositories.memory_repository import SqlAlchemyMemoryRepository
from storage.repositories.message_repository import SqlAlchemyMessageRepository
from storage.repositories.session_repository import SqlAlchemySessionRepository
from storage.repositories.user_repository import SqlAlchemyUserRepository


@pytest_asyncio.fixture
async def isolated_session_factory() -> AsyncIterator[
    async_sessionmaker[AsyncSession]
]:
    """Provide a clean SQLite schema for each isolation case."""

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
        await engine.dispose()


async def _ensure_user_and_session(
    session: AsyncSession,
    user_id: UserId,
    session_id: SessionId,
) -> None:
    await SqlAlchemyUserRepository(session).ensure_user(user_id)
    await SqlAlchemySessionRepository(session).ensure_session(user_id, session_id)


async def test_different_users_can_create_the_same_display_session_id(
    isolated_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A display session ID is unique only within one user."""

    user_a = UserId("A")
    user_b = UserId("B")
    session_id = SessionId("session-1")
    async with isolated_session_factory() as session:
        await _ensure_user_and_session(session, user_a, session_id)
        await _ensure_user_and_session(session, user_b, session_id)
        await SqlAlchemySessionRepository(session).ensure_session(user_a, session_id)

        rows = list(
            (
                await session.scalars(
                    select(SessionModel)
                    .where(SessionModel.session_id == str(session_id))
                    .order_by(SessionModel.user_id)
                )
            ).all()
        )

    assert [row.user_id for row in rows] == ["A", "B"]
    assert rows[0].id != rows[1].id


async def test_same_display_session_id_keeps_messages_isolated(
    isolated_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Messages are linked to each user's internal session UUID."""

    user_a = UserId("A")
    user_b = UserId("B")
    session_id = SessionId("session-1")
    async with isolated_session_factory() as session:
        await _ensure_user_and_session(session, user_a, session_id)
        await _ensure_user_and_session(session, user_b, session_id)
        messages = SqlAlchemyMessageRepository(session)
        await messages.create_user_message(user_a, session_id, "I am A")
        await messages.create_user_message(user_b, session_id, "I am B")

        recent_a = await messages.get_recent(user_a, session_id)
        recent_b = await messages.get_recent(user_b, session_id)
        rows = list((await session.scalars(select(MessageModel))).all())

    assert [message.content for message in recent_a] == ["I am A"]
    assert [message.content for message in recent_b] == ["I am B"]
    assert len({row.session_pk for row in rows}) == 2


async def test_long_term_memory_is_filtered_by_user_in_the_database(
    isolated_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One user's durable memory must never appear in another user's query."""

    user_a = UserId("A")
    user_b = UserId("B")
    session_id = SessionId("session-1")
    async with isolated_session_factory() as session:
        await _ensure_user_and_session(session, user_a, session_id)
        await _ensure_user_and_session(session, user_b, session_id)
        source = await SqlAlchemyMessageRepository(session).create_user_message(
            user_a,
            session_id,
            "I like basketball.",
        )
        candidate = MemoryCandidate(
            candidate_type=MemoryType.INTERACTION_PREFERENCE,
            content="Likes basketball.",
            source_message_ids=[source.id],
            source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
            confidence=0.95,
            requires_user_confirmation=False,
            sensitivity=MemorySensitivity.LOW,
            recommended_operation=MemoryOperation.CREATE,
        )
        decision = MemoryPolicyDecision(
            allowed=True,
            operation=MemoryOperation.CREATE,
            reason_codes=["isolation_test"],
        )
        memories = SqlAlchemyMemoryRepository(session)
        result = await memories.create(user_a, candidate, decision)
        active_a = await memories.list_active(user_a)
        active_b = await memories.list_active(user_b)
        user_b_rows = await session.scalar(
            select(func.count())
            .select_from(LongTermMemoryModel)
            .where(LongTermMemoryModel.user_id == str(user_b))
        )

    assert result.applied is True
    assert [memory.content for memory in active_a] == ["Likes basketball."]
    assert active_b == []
    assert user_b_rows == 0
