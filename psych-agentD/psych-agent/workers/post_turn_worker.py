"""Application worker for post-turn follow-up tasks."""

from dataclasses import dataclass
from typing import Protocol

from schemas.common import SessionId, UserId
from schemas.events import RollingSummaryRequestedEvent
from schemas.summary import RollingSummary


@dataclass(frozen=True)
class PostTurnWorkerResult:
    """Observable result of one post-turn task."""

    session_id: SessionId
    summary_updated: bool
    summary_version: int | None = None


class SummaryWorkerProtocol(Protocol):
    """Structural boundary used to trigger rolling-summary work."""

    async def handle_summary_requested(
        self,
        event: RollingSummaryRequestedEvent,
    ) -> RollingSummary | None:
        """Handle one rolling-summary request."""


class PostTurnWorker:
    """Translate post-turn tasks into rolling-summary requests."""

    def __init__(
        self,
        summary_worker: SummaryWorkerProtocol,
        *,
        enable_summary: bool = True,
    ) -> None:
        """Create a worker with an optional summary stage."""

        self._summary_worker = summary_worker
        self._enable_summary = enable_summary

    async def handle_post_turn(
        self,
        *,
        user_id: UserId,
        session_id: SessionId,
    ) -> PostTurnWorkerResult:
        """Trigger rolling summary work for the current repository state."""

        _ = user_id
        if not self._enable_summary:
            return PostTurnWorkerResult(
                session_id=session_id,
                summary_updated=False,
            )
        summary = await self._summary_worker.handle_summary_requested(
            RollingSummaryRequestedEvent(
                session_id=session_id,
                trigger="post_turn",
                after_message_id=None,
            )
        )
        if summary is None:
            return PostTurnWorkerResult(
                session_id=session_id,
                summary_updated=False,
            )
        return PostTurnWorkerResult(
            session_id=session_id,
            summary_updated=True,
            summary_version=summary.summary_version,
        )


__all__ = ["PostTurnWorker", "PostTurnWorkerResult", "SummaryWorkerProtocol"]
