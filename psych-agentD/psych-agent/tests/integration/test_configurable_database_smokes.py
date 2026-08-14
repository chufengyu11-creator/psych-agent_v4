"""SQLite integration tests for backend-configurable business smoke scripts."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from scripts.database_smoke_support import (
    BusinessOutcome,
    ExpectedSmokeState,
    SmokeCase,
    SmokeExitCode,
    SmokeOptions,
    cleanup_smoke_rows,
    create_smoke_case,
    run_database_smoke,
)
from scripts.smoke_model_assisted_database_corpus import (
    run_business_flow as run_corpus_flow,
)
from scripts.smoke_session_close_memory import (
    run_business_flow as run_memory_flow,
)
from storage.database import (
    create_engine,
    create_session_factory,
    dispose_engine,
    transactional_session,
)
from storage.models.base import Base
from storage.models.memory import LongTermMemoryModel
from storage.models.registry import load_all_models
from storage.models.session import SessionModel
from storage.models.user import UserModel
from storage.repositories.session_repository import SqlAlchemySessionRepository
from storage.repositories.user_repository import SqlAlchemyUserRepository


@pytest.mark.asyncio
async def test_corpus_sqlite_smoke_counts_only_the_current_session() -> None:
    """Unrelated rows must not affect acceptance counts for the corpus smoke."""

    noise_cases: list[SmokeCase] = []

    async def corpus_with_unrelated_rows(
        session_factory: async_sessionmaker[AsyncSession],
        case: SmokeCase,
    ) -> BusinessOutcome:
        outcome = await run_corpus_flow(session_factory, case)
        noise = create_smoke_case("unrelated")
        noise_cases.append(noise)
        async with transactional_session(session_factory) as session:
            await SqlAlchemyUserRepository(session).ensure_user(noise.user_id)
            await SqlAlchemySessionRepository(session).ensure_session(
                noise.session_id,
                noise.user_id,
            )
        return outcome

    result, exit_code = await run_database_smoke(
        smoke_name="model_assisted_database_corpus_test",
        options=SmokeOptions(database="sqlite", cleanup=True),
        expected=ExpectedSmokeState(
            session_status="active",
            long_term_memories_rows=0,
        ),
        business_flow=corpus_with_unrelated_rows,
        settings=Settings(_env_file=None),
    )

    assert exit_code is SmokeExitCode.SUCCESS
    assert result.success is True
    assert result.database_backend == "sqlite"
    assert result.database_driver == "sqlite+aiosqlite"
    assert result.turns == 3
    assert result.users_rows == 1
    assert result.sessions_rows == 1
    assert result.messages_rows == 6
    assert result.session_state_versions_rows == 3
    assert result.intervention_events_rows == 3
    assert result.rolling_summary_versions_rows == 1
    assert result.long_term_memories_rows == 0
    assert result.session_status == "active"
    assert result.structured_llm_calls == 9
    assert result.summary_version == 1
    assert result.source_message_ids_non_empty is True
    assert result.cleanup_requested is True
    assert result.cleanup_applied is True
    assert noise_cases
    assert result.user_id != str(noise_cases[0].user_id)
    assert result.session_id != str(noise_cases[0].session_id)


@pytest.mark.asyncio
async def test_session_close_memory_sqlite_smoke_keeps_business_contract() -> None:
    """The close flow should still persist one sourced memory and close the session."""

    result, exit_code = await run_database_smoke(
        smoke_name="session_close_memory_test",
        options=SmokeOptions(database="sqlite"),
        expected=ExpectedSmokeState(
            session_status="closed",
            long_term_memories_rows=1,
        ),
        business_flow=run_memory_flow,
        settings=Settings(_env_file=None),
    )

    assert exit_code is SmokeExitCode.SUCCESS
    assert result.success is True
    assert result.turns == 3
    assert result.messages_rows == 6
    assert result.session_state_versions_rows == 3
    assert result.intervention_events_rows == 3
    assert result.rolling_summary_versions_rows == 1
    assert result.long_term_memories_rows == 1
    assert result.session_status == "closed"
    assert result.structured_llm_calls == 9
    assert result.summary_version == 1
    assert result.candidate_memories == 1
    assert result.memory_writes == 1
    assert result.source_message_ids_non_empty is True
    assert result.cleanup_requested is False
    assert result.cleanup_applied is False


@pytest.mark.asyncio
async def test_cleanup_removes_only_current_smoke_rows() -> None:
    """Explicit cleanup should satisfy FKs and preserve an unrelated user/session."""

    current = create_smoke_case("cleanup_current")
    database_path = Path(".tmp") / f"scoped_cleanup_{current.run_id}.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        _env_file=None,
    )
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    unrelated = create_smoke_case("cleanup_unrelated")
    try:
        load_all_models()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        await run_memory_flow(session_factory, current)
        async with transactional_session(session_factory) as session:
            await SqlAlchemyUserRepository(session).ensure_user(unrelated.user_id)
            await SqlAlchemySessionRepository(session).ensure_session(
                unrelated.session_id,
                unrelated.user_id,
            )

        await cleanup_smoke_rows(session_factory, current)

        async with session_factory() as session:
            assert await session.get(UserModel, str(current.user_id)) is None
            assert await session.get(SessionModel, str(current.session_id)) is None
            current_memories = await session.scalar(
                select(func.count())
                .select_from(LongTermMemoryModel)
                .where(LongTermMemoryModel.user_id == str(current.user_id))
            )
            assert current_memories == 0
            assert await session.get(UserModel, str(unrelated.user_id)) is not None
            assert await session.get(SessionModel, str(unrelated.session_id)) is not None
    finally:
        await dispose_engine(engine)
        database_path.unlink(missing_ok=True)
