"""Redis resource ownership across Runtime and FastAPI lifecycle."""

import httpx
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisPyConnectionError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import Settings
from app.main import create_app
from llm.client import ManagedLLMClient
from llm.local_client import OpenAICompatibleHTTPClient
from llm.structured_client import StructuredLLMClient, StructuredLLMClientProtocol
from runtime.application import ApplicationRuntime
from runtime.factory import build_application_runtime
from runtime.redis_client import AsyncRedisClient
from runtime.redis_exceptions import RedisConnectionError

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
TEST_REDIS_URL = "redis://cache.internal:6379/0"


class FakeRedisClient:
    """Observable async Redis substitute without a network connection."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.ping_calls = 0
        self.close_calls = 0
        self.events: list[str] | None = None

    async def ping(self) -> bool:
        self.ping_calls += 1
        if self.error is not None:
            raise self.error
        return True

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.events is not None:
            self.events.append("redis")


def _settings(
    mode: str = "in_memory",
    *,
    app_env: str = "testing",
    redis_required: bool = False,
) -> Settings:
    base = Settings(
        app_env=app_env,
        app_runtime_mode="in_memory",
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        redis_required=redis_required,
        llm_base_url="https://llm.test/v1",
        llm_api_key="runtime-test-key",
        structured_model_name="runtime-test-model",
        main_model_name="runtime-test-model",
        safety_model_name="runtime-test-model",
        _env_file=None,
    )
    return base.model_copy(update={"app_runtime_mode": mode})


def _http_client(settings: Settings) -> OpenAICompatibleHTTPClient:
    return OpenAICompatibleHTTPClient.from_settings(
        settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )


async def test_runtime_creates_one_optional_redis_pool_and_closes_it_once() -> None:
    """Optional Redis may exist in any mode without being treated as a queue."""

    fake = FakeRedisClient()
    factory_calls = 0

    def redis_factory(settings: Settings, redis_url: str) -> AsyncRedisClient:
        nonlocal factory_calls
        _ = (settings, redis_url)
        factory_calls += 1
        return fake

    runtime = await build_application_runtime(
        _settings(),
        redis_client_factory=redis_factory,
    )
    assert runtime.redis_client is not None
    assert not runtime.redis_required
    assert factory_calls == 1
    assert fake.ping_calls == 0

    await runtime.aclose()
    await runtime.aclose()
    assert fake.close_calls == 1


def test_multiple_api_requests_do_not_recreate_redis_pool() -> None:
    """FastAPI requests should reuse the single lifespan-owned Redis resource."""

    fake = FakeRedisClient()
    factory_calls = 0

    def redis_factory(settings: Settings, redis_url: str) -> AsyncRedisClient:
        nonlocal factory_calls
        _ = (settings, redis_url)
        factory_calls += 1
        return fake

    async def builder(settings: Settings) -> ApplicationRuntime:
        return await build_application_runtime(
            settings,
            redis_client_factory=redis_factory,
        )

    app = create_app(
        runtime_builder=builder,
        settings_provider=lambda: _settings(),
    )
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/live").status_code == 200
        assert factory_calls == 1

    assert fake.close_calls == 1


async def test_production_required_redis_ping_failure_aborts_and_closes() -> None:
    """Required production Redis must be reachable before startup succeeds."""

    fake = FakeRedisClient(error=RedisPyConnectionError("offline"))

    with pytest.raises(RedisConnectionError):
        await build_application_runtime(
            _settings(
                "sqlalchemy_fake",
                app_env="production",
                redis_required=True,
            ),
            redis_client_factory=lambda settings, redis_url: fake,
        )

    assert fake.ping_calls == 1
    assert fake.close_calls == 1


async def test_later_startup_failure_releases_redis_http_and_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All already-created resources should close if final assembly fails."""

    fake = FakeRedisClient()
    engine = create_async_engine(TEST_DATABASE_URL)
    http_client: OpenAICompatibleHTTPClient | None = None
    dispose_calls = 0
    original_dispose = AsyncEngine.dispose

    async def tracked_dispose(active_engine: AsyncEngine) -> None:
        nonlocal dispose_calls
        if active_engine is engine:
            dispose_calls += 1
        await original_dispose(active_engine)

    monkeypatch.setattr(AsyncEngine, "dispose", tracked_dispose)

    def http_factory(settings: Settings) -> OpenAICompatibleHTTPClient:
        nonlocal http_client
        http_client = _http_client(settings)
        return http_client

    def failing_wrapper(
        structured: StructuredLLMClient,
        client: ManagedLLMClient,
    ) -> StructuredLLMClientProtocol:
        _ = (structured, client)
        raise RuntimeError("safe assembly failure")

    with pytest.raises(RuntimeError, match="safe assembly failure"):
        await build_application_runtime(
            _settings("sqlalchemy_model"),
            engine_factory=lambda settings: engine,
            http_client_factory=http_factory,
            redis_client_factory=lambda settings, redis_url: fake,
            structured_client_wrapper=failing_wrapper,
        )

    assert fake.close_calls == 1
    assert http_client is not None and http_client.is_closed
    assert dispose_calls == 1


async def test_runtime_shutdown_order_is_redis_then_http_then_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown should follow the documented resource dependency order."""

    events: list[str] = []
    fake = FakeRedisClient()
    fake.events = events
    runtime = await build_application_runtime(
        _settings("sqlalchemy_model"),
        http_client_factory=_http_client,
        redis_client_factory=lambda settings, redis_url: fake,
    )
    assert runtime.engine is not None
    tracked_engine = runtime.engine
    original_http_close = OpenAICompatibleHTTPClient.aclose
    original_engine_dispose = AsyncEngine.dispose

    async def tracked_http_close(client: OpenAICompatibleHTTPClient) -> None:
        events.append("http")
        await original_http_close(client)

    async def tracked_engine_close(engine: AsyncEngine) -> None:
        if engine is tracked_engine:
            events.append("engine")
        await original_engine_dispose(engine)

    monkeypatch.setattr(OpenAICompatibleHTTPClient, "aclose", tracked_http_close)
    monkeypatch.setattr(AsyncEngine, "dispose", tracked_engine_close)
    await runtime.aclose()
    await runtime.aclose()

    assert events == ["redis", "http", "engine"]
