"""Post-turn task queue boundary."""

from typing import Protocol

from schemas.common import SessionId, UserId
from workers.post_turn_worker import PostTurnWorker, PostTurnWorkerResult


class TaskQueue(Protocol):
    """Minimal async queue contract for background post-turn work."""

    async def enqueue(self, task_name: str, user_id: str, session_id: str) -> None:
        """Schedule a post-turn task by name."""


class NoopTaskQueue:
    """Task queue implementation that records no background work."""

    async def enqueue(self, task_name: str, user_id: str, session_id: str) -> None:
        """Accept a task request and intentionally do nothing."""

        _ = (task_name, user_id, session_id)


class InlinePostTurnTaskQueue:
    """Execute post-turn work inline for local application integration."""

    def __init__(self, post_turn_worker: PostTurnWorker) -> None:
        """Create an inline queue backed by one post-turn worker."""

        self._post_turn_worker = post_turn_worker
        self.results: list[PostTurnWorkerResult] = []

    async def enqueue(self, task_name: str, user_id: str, session_id: str) -> None:
        """Run recognized post-turn tasks immediately and record their results."""

        if task_name != "post_turn":
            return
        result = await self._post_turn_worker.handle_post_turn(
            user_id=UserId(user_id),
            session_id=SessionId(session_id),
        )
        self.results.append(result)


__all__ = ["InlinePostTurnTaskQueue", "NoopTaskQueue", "TaskQueue"]
