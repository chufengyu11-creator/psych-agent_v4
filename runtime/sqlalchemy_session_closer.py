"""Transactional SQLAlchemy runtime service for session close and memory flow."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.memory_curator import MemoryCurator
from agents.rolling_summarizer import FakeRollingSummarizer
from agents.session_finalizer import FakeSessionFinalizer
from schemas.common import SessionId, UserId
from schemas.events import RollingSummaryRequestedEvent, SessionCloseRequestedEvent
from services.memory_policy import MemoryPolicy
from storage.database import transactional_session
from storage.models.user import UserModel
from storage.repositories.intervention_repository import SqlAlchemyInterventionRepository
from storage.repositories.memory_repository import SqlAlchemyMemoryRepository
from storage.repositories.message_repository import SqlAlchemyMessageRepository
from storage.repositories.session_repository import SqlAlchemySessionRepository
from storage.repositories.state_repository import SqlAlchemyStateRepository
from storage.repositories.summary_repository import SqlAlchemySummaryRepository
from storage.repositories.user_repository import SqlAlchemyUserRepository
from workers.session_close_worker import (
    MemoryCuratorProtocol,
    SessionCloseMemoryWorker,
    SessionCloseResult,
)
from workers.session_close_worker import (
    SessionFinalizer as SessionFinalizerProtocol,
)
from workers.summary_worker import Summarizer, SummaryWorker


class SqlAlchemySessionCloser:
    """Close an existing owned session and persist policy-approved memory atomically."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        summarizer: Summarizer | None = None,
        finalizer: SessionFinalizerProtocol | None = None,
        memory_curator: MemoryCuratorProtocol | None = None,
        memory_policy: MemoryPolicy | None = None,
        message_limit: int = 50,
        ensure_summary: bool = True,
    ) -> None:
        """Configure transaction-local close dependencies and optional overrides."""

        if message_limit < 1:
            raise ValueError("message_limit must be at least 1")
        self._session_factory = session_factory
        self._summarizer = summarizer or FakeRollingSummarizer()
        self._finalizer = finalizer or FakeSessionFinalizer()
        self._memory_curator = memory_curator or MemoryCurator()
        self._memory_policy = memory_policy or MemoryPolicy()
        self._message_limit = message_limit
        self._ensure_summary = ensure_summary

    async def close_session(
        self,
        *,
        user_id: UserId,
        session_id: SessionId,
        reason: str | None = None,
    ) -> SessionCloseResult:
        """Run summary, finalization, memory policy, writes, and close in one transaction."""

        async with transactional_session(self._session_factory) as session:
            message_repository = SqlAlchemyMessageRepository(session)
            state_repository = SqlAlchemyStateRepository(session)
            summary_repository = SqlAlchemySummaryRepository(session)
            intervention_repository = SqlAlchemyInterventionRepository(session)
            memory_repository = SqlAlchemyMemoryRepository(session)
            session_repository = SqlAlchemySessionRepository(session)

            await SqlAlchemyUserRepository(session).ensure_user(user_id)
            user_row = await session.get(UserModel, str(user_id))
            user_memory_enabled = bool(user_row and user_row.memory_enabled)
            await session_repository.assert_owned_by(user_id, session_id)

            if self._ensure_summary:
                await SummaryWorker(
                    message_repository=message_repository,
                    state_repository=state_repository,
                    summary_repository=summary_repository,
                    intervention_repository=intervention_repository,
                    summarizer=self._summarizer,
                    message_limit=self._message_limit,
                ).handle_summary_requested(
                    RollingSummaryRequestedEvent(
                        user_id=user_id,
                        session_id=session_id,
                        trigger="session_close",
                        after_message_id=None,
                    )
                )

            worker = SessionCloseMemoryWorker(
                message_repository=message_repository,
                state_repository=state_repository,
                summary_repository=summary_repository,
                intervention_repository=intervention_repository,
                memory_repository=memory_repository,
                session_repository=session_repository,
                finalizer=self._finalizer,
                memory_policy=self._memory_policy,
                message_limit=self._message_limit,
                user_memory_enabled=user_memory_enabled,
                memory_curator=self._memory_curator,
            )
            return await worker.handle_session_close_requested(
                SessionCloseRequestedEvent(
                    session_id=session_id,
                    user_id=user_id,
                    reason=reason,
                )
            )


__all__ = ["SqlAlchemySessionCloser"]
