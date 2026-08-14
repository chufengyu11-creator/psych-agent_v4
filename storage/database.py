"""SQLAlchemy async engine, session factory, and transaction helpers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Create a lazy async engine from application settings."""

    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
    )


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create a factory that returns independent async sessions."""

    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def transactional_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session inside a transaction that commits or rolls back on exit."""

    async with session_factory() as session:
        async with session.begin():
            yield session


async def dispose_engine(engine: AsyncEngine) -> None:
    """Dispose an engine and release any pooled connections."""

    await engine.dispose()
