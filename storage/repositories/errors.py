"""Shared errors raised by persistent repositories."""


class RepositoryError(RuntimeError):
    """Base error for persistence repository failures."""


class UserNotFoundError(RepositoryError):
    """Raised when a required user does not exist."""


class UserUnavailableError(RepositoryError):
    """Raised when a user is disabled or deleted."""


class SessionNotFoundError(RepositoryError):
    """Raised when a required session does not exist."""


class SessionOwnershipError(RepositoryError):
    """Raised when a session belongs to another user."""


class SessionUnavailableError(RepositoryError):
    """Raised when a session cannot accept the requested operation."""


__all__ = [
    "RepositoryError",
    "SessionNotFoundError",
    "SessionOwnershipError",
    "SessionUnavailableError",
    "UserNotFoundError",
    "UserUnavailableError",
]
