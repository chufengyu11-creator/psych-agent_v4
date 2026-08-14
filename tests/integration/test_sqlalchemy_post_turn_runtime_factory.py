"""Integration test for the SQLAlchemy post-turn summary runtime factory."""

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from runtime.factory import build_sqlalchemy_orchestrator_with_post_turn_summary
from schemas.common import SessionId, UserId
from storage.models.base import Base
from storage.models.registry import load_all_models
from storage.models.session import SessionModel
from storage.models.summary import RollingSummaryVersionModel


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


async def test_runtime_factory_automatically_summarizes_each_turn(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The DB runtime factory should wire post-turn work to session-bound repositories."""

    user_id = UserId("runtime-post-turn-user")
    session_id = SessionId("runtime-post-turn-session")
    orchestrator = build_sqlalchemy_orchestrator_with_post_turn_summary(session_factory)

    await orchestrator.handle_turn(user_id, session_id, "Work conversations feel tense.")
    await orchestrator.handle_turn(user_id, session_id, "I need a concrete next step.")

    async with session_factory() as session:
        rows = list((await session.execute(select(RollingSummaryVersionModel))).scalars())
        stored_session = await session.scalar(
            select(SessionModel).where(
                SessionModel.user_id == str(user_id),
                SessionModel.session_id == str(session_id),
            )
        )

    assert len(rows) >= 1
    assert stored_session is not None
    assert stored_session.current_summary_version >= 1
    assert rows[-1].summary_version == stored_session.current_summary_version
