"""Post-turn task queue boundary."""

from typing import Protocol


class TaskQueue(Protocol):
    """Minimal async queue contract for background post-turn work."""

    async def enqueue(self, task_name: str, user_id: str, session_id: str) -> None:
        """Schedule a post-turn task by name."""


class NoopTaskQueue:
    """Task queue implementation that records no background work."""

    async def enqueue(self, task_name: str, user_id: str, session_id: str) -> None:
        """Accept a task request and intentionally do nothing."""

        _ = (task_name, user_id, session_id)
