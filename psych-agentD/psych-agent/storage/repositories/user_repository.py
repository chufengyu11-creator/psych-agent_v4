"""User repository protocol and SQLAlchemy implementation."""

from typing import Protocol

from sqlalchemy import false, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.common import UserId
from storage.error_classification import IntegrityErrorKind, classify_integrity_error
from storage.models.user import USER_STATUS_ACTIVE, UserModel
from storage.repositories.errors import UserNotFoundError, UserUnavailableError


class UserRepository(Protocol):
    """Persistence boundary for user availability."""

    async def ensure_user(self, user_id: UserId) -> None:
        """Create an active user if absent and reject unavailable users."""

    async def exists(self, user_id: UserId) -> bool:
        """Return whether a user row exists regardless of its status."""

    async def get_memory_enabled(self, user_id: UserId) -> bool:
        """Return the persisted memory authorization for an active user."""


class SqlAlchemyUserRepository:
    """SQLAlchemy persistence for user existence and availability."""

    def __init__(self, session: AsyncSession) -> None:
        """Use the caller-owned asynchronous session."""

        self._session = session

    async def ensure_user(self, user_id: UserId) -> None:
        """Create an active user if absent and reject unavailable users."""

        user_id_value = str(user_id)
        stored_user = await self._session.get(UserModel, user_id_value)
        if stored_user is None:
            if self._session.get_bind().dialect.name == "sqlite":
                await self._session.execute(
                    update(UserModel).where(false()).values(status=UserModel.status)
                )
            try:
                async with self._session.begin_nested():
                    self._session.add(UserModel(id=user_id_value))
                    await self._session.flush()
                return
            except IntegrityError as error:
                if classify_integrity_error(error) is not IntegrityErrorKind.UNIQUE:
                    raise
                stored_user = await self._session.get(UserModel, user_id_value)
                if stored_user is None:
                    raise

        if stored_user.status != USER_STATUS_ACTIVE:
            raise UserUnavailableError(f"User {user_id_value!r} is not active.")

    async def exists(self, user_id: UserId) -> bool:
        """Return whether a user row exists regardless of its status."""

        stored_user = await self._session.get(UserModel, str(user_id))
        return stored_user is not None

    async def get_memory_enabled(self, user_id: UserId) -> bool:
        """Read memory authorization without creating or modifying the user."""

        user_id_value = str(user_id)
        stored_user = await self._session.get(UserModel, user_id_value)
        if stored_user is None:
            raise UserNotFoundError(f"User {user_id_value!r} does not exist.")
        if stored_user.status != USER_STATUS_ACTIVE:
            raise UserUnavailableError(f"User {user_id_value!r} is not active.")
        return bool(stored_user.memory_enabled)


__all__ = [
    "SqlAlchemyUserRepository",
    "UserRepository",
]
