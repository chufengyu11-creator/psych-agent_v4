"""Explicit asyncpg, Alembic, repository, and constraint integration tests."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from schemas.common import utc_now
from schemas.intervention import InterventionRecord
from schemas.memory import (
    LongTermMemory,
    MemoryCandidate,
    MemoryOperation,
    MemoryPolicyInput,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from schemas.messages import Message, MessageRole
from schemas.state import SessionState
from schemas.summary import RollingSummary
from scripts.check_database_connection import (
    CheckOptions,
    DatabaseExitCode,
    run_database_checks,
)
from services.memory_policy import MemoryPolicy
from storage.database import transactional_session
from storage.models.intervention import InterventionEventModel
from storage.models.message import MessageModel
from storage.models.session import SessionModel
from storage.models.session_state import SessionStateVersionModel
from storage.models.user import UserModel
from storage.repositories.intervention_repository import (
    SqlAlchemyInterventionRepository,
)
from storage.repositories.memory_repository import SqlAlchemyMemoryRepository
from storage.repositories.message_repository import SqlAlchemyMessageRepository
from storage.repositories.session_repository import SqlAlchemySessionRepository
from storage.repositories.state_repository import SqlAlchemyStateRepository
from storage.repositories.summary_repository import SqlAlchemySummaryRepository
from storage.repositories.user_repository import SqlAlchemyUserRepository
from tests.integration.postgres.conftest import PostgresCase

pytestmark = pytest.mark.postgres


async def test_postgres_connection_alembic_tables_and_schema_objects(
    postgres_test_settings: Settings,
) -> None:
    """A migrated test database should match the single Alembic head."""

    settings = postgres_test_settings.model_copy(
        update={"test_database_url": postgres_test_settings.database_url}
    )
    result, exit_code = await run_database_checks(
        settings,
        CheckOptions(url_env="TEST_DATABASE_URL"),
    )

    assert exit_code is DatabaseExitCode.SUCCESS
    assert result.connection_status == "ok"
    assert result.database_type == "postgresql"
    assert result.schema_status == "ok"
    assert result.alembic_current_revision == result.expected_head_revision
    assert all(result.tables.values())
    assert all(result.schema_objects.values())


async def test_postgres_repository_commit_and_round_trip(
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_case: PostgresCase,
) -> None:
    """All seven repositories should commit JSONB and typed results through asyncpg."""

    user_id = postgres_case.user_id
    session_id = postgres_case.session_id
    async with transactional_session(postgres_session_factory) as session:
        user_repository = SqlAlchemyUserRepository(session)
        session_repository = SqlAlchemySessionRepository(session)
        message_repository = SqlAlchemyMessageRepository(session)
        await user_repository.ensure_user(user_id)
        await session_repository.ensure_session(session_id, user_id)
        user_row = await session.get(UserModel, str(user_id))
        assert user_row is not None
        user_row.memory_enabled = True

        first = await message_repository.create_user_message(
            session_id,
            "Artificial PostgreSQL repository test message.",
        )
        second = await message_repository.create_assistant_message(
            session_id,
            "Artificial repository response.",
        )
        state = SessionState(
            session_id=session_id,
            version=1,
            session_goal="Verify PostgreSQL JSONB round-trip.",
        )
        await SqlAlchemyStateRepository(session).save_version(
            state,
            source_message_id=first.id,
        )
        intervention = await SqlAlchemyInterventionRepository(session).create_pending(
            session_id,
            second.id,
            "reflective_listening",
            "verify PostgreSQL persistence",
            ["typed result returned"],
        )
        summary = RollingSummary(
            session_id=session_id,
            summary_version=1,
            covered_from=first.id,
            covered_to=second.id,
            current_problem="Artificial database integration case.",
            source_message_ids=[first.id, second.id],
        )
        await SqlAlchemySummaryRepository(session).save_version(summary)
        candidate = MemoryCandidate(
            candidate_type=MemoryType.INTERACTION_PREFERENCE,
            content="Artificial preference used only for a PostgreSQL test.",
            source_message_ids=[first.id],
            source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
            confidence=0.9,
            requires_user_confirmation=False,
            sensitivity=MemorySensitivity.LOW,
            recommended_operation=MemoryOperation.CREATE,
        )
        decision = MemoryPolicy().evaluate_candidate(
            MemoryPolicyInput(
                candidate=candidate,
                user_memory_enabled=True,
            )
        )
        memory_result = await SqlAlchemyMemoryRepository(session).create(
            user_id,
            candidate,
            decision,
        )

    async with postgres_session_factory() as session:
        assert await SqlAlchemyUserRepository(session).exists(user_id)
        assert await SqlAlchemySessionRepository(session).exists(session_id)
        recent = await SqlAlchemyMessageRepository(session).get_recent(session_id)
        current_state = await SqlAlchemyStateRepository(session).get_current(session_id)
        pending = await SqlAlchemyInterventionRepository(session).get_pending(session_id)
        current_summary = await SqlAlchemySummaryRepository(session).get_current(
            session_id
        )
        memories = await SqlAlchemyMemoryRepository(session).list_active(user_id)
        first_row = await session.get(MessageModel, str(first.id))
        state_row = await session.scalar(
            select(SessionStateVersionModel).where(
                SessionStateVersionModel.session_id == str(session_id)
            )
        )

    assert memory_result.applied is True
    assert memory_result.memory_id is not None
    assert all(isinstance(item, Message) for item in recent)
    assert [item.role for item in recent] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert current_state == state
    assert isinstance(pending, InterventionRecord)
    assert pending == intervention
    assert current_summary == summary
    assert len(memories) == 1
    assert isinstance(memories[0], LongTermMemory)
    assert memories[0].source[0].message_id == first.id
    assert first_row is not None
    assert first_row.created_at.tzinfo is not None
    assert state_row is not None
    assert state_row.source_message_id == str(first.id)


class ExpectedRollbackError(RuntimeError):
    """Expected exception proving outer transaction rollback."""


async def test_postgres_transactional_session_rolls_back(
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_case: PostgresCase,
) -> None:
    """An exception after repository flushes must roll back every row."""

    with pytest.raises(ExpectedRollbackError):
        async with transactional_session(postgres_session_factory) as session:
            await SqlAlchemyUserRepository(session).ensure_user(postgres_case.user_id)
            await SqlAlchemySessionRepository(session).ensure_session(
                postgres_case.session_id,
                postgres_case.user_id,
            )
            raise ExpectedRollbackError("force PostgreSQL rollback")

    async with postgres_session_factory() as session:
        assert not await SqlAlchemyUserRepository(session).exists(postgres_case.user_id)
        assert not await SqlAlchemySessionRepository(session).exists(
            postgres_case.session_id
        )


async def test_postgres_foreign_unique_and_partial_unique_constraints(
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_case: PostgresCase,
) -> None:
    """PostgreSQL must enforce foreign, compound unique, and partial unique rules."""

    with pytest.raises(IntegrityError):
        async with transactional_session(postgres_session_factory) as session:
            session.add(
                SessionModel(
                    id=str(postgres_case.session_id),
                    user_id=str(postgres_case.user_id),
                )
            )
            await session.flush()

    async with transactional_session(postgres_session_factory) as session:
        await SqlAlchemyUserRepository(session).ensure_user(postgres_case.user_id)
        await SqlAlchemySessionRepository(session).ensure_session(
            postgres_case.session_id,
            postgres_case.user_id,
        )
        message_repository = SqlAlchemyMessageRepository(session)
        first = await message_repository.create_assistant_message(
            postgres_case.session_id,
            "First artificial assistant message.",
        )
        second = await message_repository.create_assistant_message(
            postgres_case.session_id,
            "Second artificial assistant message.",
        )

    with pytest.raises(IntegrityError):
        async with transactional_session(postgres_session_factory) as session:
            session.add(
                MessageModel(
                    id=f"pg-duplicate-{postgres_case.session_id}",
                    session_id=str(postgres_case.session_id),
                    role=MessageRole.USER.value,
                    content="Duplicate sequence number.",
                    sequence_number=1,
                    created_at=utc_now(),
                )
            )
            await session.flush()

    with pytest.raises(IntegrityError):
        async with transactional_session(postgres_session_factory) as session:
            session.add_all(
                [
                    InterventionEventModel(
                        id=f"pg-int-a-{postgres_case.user_id}",
                        session_id=str(postgres_case.session_id),
                        assistant_message_id=str(first.id),
                        strategy="reflective_listening",
                        objective="first pending intervention",
                        expected_signals=[],
                        status="pending",
                    ),
                    InterventionEventModel(
                        id=f"pg-int-b-{postgres_case.user_id}",
                        session_id=str(postgres_case.session_id),
                        assistant_message_id=str(second.id),
                        strategy="collaborative_problem_solving",
                        objective="second pending intervention",
                        expected_signals=[],
                        status="pending",
                    ),
                ]
            )
            await session.flush()
