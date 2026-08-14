"""Session repository protocol and SQLAlchemy implementation."""

from dataclasses import dataclass
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
    SessionUnavailableError,
    UserNotFoundError,
    UserUnavailableError,
)


@dataclass(frozen=True)
class SessionListItem:
    """User-visible summary for one conversation session."""

    session_id: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    current_state_version: int
    current_summary_version: int
    next_message_sequence: int


class SessionRepository(Protocol):
    """Persistence boundary for session ownership and lifecycle."""

    async def ensure_session(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> None:
        """Create an active session if absent and validate existing state."""

    async def exists(self, user_id: UserId, session_id: SessionId) -> bool:
        """Return whether an owned session row exists regardless of its status."""

    async def assert_owned_by(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> None:
        """Require the session to exist and belong to the supplied user."""

    async def close(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> None:
        """Close an active owned session while preserving idempotency."""

    async def list_for_user(
        self,
        user_id: UserId,
        limit: int = 30,
    ) -> list[SessionListItem]:
        """Return newest sessions owned by one user."""


class SqlAlchemySessionRepository:
    """SQLAlchemy persistence for session ownership and lifecycle."""

    def __init__(self, session: AsyncSession) -> None:
        """Use the caller-owned asynchronous session."""

        self._session = session

    async def ensure_session(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> None:
        """Create an active session if absent and validate existing state."""

        session_id_value = str(session_id)
        user_id_value = str(user_id)
        stored_user = await self._session.get(UserModel, user_id_value)
        if stored_user is None:
            raise UserNotFoundError(f"User {user_id_value!r} does not exist.")
        if stored_user.status != USER_STATUS_ACTIVE:
            raise UserUnavailableError(f"User {user_id_value!r} is not active.")

        stored_session = await self._get_owned_session(user_id, session_id)
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
                            user_id=user_id_value,
                            session_id=session_id_value,
                        )
                    )
                    await self._session.flush()
                return
            except IntegrityError as error:
                if classify_integrity_error(error) is not IntegrityErrorKind.UNIQUE:
                    raise
                stored_session = await self._get_owned_session(user_id, session_id)
                if stored_session is None:
                    raise

        if stored_session.status != SESSION_STATUS_ACTIVE:
            raise SessionUnavailableError(
                f"Session {session_id_value!r} is not active."
            )

    async def exists(self, user_id: UserId, session_id: SessionId) -> bool:
        """Return whether an owned session row exists regardless of its status."""

        stored_session = await self._get_owned_session(user_id, session_id)
        return stored_session is not None

    async def assert_owned_by(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> None:
        """Require the session to exist and belong to the supplied user."""

        session_id_value = str(session_id)
        stored_session = await self._get_owned_session(user_id, session_id)
        if stored_session is None:
            raise SessionNotFoundError(
                f"Session {session_id_value!r} does not exist."
            )

    async def close(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> None:
        """Close an active owned session while preserving idempotency."""

        session_id_value = str(session_id)
        user_id_value = str(user_id)
        statement = (
            select(SessionModel)
            .where(
                SessionModel.user_id == user_id_value,
                SessionModel.session_id == session_id_value,
            )
            .with_for_update()
        )
        result = await self._session.execute(statement)
        stored_session = result.scalar_one_or_none()
        if stored_session is None:
            raise SessionNotFoundError(
                f"Session {session_id_value!r} does not exist."
            )
        if stored_session.status == SESSION_STATUS_CLOSED:
            return
        if stored_session.status != SESSION_STATUS_ACTIVE:
            status = stored_session.status
            if status == SESSION_STATUS_CANCELLED:
                message = "is cancelled"
            else:
                message = f"has unsupported status {status!r}"
            raise SessionUnavailableError(f"Session {session_id_value!r} {message}.")

        stored_session.status = SESSION_STATUS_CLOSED
        stored_session.ended_at = datetime.now(UTC)
        await self._session.flush()

    async def list_for_user(
        self,
        user_id: UserId,
        limit: int = 30,
    ) -> list[SessionListItem]:
        """Return newest sessions owned by one user."""

        if limit < 1:
            return []
        statement = (
            select(SessionModel)
            .where(SessionModel.user_id == str(user_id))
            .order_by(SessionModel.started_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(statement)
        return [
            SessionListItem(
                session_id=row.session_id,
                status=row.status,
                started_at=row.started_at,
                ended_at=row.ended_at,
                current_state_version=row.current_state_version,
                current_summary_version=row.current_summary_version,
                next_message_sequence=row.next_message_sequence,
            )
            for row in result.scalars().all()
        ]

    async def _get_owned_session(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> SessionModel | None:
        """Resolve a user-visible session ID only inside its owner scope."""

        statement = select(SessionModel).where(
            SessionModel.user_id == str(user_id),
            SessionModel.session_id == str(session_id),
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()


__all__ = [
    "SessionListItem",
    "SessionRepository",
    "SqlAlchemySessionRepository",
]
