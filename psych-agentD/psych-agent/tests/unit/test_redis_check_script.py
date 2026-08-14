"""Offline tests for the standalone Redis connectivity script."""

import asyncio
import json

import pytest
from redis.exceptions import (
    AuthenticationError,
    ResponseError,
)
from redis.exceptions import (
    ConnectionError as RedisPyConnectionError,
)

from app.config import Settings
from runtime.redis_client import AsyncRedisClient, RedisClientFactory
from scripts.check_redis_connection import (
    RedisCheckExitCode,
    RedisCheckOptions,
    parse_options,
    render_result,
    run_redis_check,
)

SECRET = "redis-check-secret"


class FakeRedisClient:
    """Injected Redis client used to prove no real network is accessed."""

    def __init__(
        self,
        *,
        error: BaseException | None = None,
        delay: float = 0,
    ) -> None:
        self.error = error
        self.delay = delay
        self.ping_calls = 0
        self.close_calls = 0

    async def ping(self) -> bool:
        self.ping_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return True

    async def aclose(self) -> None:
        self.close_calls += 1


def _settings(redis_url: str | None, *, required: bool = False) -> Settings:
    return Settings(
        app_env="testing",
        redis_url=redis_url,
        redis_required=required,
        _env_file=None,
    )


def _factory(fake: FakeRedisClient) -> RedisClientFactory:
    def factory(settings: Settings, redis_url: str) -> AsyncRedisClient:
        _ = (settings, redis_url)
        return fake

    return factory


def test_redis_check_argument_contract() -> None:
    """JSON and timeout arguments should remain stable."""

    assert parse_options(["--json", "--timeout", "1.5"]) == RedisCheckOptions(
        json_output=True,
        timeout_seconds=1.5,
    )


async def test_missing_configuration_fails_safely_without_a_client() -> None:
    """An optional but absent URL must not be reported as connectivity success."""

    result, exit_code = await run_redis_check(
        _settings(None),
        RedisCheckOptions(),
    )

    assert exit_code is RedisCheckExitCode.CONFIGURATION
    assert not result.configured
    assert not result.success
    assert result.ping_status == "not_configured"


async def test_successful_ping_has_safe_machine_readable_metadata() -> None:
    """A successful fake PING should expose only host-level diagnostics."""

    fake = FakeRedisClient()
    settings = _settings(
        f"rediss://private-user:{SECRET}@cache.internal:6380/5",
        required=True,
    )
    result, exit_code = await run_redis_check(
        settings,
        RedisCheckOptions(json_output=True),
        client_factory=_factory(fake),
    )
    output = render_result(result, json_output=True)
    payload = json.loads(output)

    assert exit_code is RedisCheckExitCode.SUCCESS
    assert result.success
    assert payload["scheme"] == "rediss"
    assert payload["host"] == "cache.internal"
    assert payload["port"] == 6380
    assert payload["database"] == 5
    assert payload["tls"] is True
    assert fake.ping_calls == 1
    assert fake.close_calls == 1
    assert SECRET not in output
    assert "private-user" not in output
    assert "rediss://" not in output


@pytest.mark.parametrize(
    ("error", "expected_exit", "status"),
    [
        (
            RedisPyConnectionError("offline"),
            RedisCheckExitCode.CONNECTION,
            "unavailable",
        ),
        (
            AuthenticationError("bad"),
            RedisCheckExitCode.AUTHORIZATION,
            "authorization_failed",
        ),
        (
            ResponseError("bad response"),
            RedisCheckExitCode.PROTOCOL,
            "protocol_error",
        ),
    ],
)
async def test_failure_exit_codes_are_stable_and_sanitized(
    error: BaseException,
    expected_exit: RedisCheckExitCode,
    status: str,
) -> None:
    """Driver failures should map to stable script outcomes without raw details."""

    fake = FakeRedisClient(error=error)
    result, exit_code = await run_redis_check(
        _settings(f"redis://service:{SECRET}@cache.internal:6379/0"),
        RedisCheckOptions(),
        client_factory=_factory(fake),
    )
    output = render_result(result, json_output=False)

    assert exit_code is expected_exit
    assert result.ping_status == status
    assert SECRET not in output
    assert "offline" not in output
    assert "bad response" not in output


async def test_timeout_override_is_bounded_and_closes_client() -> None:
    """The CLI timeout should cancel a slow PING and still close the pool."""

    fake = FakeRedisClient(delay=0.05)
    result, exit_code = await run_redis_check(
        _settings("redis://cache.internal:6379/0"),
        RedisCheckOptions(timeout_seconds=0.01),
        client_factory=_factory(fake),
    )

    assert exit_code is RedisCheckExitCode.TIMEOUT
    assert result.ping_status == "timeout"
    assert fake.close_calls == 1
