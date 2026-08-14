"""Application worker for post-turn follow-up tasks."""

from dataclasses import dataclass
from typing import Protocol

from schemas.common import SessionId, UserId
from schemas.events import RollingSummaryRequestedEvent
from schemas.summary import RollingSummary
from workers.post_turn_memory_worker import PostTurnMemoryResult, PostTurnMemoryWorker


@dataclass(frozen=True)
class PostTurnWorkerResult:
    """Observable result of one post-turn task."""

    session_id: SessionId
    summary_updated: bool
    summary_version: int | None = None
    memory_candidate_count: int = 0
    memory_write_count: int = 0


class SummaryWorkerProtocol(Protocol):
    """Structural boundary used to trigger rolling-summary work."""

    async def handle_summary_requested(
        self,
        event: RollingSummaryRequestedEvent,
    ) -> RollingSummary | None:
        """Handle one rolling-summary request."""


class PostTurnWorker:
    """Translate post-turn tasks into summary and lightweight memory work."""

    def __init__(
        self,
        summary_worker: SummaryWorkerProtocol,
        *,
        memory_worker: PostTurnMemoryWorker | None = None,
        enable_summary: bool = True,
        enable_memory: bool = True,
    ) -> None:
        """Create a worker with optional summary and memory stages."""

        self._summary_worker = summary_worker
        self._memory_worker = memory_worker
        self._enable_summary = enable_summary
        self._enable_memory = enable_memory

    async def handle_post_turn(
        self,
        *,
        user_id: UserId,
        session_id: SessionId,
    ) -> PostTurnWorkerResult:
        """Trigger rolling summary and safe durable-memory work."""

        summary = await self._run_summary(user_id, session_id)
        memory_result = await self._run_memory(user_id, session_id)
        return PostTurnWorkerResult(
            session_id=session_id,
            summary_updated=summary is not None,
            summary_version=summary.summary_version if summary is not None else None,
            memory_candidate_count=memory_result.candidate_count,
            memory_write_count=memory_result.memory_write_count,
        )

    async def _run_summary(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> RollingSummary | None:
        if not self._enable_summary:
            return None
        return await self._summary_worker.handle_summary_requested(
            RollingSummaryRequestedEvent(
                user_id=user_id,
                session_id=session_id,
                trigger="post_turn",
                after_message_id=None,
            )
        )

    async def _run_memory(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> PostTurnMemoryResult:
        if not self._enable_memory or self._memory_worker is None:
            return PostTurnMemoryResult(
                candidate_count=0,
                memory_write_count=0,
                memory_write_results=[],
            )
        return await self._memory_worker.handle_post_turn_memory(
            user_id=user_id,
            session_id=session_id,
        )


__all__ = ["PostTurnWorker", "PostTurnWorkerResult", "SummaryWorkerProtocol"]
