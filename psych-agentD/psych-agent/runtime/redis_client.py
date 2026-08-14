"""Application-owned asynchronous Redis client infrastructure."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, cast

from redis.asyncio import Redis
from redis.exceptions import (
    AuthenticationError,
    AuthorizationError,
    RedisError,
    ResponseError,
)
from redis.exceptions import (
    ConnectionError as RedisPyConnectionError,
)
from redis.exceptions import (
    TimeoutError as RedisPyTimeoutError,
)

from app.config import Settings
from app.redis_config import RedisEndpoint, parse_redis_url
from runtime.redis_exceptions import (
    RedisAuthenticationError,
    RedisConfigurationError,
    RedisConnectionError,
    RedisPermissionError,
    RedisProtocolError,
    RedisTimeoutError,
    RedisUnavailableError,
)


class AsyncRedisClient(Protocol):
    """Small redis-py surface needed by health checks and future adapters."""

    async def ping(self) -> bool:
        """Return whether Redis accepted PING."""

    async def aclose(self) -> None:
        """Close the client connection pool."""


RedisClientFactory = Callable[[Settings, str], AsyncRedisClient]


@dataclass(repr=False)
class RedisResource:
    """One process-scoped Redis pool plus safe endpoint metadata."""

    client: AsyncRedisClient
    endpoint: RedisEndpoint
    operation_timeout_seconds: float
    _closed: bool = field(default=False, init=False)

    @property
    def closed(self) -> bool:
        """Return whether the underlying client has been closed."""

        return self._closed

    async def ping(self, *, timeout_seconds: float | None = None) -> None:
        """Execute read-only PING and convert redis-py failures safely."""

        timeout = timeout_seconds or self.operation_timeout_seconds
        try:
            async with asyncio.timeout(timeout):
                result = await self.client.ping()
        except AuthenticationError as exc:
            raise RedisAuthenticationError("Redis authentication failed") from exc
        except AuthorizationError as exc:
            raise RedisPermissionError("Redis permission denied") from exc
        except (RedisPyTimeoutError, TimeoutError) as exc:
            raise RedisTimeoutError("Redis PING timed out") from exc
        except RedisPyConnectionError as exc:
            raise RedisConnectionError("Redis connection failed") from exc
        except ResponseError as exc:
            raise RedisProtocolError("Redis PING returned an invalid response") from exc
        except RedisError as exc:
            raise RedisUnavailableError("Redis service is unavailable") from exc
        if result is not True:
            raise RedisProtocolError("Redis PING did not return success")

    async def aclose(self) -> None:
        """Close the shared client once; repeated shutdown is safe."""

        if self._closed:
            return
        self._closed = True
        await self.client.aclose()

    def __repr__(self) -> str:
        """Return diagnostics without username, password, or full URL."""

        return (
            f"{type(self).__name__}(scheme={self.endpoint.scheme!r}, "
            f"host={self.endpoint.host!r}, port={self.endpoint.port}, "
            f"database={self.endpoint.database}, tls={self.endpoint.tls}, "
            f"closed={self._closed})"
        )


def create_redis_client(
    settings: Settings,
    *,
    client_factory: RedisClientFactory | None = None,
) -> RedisResource | None:
    """Create one lazy redis-py pool or enforce required configuration."""

    if settings.redis_url is None:
        if settings.redis_required:
            raise RedisConfigurationError("REDIS_URL is required but not configured")
        return None
    redis_url = settings.redis_url.get_secret_value()
    try:
        endpoint = parse_redis_url(redis_url)
    except ValueError as exc:
        raise RedisConfigurationError("REDIS_URL is invalid") from exc
    factory = client_factory or _default_client_factory
    client = factory(settings, redis_url)
    return RedisResource(
        client=client,
        endpoint=endpoint,
        operation_timeout_seconds=settings.redis_socket_timeout_seconds,
    )


def _default_client_factory(settings: Settings, redis_url: str) -> AsyncRedisClient:
    """Create redis.asyncio without opening a network connection."""

    client = Redis.from_url(
        redis_url,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        decode_responses=True,
    )
    return cast(AsyncRedisClient, client)


__all__ = [
    "AsyncRedisClient",
    "RedisClientFactory",
    "RedisResource",
    "create_redis_client",
]
