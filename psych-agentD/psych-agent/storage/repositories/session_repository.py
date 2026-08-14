"""Session repository protocol and SQLAlchemy implementation."""

from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import false, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.common import SessionId, UserId
from storage.error_classification import IntegrityErrorKind, classify_integrity_error
from storage.models.session import (
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_CANCELLED,
    SESSION_STATUS_CLOSED,
    SessionModel,
)
from storage.models.user import USER_STATUS_ACTIVE, UserModel
from storage.repositories.errors import (
    SessionNotFoundError,
    SessionOwnershipError,
    SessionUnavailableError,
    UserNotFoundError,
    UserUnavailableError,
)


class SessionRepository(Protocol):
    """Persistence boundary for session ownership and lifecycle."""

    async def ensure_session(
        self,
        session_id: SessionId,
        user_id: UserId,
    ) -> None:
        """Create an active session if absent and validate existing state."""

    async def exists(self, session_id: SessionId) -> bool:
        """Return whether a session row exists regardless of its status."""

    async def assert_owned_by(
        self,
        session_id: SessionId,
        user_id: UserId,
    ) -> None:
        """Require the session to exist and belong to the supplied user."""

    async def close(
        self,
        session_id: SessionId,
        user_id: UserId,
    ) -> None:
        """Close an active owned session while preserving idempotency."""


class SqlAlchemySessionRepository:
    """SQLAlchemy persistence for session ownership and lifecycle."""

    def __init__(self, session: AsyncSession) -> None:
        """Use the caller-owned asynchronous session."""

        self._session = session

    async def ensure_session(
        self,
        session_id: SessionId,
        user_id: UserId,
    ) -> None:
        """Create an active session if absent and validate existing state."""

        session_id_value = str(session_id)
        user_id_value = str(user_id)
        stored_user = await self._session.get(UserModel, user_id_value)
        if stored_user is None:
            raise UserNotFoundError(
                f"User {user_id_value!r} does not exist."
            )
        if stored_user.status != USER_STATUS_ACTIVE:
            raise UserUnavailableError(
                f"User {user_id_value!r} is not active."
            )

        stored_session = await self._session.get(
            SessionModel,
            session_id_value,
        )
        if stored_session is None:
            if self._session.get_bind().dialect.name == "sqlite":
                await self._session.execute(
                    update(SessionModel)
                    .where(false())
                    .values(status=SessionModel.status)
                )
            try:
                async with self._session.begin_nested():
                    self._session.add(
                        SessionModel(
                            id=session_id_value,
                            user_id=user_id_value,
                        )
                    )
                    await self._session.flush()
                return
            except IntegrityError as error:
                if classify_integrity_error(error) is not IntegrityErrorKind.UNIQUE:
                    raise
                stored_session = await self._session.get(
                    SessionModel,
                    session_id_value,
                )
                if stored_session is None:
                    raise

        if stored_session.user_id != user_id_value:
            raise SessionOwnershipError(
                f"Session {session_id_value!r} belongs to another user."
            )
        if stored_session.status != SESSION_STATUS_ACTIVE:
            raise SessionUnavailableError(
                f"Session {session_id_value!r} is not active."
            )

    async def exists(self, session_id: SessionId) -> bool:
        """Return whether a session row exists regardless of its status."""

        stored_session = await self._session.get(
            SessionModel,
            str(session_id),
        )
        return stored_session is not None

    async def assert_owned_by(
        self,
        session_id: SessionId,
        user_id: UserId,
    ) -> None:
        """Require the session to exist and belong to the supplied user."""

        session_id_value = str(session_id)
        user_id_value = str(user_id)
        stored_session = await self._session.get(
            SessionModel,
            session_id_value,
        )
        if stored_session is None:
            raise SessionNotFoundError(
                f"Session {session_id_value!r} does not exist."
            )
        if stored_session.user_id != user_id_value:
            raise SessionOwnershipError(
                f"Session {session_id_value!r} belongs to another user."
            )

    async def close(
        self,
        session_id: SessionId,
        user_id: UserId,
    ) -> None:
        """Close an active owned session while preserving idempotency."""

        session_id_value = str(session_id)
        user_id_value = str(user_id)
        statement = (
            select(SessionModel)
            .where(SessionModel.id == session_id_value)
            .with_for_update()
        )
        result = await self._session.execute(statement)
        stored_session = result.scalar_one_or_none()
        if stored_session is None:
            raise SessionNotFoundError(
                f"Session {session_id_value!r} does not exist."
            )
        if stored_session.user_id != user_id_value:
            raise SessionOwnershipError(
                f"Session {session_id_value!r} belongs to another user."
            )
        if stored_session.status == SESSION_STATUS_CLOSED:
            return
        if stored_session.status != SESSION_STATUS_ACTIVE:
            status = stored_session.status
            if status == SESSION_STATUS_CANCELLED:
                message = "is cancelled"
            else:
                message = f"has unsupported status {status!r}"
            raise SessionUnavailableError(
                f"Session {session_id_value!r} {message}."
            )

        stored_session.status = SESSION_STATUS_CLOSED
        stored_session.ended_at = datetime.now(UTC)
        await self._session.flush()


__all__ = [
    "SessionRepository",
    "SqlAlchemySessionRepository",
]
