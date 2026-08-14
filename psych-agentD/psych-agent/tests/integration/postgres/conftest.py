"""Safety-gated fixtures for explicit PostgreSQL integration tests."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from schemas.common import SessionId, UserId
from storage.database import (
    create_engine,
    create_session_factory,
    dispose_engine,
    transactional_session,
)
from storage.models.intervention import InterventionEventModel
from storage.models.memory import LongTermMemoryModel
from storage.models.message import MessageModel
from storage.models.session import SessionModel
from storage.models.session_state import SessionStateVersionModel
from storage.models.summary import RollingSummaryVersionModel
from storage.models.user import UserModel

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class PostgresCase:
    """Unique identifiers owned by one PostgreSQL test and its cleanup."""

    user_id: UserId
    session_id: SessionId


def validate_postgres_test_database_url(database_url: str) -> None:
    """Reject non-asyncpg or non-test URLs before any migration or connection."""

    parsed_url = make_url(database_url)
    if parsed_url.drivername != "postgresql+asyncpg":
        msg = "TEST_DATABASE_URL must use postgresql+asyncpg"
        raise ValueError(msg)
    database_name = parsed_url.database or ""
    if "test" not in database_name.casefold():
        msg = "TEST_DATABASE_URL database name must contain 'test'"
        raise ValueError(msg)


@pytest.fixture(scope="session")
def postgres_test_settings() -> Settings:
    """Require an explicitly named test database and migrate it with Alembic."""

    settings = Settings()
    database_url = settings.test_database_url
    if database_url is None or not database_url.strip():
        pytest.skip("TEST_DATABASE_URL is not configured; PostgreSQL tests are explicit")

    try:
        validate_postgres_test_database_url(database_url)
    except ValueError as exc:
        pytest.fail(str(exc))

    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if migration.returncode != 0:
        pytest.fail(
            "Alembic upgrade failed for TEST_DATABASE_URL "
            f"with exit code {migration.returncode}"
        )
    return settings.model_copy(update={"database_url": database_url})


@pytest_asyncio.fixture
async def postgres_engine(
    postgres_test_settings: Settings,
) -> AsyncIterator[AsyncEngine]:
    """Provide a real asyncpg engine without creating or dropping schema."""

    engine = create_engine(postgres_test_settings)
    try:
        yield engine
    finally:
        await dispose_engine(engine)


@pytest_asyncio.fixture
async def postgres_session_factory(
    postgres_engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create caller-owned sessions for real transaction tests."""

    yield create_session_factory(postgres_engine)


@pytest_asyncio.fixture
async def postgres_case(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[PostgresCase]:
    """Yield unique IDs and delete only rows created by this test."""

    suffix = uuid4().hex
    case = PostgresCase(
        user_id=UserId(f"pg-user-{suffix}"),
        session_id=SessionId(f"pg-session-{suffix}"),
    )
    try:
        yield case
    finally:
        async with transactional_session(postgres_session_factory) as session:
            await session.execute(
                delete(LongTermMemoryModel).where(
                    LongTermMemoryModel.user_id == str(case.user_id)
                )
            )
            await session.execute(
                delete(RollingSummaryVersionModel).where(
                    RollingSummaryVersionModel.session_id == str(case.session_id)
                )
            )
            await session.execute(
                delete(InterventionEventModel).where(
                    InterventionEventModel.session_id == str(case.session_id)
                )
            )
            await session.execute(
                delete(SessionStateVersionModel).where(
                    SessionStateVersionModel.session_id == str(case.session_id)
                )
            )
            await session.execute(
                delete(MessageModel).where(
                    MessageModel.session_id == str(case.session_id)
                )
            )
            await session.execute(
                delete(SessionModel).where(SessionModel.id == str(case.session_id))
            )
            await session.execute(
                delete(UserModel).where(UserModel.id == str(case.user_id))
            )
