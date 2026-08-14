"""Offline tests for the safe PostgreSQL connection-check script."""

from typing import cast

import pytest
from sqlalchemy.exc import DBAPIError

from app.config import Settings
from scripts.check_database_connection import (
    CORE_TABLES,
    KEY_SCHEMA_OBJECTS,
    CheckOptions,
    DatabaseCheckResult,
    DatabaseExitCode,
    database_exit_code_for_error,
    get_expected_head_revision,
    parse_options,
    render_result,
    resolve_database_url,
    run_database_checks,
)


def test_parse_options_supports_safe_check_modes() -> None:
    """CLI flags should remain explicit and stable."""

    options = parse_options(
        ["--json", "--skip-schema-check", "--url-env", "TEST_DATABASE_URL"]
    )

    assert options == CheckOptions(
        json_output=True,
        skip_schema_check=True,
        url_env="TEST_DATABASE_URL",
    )


def test_resolve_database_url_uses_typed_settings() -> None:
    """Development and test URLs should come from shared Settings."""

    settings = Settings(
        database_url="postgresql+asyncpg://user:dev-secret@localhost/app",
        test_database_url=(
            "postgresql+asyncpg://user:test-secret@localhost/app_test"
        ),
        _env_file=None,
    )

    assert resolve_database_url(settings, "DATABASE_URL") == settings.database_url
    assert (
        resolve_database_url(settings, "TEST_DATABASE_URL")
        == settings.test_database_url
    )


def test_resolve_database_url_rejects_missing_or_unsafe_names() -> None:
    """An absent test URL or malformed environment name is configuration failure."""

    settings = Settings(test_database_url=None, _env_file=None)

    with pytest.raises(ValueError, match="not configured"):
        resolve_database_url(settings, "TEST_DATABASE_URL")
    with pytest.raises(ValueError, match="invalid"):
        resolve_database_url(settings, "database-url")


@pytest.mark.asyncio
async def test_non_postgres_url_fails_before_engine_creation() -> None:
    """The checker must not silently accept SQLite as PostgreSQL validation."""

    settings = Settings(database_url="sqlite+aiosqlite:///:memory:", _env_file=None)
    result, exit_code = await run_database_checks(settings, CheckOptions())

    assert exit_code is DatabaseExitCode.CONFIGURATION
    assert result.connection_status == "not_run"
    assert result.error_type == "ValueError"


def test_render_result_never_contains_database_password_or_url() -> None:
    """Machine and human output should contain only explicitly safe fields."""

    secret = "database-password-that-must-not-leak"
    result = DatabaseCheckResult(
        url_env="DATABASE_URL",
        driver="postgresql+asyncpg",
        host="127.0.0.1",
        port=5432,
        database="psych_agent_test",
        tables={name: True for name in CORE_TABLES},
        schema_objects={name: True for name in KEY_SCHEMA_OBJECTS},
        success=True,
    )

    for rendered in (
        render_result(result, json_output=False),
        render_result(result, json_output=True),
    ):
        assert secret not in rendered
        assert "postgresql+asyncpg://" not in rendered
        assert "password" not in rendered


@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    [
        ("28P01", DatabaseExitCode.AUTHORIZATION),
        ("42501", DatabaseExitCode.AUTHORIZATION),
        ("3D000", DatabaseExitCode.DATABASE_NOT_FOUND),
        ("08006", DatabaseExitCode.CONNECTION),
        ("23505", DatabaseExitCode.DATABASE_ERROR),
    ],
)
def test_database_error_exit_codes_are_stable(
    sqlstate: str,
    expected: DatabaseExitCode,
) -> None:
    """PostgreSQL SQLSTATE classes should map to documented safe exit codes."""

    class OriginalError(Exception):
        def __init__(self, state: str) -> None:
            super().__init__("unsafe provider detail")
            self.sqlstate = state

    class WrappedError:
        def __init__(self, original: Exception) -> None:
            self.orig = original

    wrapped = cast(DBAPIError, WrappedError(OriginalError(sqlstate)))

    assert database_exit_code_for_error(wrapped) is expected


def test_expected_alembic_head_is_loaded_from_project_resources() -> None:
    """The checker should discover the packaged migration head without a DB."""

    assert get_expected_head_revision() == "0001_initial_core_schema"
