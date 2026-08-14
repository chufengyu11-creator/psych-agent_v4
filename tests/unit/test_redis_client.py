"""Unit tests for safe Redis configuration and client infrastructure."""

import pytest
from pydantic import ValidationError
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
from runtime.redis_client import AsyncRedisClient, create_redis_client
from runtime.redis_exceptions import (
    RedisAuthenticationError,
    RedisConfigurationError,
    RedisConnectionError,
    RedisPermissionError,
    RedisProtocolError,
    RedisTimeoutError,
    RedisUnavailableError,
)

SECRET = "redis-secret-that-must-not-leak"


class FakeRedisClient:
    """Small injected redis-py substitute with observable calls."""

    def __init__(
        self,
        *,
        result: bool = True,
        error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.ping_calls = 0
        self.close_calls = 0

    async def ping(self) -> bool:
        self.ping_calls += 1
        if self.error is not None:
            raise self.error
        return self.result

    async def aclose(self) -> None:
        self.close_calls += 1


def _settings(
    redis_url: str | None,
    *,
    redis_required: bool = False,
    app_env: str = "testing",
) -> Settings:
    return Settings(
        app_env=app_env,
        redis_url=redis_url,
        redis_required=redis_required,
        _env_file=None,
    )


@pytest.mark.parametrize(
    ("url", "scheme", "database", "tls"),
    [
        ("redis://cache.internal:6380/4", "redis", 4, False),
        ("rediss://cache.internal/2", "rediss", 2, True),
    ],
)
def test_valid_redis_urls_and_database_index(
    url: str,
    scheme: str,
    database: int,
    tls: bool,
) -> None:
    """Both supported schemes should retain only safe endpoint metadata."""

    fake = FakeRedisClient()
    resource = create_redis_client(
        _settings(url),
        client_factory=lambda settings, raw_url: fake,
    )

    assert resource is not None
    assert resource.endpoint.scheme == scheme
    assert resource.endpoint.database == database
    assert resource.endpoint.tls is tls


@pytest.mark.parametrize(
    "redis_url",
    [
        "http://cache.internal:6379/0",
        "redis://:6379/0",
        "redis://cache.internal:0/0",
        "redis://cache.internal:70000/0",
        "redis://cache.internal/not-a-db",
        "redis://cache.internal/0#fragment",
    ],
)
def test_invalid_redis_urls_are_rejected(redis_url: str) -> None:
    """Invalid endpoints should fail Settings validation without a connection."""

    with pytest.raises(ValidationError):
        _settings(redis_url)


def test_settings_repr_redacts_redis_credentials_and_required_flag_works() -> None:
    """Settings diagnostics must never contain Redis authentication material."""

    settings = _settings(
        f"rediss://service:{SECRET}@cache.internal:6380/3",
        redis_required=True,
    )

    assert settings.redis_required
    assert SECRET not in repr(settings)
    assert "redis_url=" not in repr(settings)


def test_production_rejects_an_obvious_placeholder_password() -> None:
    """A production Redis URL may not retain the checked-in placeholder."""

    with pytest.raises(ValidationError, match="placeholder"):
        _settings(
            "redis://service:replace-me@cache.internal:6379/0",
            redis_required=True,
            app_env="production",
        )


def test_invalid_redis_validation_error_hides_credentials() -> None:
    """Configuration diagnostics must not echo an invalid URL or password."""

    secret = "invalid-url-secret"
    with pytest.raises(ValidationError) as captured:
        _settings(f"http://service:{secret}@cache.internal:6379/0")

    assert secret not in str(captured.value)
    assert "http://" not in str(captured.value)


def test_optional_missing_redis_is_none_but_required_missing_fails() -> None:
    """Optional and required configuration must have distinct semantics."""

    assert create_redis_client(_settings(None)) is None
    with pytest.raises(RedisConfigurationError, match="required"):
        create_redis_client(_settings(None, redis_required=True))


async def test_client_is_created_once_pinged_and_closed_idempotently() -> None:
    """One resource should reuse one injected pool across all PING calls."""

    fake = FakeRedisClient()
    factory_calls = 0

    def factory(settings: Settings, redis_url: str) -> AsyncRedisClient:
        nonlocal factory_calls
        _ = (settings, redis_url)
        factory_calls += 1
        return fake

    resource = create_redis_client(
        _settings("redis://cache.internal:6379/0"),
        client_factory=factory,
    )
    assert resource is not None
    await resource.ping()
    await resource.ping()
    await resource.aclose()
    await resource.aclose()

    assert factory_calls == 1
    assert fake.ping_calls == 2
    assert fake.close_calls == 1
    assert resource.closed


@pytest.mark.parametrize(
    ("driver_error", "expected"),
    [
        (AuthenticationError("bad credentials"), RedisAuthenticationError),
        (AuthorizationError("denied"), RedisPermissionError),
        (RedisPyTimeoutError("slow"), RedisTimeoutError),
        (RedisPyConnectionError("offline"), RedisConnectionError),
        (ResponseError("bad response"), RedisProtocolError),
        (RedisError("other"), RedisUnavailableError),
    ],
)
async def test_ping_maps_redis_py_failures_precisely(
    driver_error: BaseException,
    expected: type[Exception],
) -> None:
    """redis-py exceptions should not escape into health routes."""

    fake = FakeRedisClient(error=driver_error)
    resource = create_redis_client(
        _settings(f"redis://service:{SECRET}@cache.internal:6379/0"),
        client_factory=lambda settings, redis_url: fake,
    )
    assert resource is not None

    with pytest.raises(expected) as captured:
        await resource.ping()

    assert SECRET not in str(captured.value)
    assert "redis://" not in str(captured.value)


async def test_false_ping_is_protocol_error_and_repr_is_safe() -> None:
    """A non-true PING response is not connectivity success."""

    fake = FakeRedisClient(result=False)
    resource = create_redis_client(
        _settings(f"redis://service:{SECRET}@cache.internal:6379/7"),
        client_factory=lambda settings, redis_url: fake,
    )
    assert resource is not None

    with pytest.raises(RedisProtocolError):
        await resource.ping()
    representation = repr(resource)
    assert "cache.internal" in representation
    assert "database=7" in representation
    assert SECRET not in representation
    assert "service" not in representation
