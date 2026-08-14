# """Post-turn task queue boundary."""

# from typing import Protocol

# from schemas.common import SessionId, UserId
# from workers.post_turn_worker import PostTurnWorker, PostTurnWorkerResult


# class TaskQueue(Protocol):
#     """Minimal async queue contract for background post-turn work."""

#     async def enqueue(self, task_name: str, user_id: str, session_id: str) -> None:
#         """Schedule a post-turn task by name."""


# class NoopTaskQueue:
#     """Task queue implementation that records no background work."""

#     async def enqueue(self, task_name: str, user_id: str, session_id: str) -> None:
#         """Accept a task request and intentionally do nothing."""

#         _ = (task_name, user_id, session_id)


# class InlinePostTurnTaskQueue:
#     """Execute post-turn work inline for local application integration."""

#     def __init__(self, post_turn_worker: PostTurnWorker) -> None:
#         """Create an inline queue backed by one post-turn worker."""

#         self._post_turn_worker = post_turn_worker
#         self.results: list[PostTurnWorkerResult] = []

#     async def enqueue(self, task_name: str, user_id: str, session_id: str) -> None:
#         """Run recognized post-turn tasks immediately and record their results."""

#         if task_name != "post_turn":
#             return
#         result = await self._post_turn_worker.handle_post_turn(
#             user_id=UserId(user_id),
#             session_id=SessionId(session_id),
#         )
#         self.results.append(result)


# __all__ = ["InlinePostTurnTaskQueue", "NoopTaskQueue", "TaskQueue"]








"""Post-turn task queue boundary."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

from schemas.common import SessionId, UserId
from services.timing import timing_span
from workers.post_turn_worker import PostTurnWorker, PostTurnWorkerResult

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PostTurnTask:
    """One immutable task captured during a request transaction."""

    task_name: str
    user_id: str
    session_id: str


class TaskQueue(Protocol):
    """Minimal async queue contract for background post-turn work."""

    async def enqueue(self, task_name: str, user_id: str, session_id: str) -> None:
        """Schedule a post-turn task by name."""


class NoopTaskQueue:
    """Task queue implementation that records no background work."""

    async def enqueue(self, task_name: str, user_id: str, session_id: str) -> None:
        """Accept a task request and intentionally do nothing."""

        _ = (task_name, user_id, session_id)


class BufferedPostTurnTaskQueue:
    """Collect tasks until the surrounding database transaction commits."""

    def __init__(self) -> None:
        self._tasks: list[PostTurnTask] = []

    async def enqueue(self, task_name: str, user_id: str, session_id: str) -> None:
        """Buffer a task without starting work inside the request transaction."""

        self._tasks.append(PostTurnTask(task_name, user_id, session_id))

    def drain(self) -> tuple[PostTurnTask, ...]:
        """Return and clear all buffered tasks after a successful commit."""

        tasks = tuple(self._tasks)
        self._tasks.clear()
        return tasks


BackgroundTaskHandler = Callable[
    [str, str, str],
    Coroutine[Any, Any, PostTurnWorkerResult | None],
]


class BackgroundPostTurnTaskQueue:
    """Run committed post-turn work in application-owned background tasks."""

    def __init__(self, handler: BackgroundTaskHandler) -> None:
        self._handler = handler
        self._tasks: set[asyncio.Task[PostTurnWorkerResult | None]] = set()
        self._session_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._closed = False

    @property
    def pending_count(self) -> int:
        """Return the number of tasks still running."""

        return len(self._tasks)

    async def enqueue(self, task_name: str, user_id: str, session_id: str) -> None:
        """Schedule committed work and return without waiting for inference."""

        if self._closed:
            raise RuntimeError("background post-turn queue is closed")
        task: asyncio.Task[PostTurnWorkerResult | None] = asyncio.create_task(
            self._run_serialized(task_name, user_id, session_id),
            name=f"post-turn:{user_id}:{session_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)

    async def aclose(self) -> None:
        """Stop accepting work and drain tasks before dependencies close."""

        if self._closed:
            return
        self._closed = True
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def _run_serialized(
        self,
        task_name: str,
        user_id: str,
        session_id: str,
    ) -> PostTurnWorkerResult | None:
        """Preserve task order within one conversation session."""

        lock = self._session_locks.setdefault((user_id, session_id), asyncio.Lock())
        async with lock:
            with timing_span("post_turn.background", task_name=task_name):
                return await self._handler(task_name, user_id, session_id)

    def _task_finished(
        self,
        task: asyncio.Task[PostTurnWorkerResult | None],
    ) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _LOGGER.error(
                "background_post_turn_failed",
                extra={"error_type": type(error).__name__},
            )


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


__all__ = [
    "BackgroundPostTurnTaskQueue",
    "BufferedPostTurnTaskQueue",
    "InlinePostTurnTaskQueue",
    "NoopTaskQueue",
    "PostTurnTask",
    "TaskQueue",
]
