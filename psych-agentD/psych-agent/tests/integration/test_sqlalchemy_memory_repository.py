"""Integration tests for SQLAlchemy long-term memory repository."""

from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from schemas.common import MemoryId, MessageId, SessionId, UserId
from schemas.memory import (
    MemoryCandidate,
    MemoryOperation,
    MemoryPolicyDecision,
    MemoryPolicyInput,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from services.memory_policy import MemoryPolicy
from storage.database import transactional_session
from storage.models.base import Base
from storage.models.memory import (
    MEMORY_STATUS_ACTIVE,
    MEMORY_STATUS_CONFLICTED,
    MEMORY_STATUS_DELETED,
    MEMORY_STATUS_EXPIRED,
    MEMORY_STATUS_SUPERSEDED,
    LongTermMemoryModel,
)
from storage.models.registry import load_all_models
from storage.repositories.memory_repository import SqlAlchemyMemoryRepository
from storage.repositories.message_repository import SqlAlchemyMessageRepository
from storage.repositories.session_repository import SqlAlchemySessionRepository
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


def _candidate(
    source_message_id: MessageId,
    *,
    content: str = "User prefers one small step at a time.",
    operation: MemoryOperation = MemoryOperation.CREATE,
) -> MemoryCandidate:
    """Build one explicit low-sensitivity preference candidate."""

    return MemoryCandidate(
        candidate_type=MemoryType.INTERACTION_PREFERENCE,
        content=content,
        source_message_ids=[source_message_id],
        source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
        confidence=0.94,
        requires_user_confirmation=False,
        sensitivity=MemorySensitivity.LOW,
        recommended_operation=operation,
    )


async def _setup_candidate(
    session: AsyncSession,
    user_id: UserId,
) -> MemoryCandidate:
    """Create an owned source message and return a candidate citing it."""

    session_id = SessionId(f"session-{user_id}")
    await SqlAlchemyUserRepository(session).ensure_user(user_id)
    await SqlAlchemySessionRepository(session).ensure_session(session_id, user_id)
    source = await SqlAlchemyMessageRepository(session).create_user_message(
        session_id,
        "Artificial source message for memory repository testing.",
    )
    return _candidate(source.id)


async def _create_source_message(
    session: AsyncSession,
    user_id: UserId,
    suffix: str,
) -> MessageId:
    """Create one artificial source message owned by a user."""

    session_id = SessionId(f"session-{user_id}")
    await SqlAlchemyUserRepository(session).ensure_user(user_id)
    await SqlAlchemySessionRepository(session).ensure_session(session_id, user_id)
    message = await SqlAlchemyMessageRepository(session).create_user_message(
        session_id,
        f"Artificial source message {suffix}.",
    )
    return message.id


def _decision(
    operation: MemoryOperation,
    *,
    target_memory_id: MemoryId | None = None,
    sanitized_content: str | None = None,
    requires_user_confirmation: bool = False,
) -> MemoryPolicyDecision:
    """Build an explicitly approved decision for repository boundary tests."""

    return MemoryPolicyDecision(
        allowed=True,
        operation=operation,
        target_memory_id=target_memory_id,
        sanitized_content=sanitized_content,
        requires_user_confirmation=requires_user_confirmation,
        reason_codes=["repository_test"],
    )


async def test_sqlalchemy_memory_repository_creates_policy_allowed_memory(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A policy-allowed candidate should become an active memory row."""

    user_id = UserId("memory-user-1")
    async with session_factory() as session:
        candidate = await _setup_candidate(session, user_id)
        repository = SqlAlchemyMemoryRepository(session)
        existing = await repository.list_active(user_id)
        decision = MemoryPolicy().evaluate_candidate(
            MemoryPolicyInput(
                candidate=candidate,
                existing_memories=existing,
                user_memory_enabled=True,
            )
        )

        result = await repository.create(user_id, candidate, decision)
        await session.commit()

    async with session_factory() as session:
        rows = list((await session.execute(select(LongTermMemoryModel))).scalars())
        active = await SqlAlchemyMemoryRepository(session).list_active(user_id)

    assert result.applied is True
    assert result.operation == MemoryOperation.CREATE
    assert result.memory_id is not None
    assert len(rows) == 1
    assert rows[0].status == "active"
    assert rows[0].content == candidate.content
    assert rows[0].source_message_ids == [str(candidate.source_message_ids[0])]
    assert len(active) == 1
    assert active[0].id == result.memory_id
    assert active[0].content == candidate.content


async def test_sqlalchemy_memory_repository_reinforces_duplicate_memory(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Duplicate candidates should reinforce the existing active memory."""

    user_id = UserId("memory-user-2")
    async with session_factory() as session:
        candidate = await _setup_candidate(session, user_id)
        repository = SqlAlchemyMemoryRepository(session)
        first_decision = MemoryPolicy().evaluate_candidate(
            MemoryPolicyInput(candidate=candidate, user_memory_enabled=True)
        )
        await repository.create(user_id, candidate, first_decision)

        existing = await repository.list_active(user_id)
        second_decision = MemoryPolicy().evaluate_candidate(
            MemoryPolicyInput(
                candidate=candidate,
                existing_memories=existing,
                user_memory_enabled=True,
            )
        )
        result = await repository.create(user_id, candidate, second_decision)
        await session.commit()

    async with session_factory() as session:
        rows = list((await session.execute(select(LongTermMemoryModel))).scalars())

    assert result.applied is True
    assert result.operation == MemoryOperation.REINFORCE
    assert len(rows) == 1
    assert rows[0].reinforcement_count == 1
    assert rows[0].last_reinforced_at is not None


@pytest.mark.parametrize(
    ("sanitized_content", "expected_content"),
    [
        ("Sanitized durable preference.", "Sanitized durable preference."),
        (None, "Original artificial preference."),
    ],
)
async def test_create_uses_sanitized_content_with_explicit_none_fallback(
    session_factory: async_sessionmaker[AsyncSession],
    sanitized_content: str | None,
    expected_content: str,
) -> None:
    """Only None should fall back to the candidate's original content."""

    user_id = UserId("memory-sanitized-user")
    async with session_factory() as session:
        source_id = await _create_source_message(session, user_id, "sanitized")
        candidate = _candidate(
            source_id,
            content="Original artificial preference.",
        )
        result = await SqlAlchemyMemoryRepository(session).create(
            user_id,
            candidate,
            _decision(
                MemoryOperation.CREATE,
                sanitized_content=sanitized_content,
            ),
        )
        row = await session.get(LongTermMemoryModel, str(result.memory_id))

    assert result.applied is True
    assert row is not None
    assert row.content == expected_content
    assert row.source_message_ids == [str(source_id)]


@pytest.mark.parametrize("sanitized_content", ["", "   \t"])
async def test_blank_sanitized_content_is_not_persisted(
    session_factory: async_sessionmaker[AsyncSession],
    sanitized_content: str,
) -> None:
    """An approved decision must not create an empty or whitespace memory."""

    user_id = UserId("memory-blank-user")
    async with session_factory() as session:
        source_id = await _create_source_message(session, user_id, "blank")
        candidate = _candidate(source_id)
        result = await SqlAlchemyMemoryRepository(session).create(
            user_id,
            candidate,
            _decision(
                MemoryOperation.CREATE,
                sanitized_content=sanitized_content,
            ),
        )
        count = await session.scalar(
            select(func.count()).select_from(LongTermMemoryModel)
        )

    assert result.applied is False
    assert result.memory_id is None
    assert result.reason_codes == ["repository_test"]
    assert count == 0


async def test_reinforce_uses_owned_target_sanitized_content_and_all_sources(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reinforcement should update one owned target and merge traceability."""

    user_id = UserId("memory-reinforce-owner")
    async with session_factory() as session:
        first_source = await _create_source_message(session, user_id, "first")
        second_source = await _create_source_message(session, user_id, "second")
        repository = SqlAlchemyMemoryRepository(session)
        first = await repository.create(
            user_id,
            _candidate(first_source),
            _decision(MemoryOperation.CREATE),
        )
        assert first.memory_id is not None

        result = await repository.create(
            user_id,
            _candidate(
                second_source,
                content="Unsanitized reinforcement.",
                operation=MemoryOperation.REINFORCE,
            ),
            _decision(
                MemoryOperation.REINFORCE,
                target_memory_id=first.memory_id,
                sanitized_content="Sanitized reinforced preference.",
            ),
        )
        row = await session.get(LongTermMemoryModel, str(first.memory_id))

    assert result.applied is True
    assert row is not None
    assert row.content == "Sanitized reinforced preference."
    assert row.reinforcement_count == 1
    assert row.source_message_ids == [str(first_source), str(second_source)]


async def test_supersede_validates_target_before_creating_sanitized_replacement(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A valid owned target should be replaced without losing source IDs."""

    user_id = UserId("memory-supersede-owner")
    async with session_factory() as session:
        first_source = await _create_source_message(session, user_id, "old")
        replacement_source = await _create_source_message(
            session,
            user_id,
            "replacement",
        )
        repository = SqlAlchemyMemoryRepository(session)
        first = await repository.create(
            user_id,
            _candidate(first_source),
            _decision(MemoryOperation.CREATE),
        )
        assert first.memory_id is not None
        result = await repository.create(
            user_id,
            _candidate(
                replacement_source,
                content="Unsanitized replacement.",
                operation=MemoryOperation.SUPERSEDE,
            ),
            _decision(
                MemoryOperation.SUPERSEDE,
                target_memory_id=first.memory_id,
                sanitized_content="Sanitized replacement.",
            ),
        )
        old_row = await session.get(LongTermMemoryModel, str(first.memory_id))
        new_row = await session.get(LongTermMemoryModel, str(result.memory_id))

    assert result.applied is True
    assert old_row is not None
    assert old_row.status == MEMORY_STATUS_SUPERSEDED
    assert new_row is not None
    assert new_row.user_id == str(user_id)
    assert new_row.content == "Sanitized replacement."
    assert new_row.source_message_ids == [str(replacement_source)]
    assert new_row.supersedes_memory_id == str(first.memory_id)


@pytest.mark.parametrize(
    "operation",
    [
        MemoryOperation.REINFORCE,
        MemoryOperation.SUPERSEDE,
        MemoryOperation.DELETE,
    ],
)
async def test_cross_user_target_operations_do_not_disclose_or_mutate_memory(
    session_factory: async_sessionmaker[AsyncSession],
    operation: MemoryOperation,
) -> None:
    """A foreign target should look absent and remain entirely unchanged."""

    owner_id = UserId(f"memory-owner-{operation.value.lower()}")
    attacker_id = UserId(f"memory-other-{operation.value.lower()}")
    async with session_factory() as session:
        owner_source = await _create_source_message(session, owner_id, "owner")
        attacker_source = await _create_source_message(session, attacker_id, "other")
        repository = SqlAlchemyMemoryRepository(session)
        first = await repository.create(
            owner_id,
            _candidate(owner_source),
            _decision(MemoryOperation.CREATE),
        )
        assert first.memory_id is not None
        target_before = await session.get(LongTermMemoryModel, str(first.memory_id))
        assert target_before is not None
        original_content = target_before.content
        original_status = target_before.status
        original_reinforcement_count = target_before.reinforcement_count

        result = await repository.create(
            attacker_id,
            _candidate(
                attacker_source,
                operation=operation,
            ),
            _decision(
                operation,
                target_memory_id=first.memory_id,
                sanitized_content="Foreign sanitized content.",
            ),
        )
        await session.refresh(target_before)
        attacker_memories = await repository.list_active(attacker_id)

    assert result.applied is False
    assert result.memory_id is None
    assert target_before.content == original_content
    assert target_before.status == original_status
    assert target_before.reinforcement_count == original_reinforcement_count
    assert attacker_memories == []


@pytest.mark.parametrize(
    ("operation", "expected_status"),
    [
        (MemoryOperation.MARK_CONFLICT, MEMORY_STATUS_CONFLICTED),
        (MemoryOperation.EXPIRE, MEMORY_STATUS_EXPIRED),
        (MemoryOperation.DELETE, MEMORY_STATUS_DELETED),
    ],
)
async def test_owned_lifecycle_target_operations_are_applied(
    session_factory: async_sessionmaker[AsyncSession],
    operation: MemoryOperation,
    expected_status: str,
) -> None:
    """Known logical revoke operations should mutate only an owned target."""

    user_id = UserId(f"memory-lifecycle-{operation.value.lower()}")
    async with session_factory() as session:
        source_id = await _create_source_message(session, user_id, "lifecycle")
        repository = SqlAlchemyMemoryRepository(session)
        first = await repository.create(
            user_id,
            _candidate(source_id),
            _decision(MemoryOperation.CREATE),
        )
        assert first.memory_id is not None
        result = await repository.create(
            user_id,
            _candidate(source_id, operation=operation),
            _decision(operation, target_memory_id=first.memory_id),
        )
        row = await session.get(LongTermMemoryModel, str(first.memory_id))

    assert result.applied is True
    assert row is not None
    assert row.status == expected_status


@pytest.mark.parametrize(
    "operation",
    [
        MemoryOperation.REINFORCE,
        MemoryOperation.SUPERSEDE,
        MemoryOperation.DELETE,
    ],
)
async def test_target_operations_require_target_and_create_rejects_target(
    session_factory: async_sessionmaker[AsyncSession],
    operation: MemoryOperation,
) -> None:
    """Operation/target mismatches should fail before writing any row."""

    user_id = UserId(f"memory-contract-{operation.value.lower()}")
    async with session_factory() as session:
        source_id = await _create_source_message(session, user_id, "contract")
        repository = SqlAlchemyMemoryRepository(session)
        missing_target = await repository.create(
            user_id,
            _candidate(source_id, operation=operation),
            _decision(operation),
        )
        nonexistent_target = await repository.create(
            user_id,
            _candidate(source_id, operation=operation),
            _decision(
                operation,
                target_memory_id=MemoryId("missing-memory-target"),
            ),
        )
        create_with_target = await repository.create(
            user_id,
            _candidate(source_id),
            _decision(
                MemoryOperation.CREATE,
                target_memory_id=MemoryId("foreign-or-missing-memory"),
            ),
        )
        count = await session.scalar(
            select(func.count()).select_from(LongTermMemoryModel)
        )

    assert missing_target.applied is False
    assert nonexistent_target.applied is False
    assert nonexistent_target.memory_id is None
    assert create_with_target.applied is False
    assert count == 0


async def test_source_messages_must_belong_to_current_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A user cannot persist another user's message ID as memory evidence."""

    owner_id = UserId("memory-source-owner")
    other_id = UserId("memory-source-other")
    async with session_factory() as session:
        foreign_source = await _create_source_message(session, other_id, "foreign")
        await _create_source_message(session, owner_id, "owned")
        result = await SqlAlchemyMemoryRepository(session).create(
            owner_id,
            _candidate(foreign_source),
            _decision(MemoryOperation.CREATE),
        )
        count = await session.scalar(
            select(func.count()).select_from(LongTermMemoryModel)
        )

    assert result.applied is False
    assert count == 0


class ExpectedSupersedeFailure(RuntimeError):
    """Expected failure used to verify outer atomic rollback."""


async def test_supersede_failure_rolls_back_old_and_new_memory(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure after the multi-row flush must leave no replacement orphan."""

    user_id = UserId("memory-atomic-owner")
    async with transactional_session(session_factory) as session:
        old_source = await _create_source_message(session, user_id, "old")
        new_source = await _create_source_message(session, user_id, "new")
        first = await SqlAlchemyMemoryRepository(session).create(
            user_id,
            _candidate(old_source),
            _decision(MemoryOperation.CREATE),
        )
        assert first.memory_id is not None

    with pytest.raises(ExpectedSupersedeFailure):
        async with transactional_session(session_factory) as session:
            original_flush = session.flush

            async def flush_then_fail(
                objects: Sequence[object] | None = None,
            ) -> None:
                await original_flush(objects)
                raise ExpectedSupersedeFailure("force supersede rollback")

            monkeypatch.setattr(session, "flush", flush_then_fail)
            await SqlAlchemyMemoryRepository(session).create(
                user_id,
                _candidate(new_source, operation=MemoryOperation.SUPERSEDE),
                _decision(
                    MemoryOperation.SUPERSEDE,
                    target_memory_id=first.memory_id,
                    sanitized_content="Atomic sanitized replacement.",
                ),
            )

    async with session_factory() as session:
        rows = list((await session.scalars(select(LongTermMemoryModel))).all())

    assert len(rows) == 1
    assert rows[0].id == str(first.memory_id)
    assert rows[0].status == MEMORY_STATUS_ACTIVE
