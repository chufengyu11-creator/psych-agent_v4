"""Offline component readiness tests for database, Redis, and LLM."""

import asyncio
import inspect

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from api.health_models import ComponentHealth
from app.config import AppRuntimeMode, Settings
from app.redis_config import RedisEndpoint
from llm.local_client import OpenAICompatibleHTTPClient
from runtime.application import ApplicationRuntime
from runtime.factory import build_in_memory_orchestrator
from runtime.health import (
    DatabaseHealthChecker,
    LLMHealthChecker,
    RedisHealthChecker,
    RuntimeHealthChecker,
)
from runtime.redis_client import RedisResource

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
TEST_MODEL = "health-model"


class FakeRedisClient:
    """Read-only fake Redis client for readiness tests."""

    def __init__(self, *, result: bool = True) -> None:
        self.result = result
        self.ping_calls = 0
        self.close_calls = 0

    async def ping(self) -> bool:
        self.ping_calls += 1
        return self.result

    async def aclose(self) -> None:
        self.close_calls += 1


class StaticChecker:
    """Injected component checker for aggregation and route scenarios."""

    def __init__(self, result: ComponentHealth) -> None:
        self.result = result
        self.calls = 0

    def is_required(self, runtime: ApplicationRuntime) -> bool:
        _ = runtime
        return self.result.required

    async def check(self, runtime: ApplicationRuntime) -> ComponentHealth:
        _ = runtime
        self.calls += 1
        return self.result


class SlowConnectionContext:
    """Async context that exceeds the configured database health deadline."""

    async def __aenter__(self) -> object:
        await asyncio.sleep(0.05)
        raise RuntimeError("deadline should cancel before this point")

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        _ = (exc_type, exc_value, traceback)


def _settings(
    mode: AppRuntimeMode,
    *,
    redis_required: bool = False,
    health_timeout: float = 0.2,
) -> Settings:
    return Settings(
        app_env="testing",
        app_runtime_mode=mode,
        database_url=TEST_DATABASE_URL,
        redis_required=redis_required,
        llm_base_url="https://health.test/v1",
        llm_api_key="health-test-key",
        structured_model_name=TEST_MODEL,
        main_model_name=TEST_MODEL,
        safety_model_name=TEST_MODEL,
        health_check_timeout_seconds=health_timeout,
        _env_file=None,
    )


def _runtime(
    mode: AppRuntimeMode,
    *,
    engine: AsyncEngine | None = None,
    redis_client: RedisResource | None = None,
    redis_required: bool = False,
    http_client: OpenAICompatibleHTTPClient | None = None,
    health_timeout: float = 0.2,
) -> ApplicationRuntime:
    return ApplicationRuntime(
        settings=_settings(
            mode,
            redis_required=redis_required,
            health_timeout=health_timeout,
        ),
        mode=mode,
        orchestrator=build_in_memory_orchestrator(),
        engine=engine,
        redis_client=redis_client,
        redis_required=redis_required,
        http_client=http_client,
    )


def _models_response(status_code: int, models: list[str] | None = None) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={"data": [{"id": model} for model in models or []]},
    )


def _http_client(
    handler: httpx.AsyncBaseTransport | httpx.MockTransport,
    *,
    health_timeout: float = 0.2,
) -> OpenAICompatibleHTTPClient:
    return OpenAICompatibleHTTPClient.from_settings(
        _settings("sqlalchemy_model", health_timeout=health_timeout),
        transport=handler,
    )


async def test_in_memory_readiness_skips_all_external_dependencies() -> None:
    """The default test mode must be ready without external connections."""

    result = await RuntimeHealthChecker().check_readiness(_runtime("in_memory"))

    assert result.status == "ready"
    assert result.components.database.status == "not_required"
    assert result.components.redis.status == "not_configured"
    assert result.components.llm.status == "not_required"


async def test_sqlalchemy_fake_requires_database_but_not_llm() -> None:
    """SQLite should pass SELECT 1 without requiring an Alembic table."""

    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        result = await RuntimeHealthChecker().check_readiness(
            _runtime("sqlalchemy_fake", engine=engine)
        )
    finally:
        await engine.dispose()

    assert result.status == "ready"
    assert result.components.database.status == "ok"
    assert result.components.llm.status == "not_required"


async def test_database_connection_failure_is_safe_and_blocks_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Database connection failures should become a stable unavailable result."""

    engine = create_async_engine(TEST_DATABASE_URL)
    original_connect = AsyncEngine.connect

    def failing_connect(active_engine: AsyncEngine) -> AsyncConnection:
        if active_engine is engine:
            raise OperationalError("SELECT 1", {}, RuntimeError("offline"))
        return original_connect(active_engine)

    monkeypatch.setattr(AsyncEngine, "connect", failing_connect)
    try:
        result = await DatabaseHealthChecker().check(
            _runtime("sqlalchemy_fake", engine=engine)
        )
    finally:
        await engine.dispose()

    assert result.status == "unavailable"
    assert result.error_code == "database_unavailable"
    assert "offline" not in result.model_dump_json()


async def test_database_health_timeout_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled connection must become timeout instead of hanging readiness."""

    engine = create_async_engine(TEST_DATABASE_URL)
    original_connect = AsyncEngine.connect

    def slow_connect(
        active_engine: AsyncEngine,
    ) -> AsyncConnection | SlowConnectionContext:
        if active_engine is engine:
            return SlowConnectionContext()
        return original_connect(active_engine)

    monkeypatch.setattr(AsyncEngine, "connect", slow_connect)
    try:
        result = await DatabaseHealthChecker().check(
            _runtime(
                "sqlalchemy_fake",
                engine=engine,
                health_timeout=0.01,
            )
        )
    finally:
        await engine.dispose()

    assert result.status == "timeout"
    assert result.error_code == "database_timeout"


