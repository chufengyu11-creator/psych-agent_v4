"""Integration tests for database-backed turn persistence."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from runtime.factory import build_sqlalchemy_orchestrator
from schemas.common import MessageId, SessionId, UserId
from schemas.intervention import InterventionStatus
from schemas.state import SessionState
from storage.database import transactional_session
from storage.models.base import Base
from storage.models.intervention import InterventionEventModel
from storage.models.message import MessageModel
from storage.models.registry import load_all_models
from storage.models.session import SessionModel
from storage.models.session_state import SessionStateVersionModel
from storage.models.user import UserModel
from storage.repositories.intervention_repository import SqlAlchemyInterventionRepository
from storage.repositories.message_repository import SqlAlchemyMessageRepository
from storage.repositories.session_repository import SqlAlchemySessionRepository
from storage.repositories.state_repository import SqlAlchemyStateRepository
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


async def test_sqlalchemy_turn_flow_persists_core_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One user turn should persist user, session, messages, state, and intervention."""

    orchestrator = build_sqlalchemy_orchestrator(session_factory)

    result = await orchestrator.handle_turn(
        user_id=UserId("db-user-1"),
        session_id=SessionId("db-session-1"),
        text="I feel stuck at work and want to talk it through.",
    )

    assert result.status == "ok"
    assert result.state_version == 1
    async with session_factory() as session:
        stored_user = await session.get(UserModel, "db-user-1")
        stored_session = await session.get(SessionModel, "db-session-1")
        messages = list(
            (
                await session.execute(
                    select(MessageModel).order_by(MessageModel.sequence_number)
                )
            ).scalars()
        )
        state_versions = list(
            (
                await session.execute(select(SessionStateVersionModel))
            ).scalars()
        )
        interventions = list(
            (
                await session.execute(select(InterventionEventModel))
            ).scalars()
        )

    assert stored_user is not None
    assert stored_session is not None
    assert stored_session.user_id == "db-user-1"
    assert stored_session.current_state_version == 1
    assert stored_session.next_message_sequence == 3
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].content == "I feel stuck at work and want to talk it through."
    assert messages[0].sequence_number == 1
    assert messages[1].id == str(result.message_id)
    assert messages[1].sequence_number == 2
    assert len(state_versions) == 1
    assert state_versions[0].version == 1
    assert state_versions[0].source_message_id == messages[0].id
    assert state_versions[0].state_json["session_id"] == "db-session-1"
    assert state_versions[0].state_json["version"] == 1
    assert len(interventions) == 1
    assert interventions[0].session_id == "db-session-1"
    assert interventions[0].assistant_message_id == str(result.message_id)
    assert interventions[0].status == "pending"


async def test_sqlalchemy_turn_flow_reads_previous_pending_intervention(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second DB-backed turn should read and complete the previous intervention."""

    orchestrator = build_sqlalchemy_orchestrator(session_factory)
    await orchestrator.handle_turn(
        user_id=UserId("db-user-2"),
        session_id=SessionId("db-session-2"),
        text="I feel stuck at work and want to talk it through.",
    )

    result = await orchestrator.handle_turn(
        user_id=UserId("db-user-2"),
        session_id=SessionId("db-session-2"),
        text="I do not want more reflection, I need concrete next steps.",
    )

    assert result.state_version == 2
    async with session_factory() as session:
        messages = list(
            (
                await session.execute(
                    select(MessageModel).order_by(MessageModel.sequence_number)
                )
            ).scalars()
        )
        state_versions = list(
            (
                await session.execute(
                    select(SessionStateVersionModel).order_by(
                        SessionStateVersionModel.version
                    )
                )
            ).scalars()
        )
        interventions = list(
            (await session.execute(select(InterventionEventModel))).scalars()
        )
        intervention_records = await SqlAlchemyInterventionRepository(
            session
        ).list_for_session(SessionId("db-session-2"))

    assert len(messages) == 4
    assert [message.sequence_number for message in messages] == [1, 2, 3, 4]
    assert [state.version for state in state_versions] == [1, 2]
    assert [state.source_message_id for state in state_versions] == [
        messages[0].id,
        messages[2].id,
    ]
    assert len(interventions) == 2
    assert {intervention.status for intervention in interventions} == {
        "evaluated",
        "pending",
    }
    assert [record.status for record in intervention_records] == [
        InterventionStatus.EVALUATED,
        InterventionStatus.PENDING,
    ]
    evaluated = next(
        intervention for intervention in interventions if intervention.status == "evaluated"
    )
    assert evaluated.observed_response is not None


async def test_state_repository_optionally_persists_source_message_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Old state writes should stay nullable while explicit sources are retained."""

    user_id = UserId("state-source-user")
    session_id = SessionId("state-source-session")
    async with session_factory() as session:
        await SqlAlchemyUserRepository(session).ensure_user(user_id)
        await SqlAlchemySessionRepository(session).ensure_session(session_id, user_id)
        source = await SqlAlchemyMessageRepository(session).create_user_message(
            session_id,
            "Artificial state source message.",
        )
        repository = SqlAlchemyStateRepository(session)
        await repository.save_version(SessionState(session_id=session_id, version=1))
        await repository.save_version(
            SessionState(session_id=session_id, version=2),
            source_message_id=source.id,
        )
        rows = list(
            (
                await session.scalars(
                    select(SessionStateVersionModel).order_by(
                        SessionStateVersionModel.version
                    )
                )
            ).all()
        )

    assert [row.source_message_id for row in rows] == [None, str(source.id)]


async def test_state_source_message_id_keeps_existing_foreign_key_contract(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An explicit nonexistent source should fail when SQLite FKs are enabled."""

    user_id = UserId("state-fk-user")
    session_id = SessionId("state-fk-session")
    with pytest.raises(IntegrityError):
        async with transactional_session(session_factory) as session:
            await session.execute(text("PRAGMA foreign_keys=ON"))
            await SqlAlchemyUserRepository(session).ensure_user(user_id)
            await SqlAlchemySessionRepository(session).ensure_session(
                session_id,
                user_id,
            )
            await SqlAlchemyStateRepository(session).save_version(
                SessionState(session_id=session_id, version=1),
                source_message_id=MessageId("missing-state-source"),
            )

    async with session_factory() as session:
        assert await session.get(SessionModel, str(session_id)) is None
