"""Async SQLite tests for persistent user and session repositories."""

from collections.abc import AsyncIterator, Awaitable, Sequence
from datetime import UTC
from inspect import getsource
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from schemas.common import SessionId, UserId
from storage.database import transactional_session
from storage.models.base import Base
from storage.models.registry import load_all_models
from storage.models.session import (
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_CANCELLED,
    SESSION_STATUS_CLOSED,
    SessionModel,
)
from storage.models.user import (
    USER_STATUS_ACTIVE,
    USER_STATUS_DELETED,
    USER_STATUS_DISABLED,
    UserModel,
)
from storage.repositories.errors import (
    SessionNotFoundError,
    SessionOwnershipError,
    SessionUnavailableError,
    UserNotFoundError,
    UserUnavailableError,
)
from storage.repositories.session_repository import (
    SqlAlchemySessionRepository,
)
from storage.repositories.user_repository import SqlAlchemyUserRepository


class ExpectedTransactionError(RuntimeError):
    """Expected failure used to verify outer transaction rollback."""


async def _assert_returns_none(operation: Awaitable[None]) -> None:
    """Assert a typed write operation returns no boundary value at runtime."""

    result = await cast(Awaitable[object | None], operation)
    assert result is None


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide an isolated asynchronous in-memory SQLite database."""

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


async def _add_user(
    session: AsyncSession,
    user_id: UserId,
    *,
    status: str = USER_STATUS_ACTIVE,
) -> UserModel:
    """Add and flush one user for repository setup."""

    stored_user = UserModel(id=str(user_id), status=status)
    session.add(stored_user)
    await session.flush()
    return stored_user


async def _ensure_user_and_session(
    session: AsyncSession,
    user_id: UserId,
    session_id: SessionId,
) -> SessionModel:
    """Create an active user and owned session through repositories."""

    await SqlAlchemyUserRepository(session).ensure_user(user_id)
    await SqlAlchemySessionRepository(session).ensure_session(
        session_id,
        user_id,
    )
    stored_session = await session.get(SessionModel, str(session_id))
    assert stored_session is not None
    return stored_session


async def test_ensure_user_creates_active_user_with_memory_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A missing user should be created with safe model defaults."""

    user_id = UserId("user-create")
    async with session_factory() as session:
        await _assert_returns_none(SqlAlchemyUserRepository(session).ensure_user(user_id))
        stored_user = await session.get(UserModel, str(user_id))

        assert stored_user is not None
        assert stored_user.id == str(user_id)
        assert stored_user.status == USER_STATUS_ACTIVE
        assert stored_user.memory_enabled is False