async def test_redis_optional_missing_and_required_ping_semantics() -> None:
    """Only configured required Redis failures should block overall readiness."""

    optional = await RedisHealthChecker().check(_runtime("in_memory"))
    required_missing = await RedisHealthChecker().check(
        _runtime("in_memory", redis_required=True)
    )
    fake = FakeRedisClient()
    resource = RedisResource(
        client=fake,
        endpoint=_redis_endpoint(),
        operation_timeout_seconds=0.2,
    )
    required_ok = await RedisHealthChecker().check(
        _runtime(
            "in_memory",
            redis_client=resource,
            redis_required=True,
        )
    )

    assert optional.status == "not_configured"
    assert required_missing.status == "missing_configuration"
    assert required_ok.status == "ok"
    assert fake.ping_calls == 1


async def test_optional_configured_redis_failure_does_not_block_overall_ready() -> None:
    """An optional observable failure is reported but is not a required failure."""

    fake = FakeRedisClient(result=False)
    resource = RedisResource(
        client=fake,
        endpoint=_redis_endpoint(),
        operation_timeout_seconds=0.2,
    )
    result = await RuntimeHealthChecker().check_readiness(
        _runtime("in_memory", redis_client=resource)
    )

    assert result.status == "ready"
    assert not result.components.redis.required
    assert result.components.redis.status == "protocol_error"


async def test_model_readiness_uses_models_once_and_never_calls_agents() -> None:
    """LLM readiness should only inspect the configured served model ID."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _models_response(200, [TEST_MODEL, "another-model"])

    client = _http_client(httpx.MockTransport(handler))
    engine = create_async_engine(TEST_DATABASE_URL)
    runtime = _runtime(
        "sqlalchemy_model",
        engine=engine,
        http_client=client,
    )
    try:
        result = await RuntimeHealthChecker().check_readiness(runtime)
    finally:
        await runtime.aclose()

    assert result.status == "ready"
    assert result.components.llm.status == "ok"
    assert "another-model" not in result.model_dump_json()
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path.endswith("/v1/models")


async def test_llm_model_missing_404_authentication_and_timeout_are_distinct() -> None:
    """Model discovery failures should use stable categories and no raw response."""

    cases: list[tuple[httpx.MockTransport, str]] = [
        (
            httpx.MockTransport(
                lambda request: _models_response(200, ["different-model"])
            ),
            "model_unavailable",
        ),
        (httpx.MockTransport(lambda request: httpx.Response(404)), "unsupported"),
        (
            httpx.MockTransport(lambda request: httpx.Response(401)),
            "authentication_failed",
        ),
    ]
    for transport, expected_status in cases:
        client = _http_client(transport)
        try:
            result = await LLMHealthChecker().check(
                _runtime("sqlalchemy_model", http_client=client)
            )
        finally:
            await client.aclose()
        assert result.status == expected_status

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        _ = request
        await asyncio.sleep(0.05)
        return _models_response(200, [TEST_MODEL])

    client = _http_client(
        httpx.MockTransport(slow_handler),
        health_timeout=0.01,
    )
    try:
        timeout_result = await LLMHealthChecker().check(
            _runtime(
                "sqlalchemy_model",
                http_client=client,
                health_timeout=0.01,
            )
        )
    finally:
        await client.aclose()
    assert timeout_result.status == "timeout"


async def test_llm_health_uses_one_attempt_even_for_retryable_server_error() -> None:
    """Readiness must not consume the normal multi-attempt generation budget."""

    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        _ = request
        requests += 1
        return httpx.Response(503)

    client = _http_client(httpx.MockTransport(handler))
    try:
        result = await LLMHealthChecker().check(
            _runtime("sqlalchemy_model", http_client=client)
        )
    finally:
        await client.aclose()

    assert result.status == "protocol_error"
    assert requests == 1
    assert client.request_count == 1


async def test_multiple_required_failures_are_all_returned() -> None:
    """One failed dependency must not hide another component result."""

    database = StaticChecker(
        ComponentHealth(
            required=True,
            status="unavailable",
            error_code="database_unavailable",
        )
    )
    redis = StaticChecker(
        ComponentHealth(
            required=True,
            status="timeout",
            error_code="redis_timeout",
        )
    )
    llm = StaticChecker(
        ComponentHealth(
            required=True,
            status="authentication_failed",
            error_code="llm_authentication",
        )
    )
    checker = RuntimeHealthChecker(
        database=database,
        redis=redis,
        llm=llm,
    )

    result = await checker.check_readiness(_runtime("sqlalchemy_model"))

    assert result.status == "not_ready"
    assert result.components.database.status == "unavailable"
    assert result.components.redis.status == "timeout"
    assert result.components.llm.status == "authentication_failed"
    assert database.calls == redis.calls == llm.calls == 1


def test_database_checker_source_is_read_only() -> None:
    """The readiness implementation must not migrate schemas or write rows."""

    source = inspect.getsource(DatabaseHealthChecker)
    assert "SELECT 1" in source
    assert "create_all" not in source
    assert "alembic upgrade" not in source
    assert "INSERT" not in source
    assert "UPDATE" not in source
    assert "DELETE" not in source


def _redis_endpoint() -> RedisEndpoint:
    return RedisEndpoint(
        scheme="redis",
        host="cache.internal",
        port=6379,
        database=0,
        tls=False,
        has_authentication=False,
    )
