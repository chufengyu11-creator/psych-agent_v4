"""Sanitized Redis infrastructure exceptions."""


class RedisInfrastructureError(RuntimeError):
    """Base class for safe Redis failures."""


class RedisConfigurationError(RedisInfrastructureError):
    """Raised when required Redis configuration is absent or invalid."""


class RedisConnectionError(RedisInfrastructureError):
    """Raised when Redis cannot be reached."""


class RedisTimeoutError(RedisInfrastructureError):
    """Raised when a Redis operation exceeds its deadline."""


class RedisAuthenticationError(RedisInfrastructureError):
    """Raised when Redis credentials are rejected."""


class RedisPermissionError(RedisInfrastructureError):
    """Raised when Redis denies an authenticated command."""


class RedisProtocolError(RedisInfrastructureError):
    """Raised when Redis returns an invalid or command-level response."""


class RedisUnavailableError(RedisInfrastructureError):
    """Raised for another safely summarized Redis service failure."""


__all__ = [
    "RedisAuthenticationError",
    "RedisConfigurationError",
    "RedisConnectionError",
    "RedisInfrastructureError",
    "RedisPermissionError",
    "RedisProtocolError",
    "RedisTimeoutError",
    "RedisUnavailableError",
]
