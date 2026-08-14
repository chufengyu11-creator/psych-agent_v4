"""Unit tests for application settings and async database infrastructure."""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import Settings, get_settings
from storage.database import (
    create_engine,
    create_session_factory,
    dispose_engine,
    transactional_session,
)

TEST_DATABASE_URL = (
    "postgresql+asyncpg://psych_agent:psych_agent@localhost:5432/psych_agent_test"
)
SETTINGS_ENVIRONMENT_NAMES = (
    "APP_ENV",
    "APP_RUNTIME_MODE",
    "APP_HOST",
    "APP_PORT",
    "LOG_LEVEL",
    "DATABASE_URL",
    "TEST_DATABASE_URL",
    "REDIS_URL",
    "REDIS_REQUIRED",
    "REDIS_CONNECT_TIMEOUT_SECONDS",
    "REDIS_SOCKET_TIMEOUT_SECONDS",
    "HEALTH_CHECK_TIMEOUT_SECONDS",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "MAIN_MODEL_NAME",
    "STRUCTURED_MODEL_NAME",
    "SAFETY_MODEL_NAME",
    "EMBEDDING_MODEL_ENABLED",
    "EMBEDDING_MODEL_NAME",
    "RAG_ENABLED",
    "RAG_TOP_K",
    "RAG_TIMEOUT_MS",
    "RAG_MAX_CONTEXT_TOKENS",
    "RAG_RERANK_ENABLED",
    "RAG_EMBEDDING_TIMEOUT_MS",
    "RAG_EMBEDDING_DIMENSIONS",
)


def _clear_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove settings variables so tests observe declared defaults."""

    for name in SETTINGS_ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)


def _test_settings() -> Settings:
    """Return isolated settings for a database that is never contacted."""

    return Settings(database_url=TEST_DATABASE_URL, _env_file=None)


def test_settings_defaults_match_env_example(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings defaults should match the checked-in environment example."""

    _clear_settings_environment(monkeypatch)

    settings = Settings(_env_file=None)

    assert settings.database_url == (
        "postgresql+asyncpg://psych_agent:replace-me@127.0.0.1:5432/psych_agent"
    )
    assert settings.test_database_url is None
    assert settings.redis_url is None
    assert not settings.redis_required
    assert settings.app_port == 8000
    assert settings.app_env == "development"
    assert settings.app_runtime_mode == "in_memory"


def test_settings_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables should override defaults with type conversion."""

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("TEST_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("APP_PORT", "9001")
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("APP_RUNTIME_MODE", "sqlalchemy_fake")

    settings = Settings(_env_file=None)

    assert settings.database_url == TEST_DATABASE_URL
    assert settings.test_database_url == TEST_DATABASE_URL
    assert settings.app_port == 9001
    assert settings.app_env == "testing"
    assert settings.app_runtime_mode == "sqlalchemy_fake"


def test_database_urls_are_redacted_from_settings_repr() -> None:
    """Settings diagnostics must not expose development or test passwords."""

    secret = "database-password-that-must-not-leak"
    settings = Settings(
        database_url=f"postgresql+asyncpg://user:{secret}@localhost/app",
        test_database_url=f"postgresql+asyncpg://user:{secret}@localhost/app_test",
        _env_file=None,
    )

    assert secret not in repr(settings)
    assert "database_url=" not in repr(settings)
    assert "test_database_url=" not in repr(settings)


def test_get_settings_returns_cached_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_settings should cache one Settings instance until cleared."""

    _clear_settings_environment(monkeypatch)
    monkeypatch.chdir(Path(__file__).parent)
    get_settings.cache_clear()
    try:
        first = get_settings()
        second = get_settings()

        assert first is second
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_create_engine_uses_configured_async_url() -> None:
    """Engine creation should be lazy and retain the configured URL parts."""

    engine = create_engine(_test_settings())
    try:
        assert isinstance(engine, AsyncEngine)
        assert engine.url.drivername == "postgresql+asyncpg"
        assert engine.url.database == "psych_agent_test"
    finally:
        await dispose_engine(engine)


@pytest.mark.asyncio
async def test_session_factory_returns_independent_configured_sessions() -> None:
    """The session factory should create independent non-expiring sessions."""

    engine = create_engine(_test_settings())
    factory = create_session_factory(engine)
    first_session = factory()
    second_session = factory()
    try:
        assert isinstance(first_session, AsyncSession)
        assert isinstance(second_session, AsyncSession)
        assert first_session is not second_session
        assert first_session.sync_session.autoflush is False
        assert first_session.sync_session.expire_on_commit is False
    finally:
        await first_session.close()
        await second_session.close()
        await dispose_engine(engine)


@pytest.mark.asyncio
async def test_transactional_session_ends_transaction_on_normal_exit() -> None:
    """A successful transactional context should end its active transaction."""

    engine = create_engine(_test_settings())
    factory = create_session_factory(engine)
    try:
        async with transactional_session(factory) as session:
            assert isinstance(session, AsyncSession)
            assert session.in_transaction()

        assert not session.in_transaction()
    finally:
        await dispose_engine(engine)


@pytest.mark.asyncio
async def test_transactional_session_rolls_back_and_propagates_error() -> None:
    """An exceptional transactional context should end and re-raise the error."""

    class ExpectedError(RuntimeError):
        """Error used to verify transaction exception propagation."""

    engine = create_engine(_test_settings())
    factory = create_session_factory(engine)
    session: AsyncSession | None = None
    try:
        with pytest.raises(ExpectedError, match="expected"):
            async with transactional_session(factory) as active_session:
                session = active_session
                assert session.in_transaction()
                raise ExpectedError("expected")

        assert session is not None
        assert not session.in_transaction()
    finally:
        await dispose_engine(engine)


@pytest.mark.asyncio
async def test_dispose_engine_without_opening_connection() -> None:
    """Engine disposal should be safe before any database connection is opened."""

    engine = create_engine(_test_settings())

    await dispose_engine(engine)
