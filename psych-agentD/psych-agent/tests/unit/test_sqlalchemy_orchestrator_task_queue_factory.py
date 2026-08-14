"""Unit tests for SQLAlchemy runtime task queue selection."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.post_turn_pipeline import NoopTaskQueue
from runtime.sqlalchemy_orchestrator import SqlAlchemyTurnOrchestrator


class StaticTaskQueue:
    """Minimal static queue used to verify constructor validation."""

    async def enqueue(self, task_name: str, user_id: str, session_id: str) -> None:
        """Accept a task without side effects."""

        _ = (task_name, user_id, session_id)


def test_sqlalchemy_orchestrator_rejects_static_queue_and_factory_together() -> None:
    """Queue configuration must have exactly one source when explicitly supplied."""

    with pytest.raises(ValueError, match="mutually exclusive"):
        SqlAlchemyTurnOrchestrator(
            session_factory=async_sessionmaker(),
            task_queue=StaticTaskQueue(),
            task_queue_factory=lambda session: StaticTaskQueue(),
        )


async def test_sqlalchemy_orchestrator_defaults_to_noop_queue() -> None:
    """The legacy constructor should retain its no-op post-turn behavior."""

    orchestrator = SqlAlchemyTurnOrchestrator(session_factory=async_sessionmaker())
    session = AsyncSession()
    try:
        queue = orchestrator._task_queue_for(session)
        assert isinstance(queue, NoopTaskQueue)
        await queue.enqueue("post_turn", "user", "session")
    finally:
        await session.close()