async def test_get_memory_enabled_reads_default_and_latest_value(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Authorization reads are fail-closed and observe current stored state."""

    user_id = UserId("memory-consent-user")
    async with session_factory() as session:
        repository = SqlAlchemyUserRepository(session)
        await repository.ensure_user(user_id)
        stored_user = await session.get(UserModel, str(user_id))
        assert stored_user is not None

        assert await repository.get_memory_enabled(user_id) is False
        stored_user.memory_enabled = True
        await session.flush()
        assert await repository.get_memory_enabled(user_id) is True

        assert stored_user.status == USER_STATUS_ACTIVE
        assert stored_user.memory_enabled is True


async def test_get_memory_enabled_rejects_missing_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with pytest.raises(UserNotFoundError):
            await SqlAlchemyUserRepository(session).get_memory_enabled(
                UserId("missing-memory-user")
            )


@pytest.mark.parametrize("status", [USER_STATUS_DISABLED, USER_STATUS_DELETED])
async def test_get_memory_enabled_rejects_unavailable_user(
    session_factory: async_sessionmaker[AsyncSession],
    status: str,
) -> None:
    user_id = UserId(f"memory-{status}")
    async with session_factory() as session:
        stored_user = await _add_user(session, user_id, status=status)
        stored_user.memory_enabled = True
        await session.flush()

        with pytest.raises(UserUnavailableError):
            await SqlAlchemyUserRepository(session).get_memory_enabled(user_id)

        assert stored_user.status == status
        assert stored_user.memory_enabled is True


async def test_ensure_user_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Repeated ensure calls should retain one user row."""

    user_id = UserId("user-idempotent")
    async with session_factory() as session:
        repository = SqlAlchemyUserRepository(session)
        await repository.ensure_user(user_id)
        await repository.ensure_user(user_id)
        count = await session.scalar(select(func.count()).select_from(UserModel))

        assert count == 1


async def test_user_exists_ignores_lifecycle_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Existence should report every stored status and reject only absence."""

    async with session_factory() as session:
        repository = SqlAlchemyUserRepository(session)
        assert await repository.exists(UserId("missing-user")) is False

        for index, status in enumerate(
            (
                USER_STATUS_ACTIVE,
                USER_STATUS_DISABLED,
                USER_STATUS_DELETED,
            )
        ):
            user_id = UserId(f"stored-user-{index}")
            await _add_user(session, user_id, status=status)
            assert await repository.exists(user_id) is True


@pytest.mark.parametrize(
    "status",
    [USER_STATUS_DISABLED, USER_STATUS_DELETED],
)
async def test_ensure_user_rejects_unavailable_status_without_reactivation(
    session_factory: async_sessionmaker[AsyncSession],
    status: str,
) -> None:
    """Disabled and deleted users should never be reactivated implicitly."""

    user_id = UserId(f"unavailable-{status}")
    async with session_factory() as session:
        stored_user = await _add_user(session, user_id, status=status)

        with pytest.raises(UserUnavailableError):
            await SqlAlchemyUserRepository(session).ensure_user(user_id)

        assert stored_user.status == status


async def test_ensure_session_creates_active_session_with_counters(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A missing session should be created with safe counter defaults."""

    user_id = UserId("session-owner")
    session_id = SessionId("session-create")
    async with session_factory() as session:
        await SqlAlchemyUserRepository(session).ensure_user(user_id)
        await _assert_returns_none(
            SqlAlchemySessionRepository(session).ensure_session(
                session_id,
                user_id,
            )
        )
        stored_session = await session.get(SessionModel, str(session_id))

        assert stored_session is not None
        assert stored_session.user_id == str(user_id)
        assert stored_session.status == SESSION_STATUS_ACTIVE
        assert stored_session.current_state_version == 0
        assert stored_session.current_summary_version == 0
        assert stored_session.next_message_sequence == 1
        assert stored_session.ended_at is None


async def test_ensure_session_is_idempotent_and_preserves_progress(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Repeated ensure calls should preserve timestamps, versions, and sequence."""

    user_id = UserId("idempotent-owner")
    session_id = SessionId("session-idempotent")
    async with session_factory() as session:
        stored_session = await _ensure_user_and_session(
            session,
            user_id,
            session_id,
        )
        started_at = stored_session.started_at
        stored_session.current_state_version = 3
        stored_session.current_summary_version = 2
        stored_session.next_message_sequence = 8
        await session.flush()

        await SqlAlchemySessionRepository(session).ensure_session(
            session_id,
            user_id,
        )
        count = await session.scalar(select(func.count()).select_from(SessionModel))

        assert count == 1
        assert stored_session.started_at == started_at
        assert stored_session.current_state_version == 3
        assert stored_session.current_summary_version == 2
        assert stored_session.next_message_sequence == 8


async def test_ensure_session_requires_existing_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Session creation should fail before writing when the user is absent."""

    session_id = SessionId("orphan-session")
    async with session_factory() as session:
        repository = SqlAlchemySessionRepository(session)

        with pytest.raises(UserNotFoundError):
            await repository.ensure_session(session_id, UserId("missing-owner"))

        assert await repository.exists(session_id) is False


@pytest.mark.parametrize(
    "status",
    [USER_STATUS_DISABLED, USER_STATUS_DELETED],
)
async def test_ensure_session_rejects_unavailable_user(
    session_factory: async_sessionmaker[AsyncSession],
    status: str,
) -> None:
    """Disabled and deleted users should not receive new sessions."""

    user_id = UserId(f"session-owner-{status}")
    session_id = SessionId(f"blocked-session-{status}")
    async with session_factory() as session:
        await _add_user(session, user_id, status=status)
        repository = SqlAlchemySessionRepository(session)

        with pytest.raises(UserUnavailableError):
            await repository.ensure_session(session_id, user_id)

        assert await repository.exists(session_id) is False


async def test_ensure_session_rejects_ownership_conflict(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An existing session should never be reassigned to another user."""

    owner_id = UserId("owner-a")
    other_id = UserId("owner-b")
    session_id = SessionId("owned-session")
    async with session_factory() as session:
        stored_session = await _ensure_user_and_session(
            session,
            owner_id,
            session_id,
        )
        await SqlAlchemyUserRepository(session).ensure_user(other_id)

        with pytest.raises(SessionOwnershipError):
            await SqlAlchemySessionRepository(session).ensure_session(
                session_id,
                other_id,
            )

        assert stored_session.user_id == str(owner_id)


@pytest.mark.parametrize(
    "status",
    [SESSION_STATUS_CLOSED, SESSION_STATUS_CANCELLED],
)
async def test_ensure_session_does_not_reopen_unavailable_session(
    session_factory: async_sessionmaker[AsyncSession],
    status: str,
) -> None:
    """Closed and cancelled sessions should remain unavailable."""

    user_id = UserId(f"unavailable-session-owner-{status}")
    session_id = SessionId(f"unavailable-session-{status}")
    async with session_factory() as session:
        stored_session = await _ensure_user_and_session(
            session,
            user_id,
            session_id,
        )
        stored_session.status = status
        await session.flush()

        with pytest.raises(SessionUnavailableError):
            await SqlAlchemySessionRepository(session).ensure_session(
                session_id,
                user_id,
            )

        assert stored_session.status == status


async def test_assert_owned_by_covers_success_missing_and_conflict(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ownership checks should distinguish missing and foreign sessions."""

    owner_id = UserId("assert-owner")
    other_id = UserId("assert-other")
    session_id = SessionId("assert-session")
    async with session_factory() as session:
        await _ensure_user_and_session(session, owner_id, session_id)
        repository = SqlAlchemySessionRepository(session)

        await _assert_returns_none(repository.assert_owned_by(session_id, owner_id))
        with pytest.raises(SessionOwnershipError):
            await repository.assert_owned_by(session_id, other_id)
        with pytest.raises(SessionNotFoundError):
            await repository.assert_owned_by(
                SessionId("assert-missing"),
                owner_id,
            )


async def test_close_is_idempotent_and_preserves_first_end_time(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Closing twice should preserve the first timezone-aware end time."""

    user_id = UserId("close-owner")
    session_id = SessionId("close-session")
    async with session_factory() as session:
        stored_session = await _ensure_user_and_session(
            session,
            user_id,
            session_id,
        )
        repository = SqlAlchemySessionRepository(session)

        await _assert_returns_none(repository.close(session_id, user_id))
        first_ended_at = stored_session.ended_at
        assert stored_session.status == SESSION_STATUS_CLOSED
        assert first_ended_at is not None
        assert first_ended_at.tzinfo is UTC

        await _assert_returns_none(repository.close(session_id, user_id))
        assert stored_session.ended_at == first_ended_at


async def test_close_rejects_wrong_owner_without_state_change(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A foreign user should not be able to close an active session."""

    owner_id = UserId("close-real-owner")
    other_id = UserId("close-wrong-owner")
    session_id = SessionId("close-owned-session")
    async with session_factory() as session:
        stored_session = await _ensure_user_and_session(
            session,
            owner_id,
            session_id,
        )

        with pytest.raises(SessionOwnershipError):
            await SqlAlchemySessionRepository(session).close(
                session_id,
                other_id,
            )

        assert stored_session.status == SESSION_STATUS_ACTIVE
        assert stored_session.ended_at is None


async def test_close_rejects_cancelled_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A cancelled session should not transition to closed."""

    user_id = UserId("cancelled-owner")
    session_id = SessionId("cancelled-session")
    async with session_factory() as session:
        stored_session = await _ensure_user_and_session(
            session,
            user_id,
            session_id,
        )
        stored_session.status = SESSION_STATUS_CANCELLED
        await session.flush()

        with pytest.raises(SessionUnavailableError):
            await SqlAlchemySessionRepository(session).close(
                session_id,
                user_id,
            )

        assert stored_session.status == SESSION_STATUS_CANCELLED
        assert stored_session.ended_at is None


async def test_close_requires_existing_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Closing a missing session should raise the dedicated error."""

    async with session_factory() as session:
        with pytest.raises(SessionNotFoundError):
            await SqlAlchemySessionRepository(session).close(
                SessionId("close-missing"),
                UserId("close-missing-owner"),
            )


async def test_session_exists_ignores_lifecycle_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Session existence should include active, closed, and cancelled rows."""

    user_id = UserId("exists-owner")
    async with session_factory() as session:
        await _add_user(session, user_id)
        repository = SqlAlchemySessionRepository(session)
        assert await repository.exists(SessionId("missing-session")) is False

        for index, status in enumerate(
            (
                SESSION_STATUS_ACTIVE,
                SESSION_STATUS_CLOSED,
                SESSION_STATUS_CANCELLED,
            )
        ):
            session_id = SessionId(f"stored-session-{index}")
            session.add(
                SessionModel(
                    id=str(session_id),
                    user_id=str(user_id),
                    status=status,
                )
            )
            await session.flush()
            assert await repository.exists(session_id) is True


async def test_outer_transaction_commits_user_and_session_together(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Successful outer transaction exit should commit both repository writes."""

    user_id = UserId("commit-owner")
    session_id = SessionId("commit-session")
    async with transactional_session(session_factory) as session:
        await SqlAlchemyUserRepository(session).ensure_user(user_id)
        await SqlAlchemySessionRepository(session).ensure_session(
            session_id,
            user_id,
        )

    async with session_factory() as verification_session:
        assert await verification_session.get(UserModel, str(user_id)) is not None
        assert await verification_session.get(SessionModel, str(session_id)) is not None


async def test_outer_transaction_rolls_back_all_repository_writes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An outer exception should roll back user and session writes together."""

    user_id = UserId("rollback-owner")
    session_id = SessionId("rollback-session")
    with pytest.raises(ExpectedTransactionError):
        async with transactional_session(session_factory) as session:
            await SqlAlchemyUserRepository(session).ensure_user(user_id)
            await SqlAlchemySessionRepository(session).ensure_session(
                session_id,
                user_id,
            )
            raise ExpectedTransactionError("force outer rollback")

    async with session_factory() as verification_session:
        assert await verification_session.get(UserModel, str(user_id)) is None
        assert await verification_session.get(SessionModel, str(session_id)) is None


async def test_public_repository_methods_return_only_none_or_bool(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Repository boundaries should never expose ORM model instances."""

    user_id = UserId("return-owner")
    session_id = SessionId("return-session")
    async with session_factory() as session:
        user_repository = SqlAlchemyUserRepository(session)
        session_repository = SqlAlchemySessionRepository(session)

        await _assert_returns_none(user_repository.ensure_user(user_id))
        assert await user_repository.exists(user_id) is True
        await _assert_returns_none(session_repository.ensure_session(session_id, user_id))
        assert await session_repository.exists(session_id) is True
        await _assert_returns_none(session_repository.assert_owned_by(session_id, user_id))
        await _assert_returns_none(session_repository.close(session_id, user_id))


def test_close_uses_for_update_row_lock_statement() -> None:
    """Production close queries should request a row lock before mutation."""

    source = getsource(SqlAlchemySessionRepository.close)

    assert ".with_for_update()" in source


async def test_ensure_user_recovers_unique_race_inside_savepoint(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate insert race should reread the user and keep the outer tx usable."""

    user_id = UserId("user-savepoint-race")
    session_id = SessionId("session-after-user-race")
    async with transactional_session(session_factory) as session:
        await SqlAlchemyUserRepository(session).ensure_user(user_id)

    async with transactional_session(session_factory) as session:
        original_get = session.get
        first_user_lookup = True

        async def racing_get(model: type[object], identity: object) -> object | None:
            nonlocal first_user_lookup
            if model is UserModel and first_user_lookup:
                first_user_lookup = False
                return None
            if model is UserModel:
                return await original_get(UserModel, str(identity))
            if model is SessionModel:
                return await original_get(SessionModel, str(identity))
            raise AssertionError(model)

        monkeypatch.setattr(session, "get", racing_get)
        await SqlAlchemyUserRepository(session).ensure_user(user_id)
        await SqlAlchemySessionRepository(session).ensure_session(session_id, user_id)

    async with session_factory() as session:
        user_count = await session.scalar(
            select(func.count()).select_from(UserModel).where(UserModel.id == str(user_id))
        )
        stored_session = await session.get(SessionModel, str(session_id))

    assert user_count == 1
    assert stored_session is not None
    assert stored_session.user_id == str(user_id)


async def test_ensure_user_race_still_rejects_unavailable_record(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unique-race recovery must not reactivate a disabled user."""

    user_id = UserId("disabled-user-savepoint-race")
    async with transactional_session(session_factory) as session:
        await _add_user(session, user_id, status=USER_STATUS_DISABLED)

    async with session_factory() as session:
        original_get = session.get
        first_user_lookup = True

        async def racing_get(model: type[object], identity: object) -> object | None:
            nonlocal first_user_lookup
            if model is UserModel and first_user_lookup:
                first_user_lookup = False
                return None
            if model is UserModel:
                return await original_get(UserModel, str(identity))
            raise AssertionError(model)

        monkeypatch.setattr(session, "get", racing_get)
        with pytest.raises(UserUnavailableError):
            await SqlAlchemyUserRepository(session).ensure_user(user_id)


async def test_ensure_session_recovers_unique_race_and_preserves_owner(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate session race should reread and validate the persisted owner."""

    owner_id = UserId("session-savepoint-owner")
    other_id = UserId("session-savepoint-other")
    session_id = SessionId("session-savepoint-race")
    async with transactional_session(session_factory) as session:
        await SqlAlchemyUserRepository(session).ensure_user(owner_id)
        await SqlAlchemyUserRepository(session).ensure_user(other_id)
        await SqlAlchemySessionRepository(session).ensure_session(session_id, owner_id)

    async with transactional_session(session_factory) as session:
        original_get = session.get
        first_session_lookup = True

        async def racing_get(model: type[object], identity: object) -> object | None:
            nonlocal first_session_lookup
            if model is SessionModel and first_session_lookup:
                first_session_lookup = False
                return None
            if model is UserModel:
                return await original_get(UserModel, str(identity))
            if model is SessionModel:
                return await original_get(SessionModel, str(identity))
            raise AssertionError(model)

        monkeypatch.setattr(session, "get", racing_get)
        await SqlAlchemySessionRepository(session).ensure_session(session_id, owner_id)
        with pytest.raises(SessionOwnershipError):
            await SqlAlchemySessionRepository(session).ensure_session(
                session_id,
                other_id,
            )
        await SqlAlchemyUserRepository(session).ensure_user(
            UserId("user-created-after-session-race")
        )

    async with session_factory() as session:
        stored_session = await session.get(SessionModel, str(session_id))
        continuation = await session.get(
            UserModel,
            "user-created-after-session-race",
        )

    assert stored_session is not None
    assert stored_session.user_id == str(owner_id)
    assert continuation is not None


class ArtificialUnknownConstraintError(Exception):
    """Driver-like error without a recognized constraint category."""


async def test_unexpected_integrity_error_is_not_swallowed_and_outer_rolls_back(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only duplicates may recover; an unknown constraint aborts the outer tx."""

    user_id = UserId("unexpected-integrity-user")
    error = IntegrityError(
        "artificial statement",
        {},
        ArtificialUnknownConstraintError("artificial unknown constraint"),
    )

    with pytest.raises(IntegrityError) as caught:
        async with transactional_session(session_factory) as session:

            async def failing_flush(
                objects: Sequence[object] | None = None,
            ) -> None:
                _ = objects
                raise error

            monkeypatch.setattr(session, "flush", failing_flush)
            await SqlAlchemyUserRepository(session).ensure_user(user_id)

    assert caught.value is error
    async with session_factory() as session:
        assert await session.get(UserModel, str(user_id)) is None


def test_b5a_repositories_do_not_commit_or_rollback_outer_transactions() -> None:
    """Savepoint recovery must not add repository-owned transaction completion."""

    repository_classes = (
        SqlAlchemyUserRepository,
        SqlAlchemySessionRepository,
    )
    for repository_class in repository_classes:
        source = getsource(repository_class)
        assert ".commit(" not in source
        assert ".rollback(" not in source
    assert ".begin_nested()" in getsource(SqlAlchemyUserRepository.ensure_user)
    assert ".begin_nested()" in getsource(SqlAlchemySessionRepository.ensure_session)
