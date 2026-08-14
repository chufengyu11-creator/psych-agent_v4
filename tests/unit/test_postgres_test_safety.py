"""Offline tests for the explicit PostgreSQL integration-test safety gate."""

import pytest

from tests.integration.postgres.conftest import validate_postgres_test_database_url


def test_postgres_test_url_accepts_separate_asyncpg_test_database() -> None:
    """A clearly named asyncpg test database should pass without connecting."""

    validate_postgres_test_database_url(
        "postgresql+asyncpg://user:secret@127.0.0.1/psych_agent_test"
    )


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://user:secret@127.0.0.1/psych_agent",
        "sqlite+aiosqlite:///psych_agent_test.db",
    ],
)
def test_postgres_test_url_rejects_unsafe_targets(database_url: str) -> None:
    """Tests must fail before migrating a development DB or another driver."""

    with pytest.raises(ValueError, match="TEST_DATABASE_URL"):
        validate_postgres_test_database_url(database_url)
