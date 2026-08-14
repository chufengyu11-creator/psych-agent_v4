"""Unit tests for explicit application runtime modes and resources."""

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agents.feedback_evaluator import FakeFeedbackEvaluator, FeedbackEvaluator
from agents.output_guard import FakeOutputGuard
from agents.response_agent import FakeResponseAgent
from agents.risk_agent import FakeRiskAgent, RiskAgent
from agents.state_tracker import FakeStateTracker, StateTracker
from agents.strategy_planner import FakeStrategyPlanner, StrategyPlanner
from app.config import AppRuntimeMode, Settings
from llm.exceptions import LLMConfigurationError
from llm.local_client import OpenAICompatibleHTTPClient
from llm.structured_client import StructuredLLMClient
from runtime.application import RuntimeConfigurationError
from runtime.factory import build_application_runtime
from runtime.sqlalchemy_session_closer import SqlAlchemySessionCloser

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
TEST_LLM_BASE_URL = "https://llm.test/v1"
TEST_LLM_KEY = "unit-test-key-not-secret"
TEST_MODEL = "unit-structured-model"


def _settings(
    mode: AppRuntimeMode,
    **updates: object,
) -> Settings:
    base = Settings(
        app_env="testing",
        app_runtime_mode=mode,
        database_url=TEST_DATABASE_URL,
        llm_base_url=TEST_LLM_BASE_URL,
        llm_api_key=TEST_LLM_KEY,
        structured_model_name=TEST_MODEL,
        main_model_name=TEST_MODEL,
        safety_model_name=TEST_MODEL,
        _env_file=None,
    )
    return base.model_copy(update=updates)


def _http_client(settings: Settings) -> OpenAICompatibleHTTPClient:
    return OpenAICompatibleHTTPClient.from_settings(
        settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )


async def test_in_memory_runtime_builds_without_external_resources() -> None:
    """The test default should stay deterministic and network-free."""

    runtime = await build_application_runtime(_settings("in_memory"))

    assert runtime.mode == "in_memory"
    assert runtime.engine is None
    assert runtime.http_client is None
    assert "unit-test-key" not in repr(runtime)
    await runtime.aclose()
    assert runtime.closed


async def test_sqlalchemy_fake_runtime_uses_real_repositories_and_fake_agents() -> None:
    """The database-flow mode should retain all deterministic Agent dependencies."""

    runtime = await build_application_runtime(_settings("sqlalchemy_fake"))
    try:
        assert runtime.engine is not None
        assert runtime.session_factory is not None
        assert runtime.http_client is None
        assert isinstance(runtime.risk_agent, FakeRiskAgent)
        assert isinstance(runtime.state_tracker, FakeStateTracker)
        assert isinstance(runtime.strategy_planner, FakeStrategyPlanner)
        assert isinstance(runtime.feedback_evaluator, FakeFeedbackEvaluator)
        assert isinstance(runtime.session_closer, SqlAlchemySessionCloser)
        first = runtime.session_factory()
        second = runtime.session_factory()
        try:
            assert first is not second
        finally:
            await first.close()
            await second.close()
    finally:
        await runtime.aclose()


async def test_sqlalchemy_model_runtime_uses_formal_clients_and_model_agents() -> None:
    """Model mode should assemble the formal provider-independent call chain."""

    runtime = await build_application_runtime(
        _settings("sqlalchemy_model"),
        http_client_factory=_http_client,
    )
    try:
        assert isinstance(runtime.http_client, OpenAICompatibleHTTPClient)
        assert isinstance(runtime.structured_client, StructuredLLMClient)
        assert runtime.agent_structured_client is runtime.structured_client
        assert isinstance(runtime.risk_agent, RiskAgent)
        assert isinstance(runtime.state_tracker, StateTracker)
        assert isinstance(runtime.strategy_planner, StrategyPlanner)
        assert isinstance(runtime.response_agent, FakeResponseAgent)
        assert isinstance(runtime.output_guard, FakeOutputGuard)
        assert isinstance(runtime.feedback_evaluator, FeedbackEvaluator)
        assert isinstance(runtime.session_closer, SqlAlchemySessionCloser)
    finally:
        await runtime.aclose()
        await runtime.aclose()

    assert runtime.closed
    assert runtime.http_client is not None
    assert runtime.http_client.is_closed


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"llm_api_key": "replace-me"}, "API key"),
        ({"structured_model_name": "replace-me"}, "model name"),
    ],
)
async def test_model_runtime_rejects_incomplete_llm_configuration_without_fallback(
    updates: dict[str, object],
    message: str,
) -> None:
    """Model selection must fail instead of substituting scripted or fake Agents."""

    with pytest.raises(LLMConfigurationError, match=message):
        await build_application_runtime(_settings("sqlalchemy_model", **updates))


async def test_invalid_database_url_and_production_in_memory_fail_safely() -> None:
    """Unsafe runtime choices should fail before serving requests."""

    with pytest.raises(RuntimeConfigurationError, match="DATABASE_URL"):
        await build_application_runtime(
            _settings("sqlalchemy_fake", database_url="not-a-database-url")
        )
    with pytest.raises(RuntimeConfigurationError, match="production"):
        await build_application_runtime(_settings("in_memory", app_env="production"))


async def test_engine_and_http_client_are_built_once_and_closed_once() -> None:
    """Process resources should not be allocated per request or per session."""

    counts = {"engine": 0, "http": 0}

    def engine_factory(settings: Settings) -> AsyncEngine:
        counts["engine"] += 1
        return create_async_engine(settings.database_url)

    def http_factory(settings: Settings) -> OpenAICompatibleHTTPClient:
        counts["http"] += 1
        return _http_client(settings)

    runtime = await build_application_runtime(
        _settings("sqlalchemy_model"),
        engine_factory=engine_factory,
        http_client_factory=http_factory,
    )
    assert counts == {"engine": 1, "http": 1}

    assert runtime.session_factory is not None
    for _ in range(2):
        session = runtime.session_factory()
        await session.close()
    assert counts == {"engine": 1, "http": 1}

    await runtime.aclose()
    await runtime.aclose()
    assert runtime.http_client is not None and runtime.http_client.is_closed


async def test_startup_failure_disposes_an_engine_created_before_llm_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial model-runtime build should release the already-created engine."""

    engine = create_async_engine(TEST_DATABASE_URL)
    dispose_calls = 0
    original_dispose = AsyncEngine.dispose

    async def tracked_dispose(active_engine: AsyncEngine) -> None:
        nonlocal dispose_calls
        if active_engine is engine:
            dispose_calls += 1
        await original_dispose(active_engine)

    monkeypatch.setattr(AsyncEngine, "dispose", tracked_dispose)

    def engine_factory(settings: Settings) -> AsyncEngine:
        _ = settings
        return engine

    def failing_http_factory(settings: Settings) -> OpenAICompatibleHTTPClient:
        _ = settings
        raise LLMConfigurationError("LLM API key is not configured")

    with pytest.raises(LLMConfigurationError):
        await build_application_runtime(
            _settings("sqlalchemy_model"),
            engine_factory=engine_factory,
            http_client_factory=failing_http_factory,
        )

    assert dispose_calls == 1
