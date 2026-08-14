"""Application-owned background execution for persisted deep-state analysis."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from schemas.state import StateDelta, StateTrackerInput
from services.timing import current_trace_id, timing_span

_LOGGER = logging.getLogger("uvicorn.error")
SessionKey = tuple[str, str]
DeepStateHandler = Callable[[StateTrackerInput], Awaitable[StateDelta]]


@dataclass(frozen=True, slots=True)
class DeepStateTask:
    """Immutable input snapshot for one non-blocking deep-state analysis."""

    user_id: str
    session_id: str
    source_message_id: str
    source_message_sequence: int
    base_state_version: int
    payload: StateTrackerInput


DeepStateResultHandler = Callable[[DeepStateTask, StateDelta], Awaitable[None]]
DeepStateFailureHandler = Callable[[DeepStateTask, str], Awaitable[None]]


@dataclass(slots=True)
class _CommitGate:
    """Keep background persistence behind the foreground transaction boundary."""

    event: asyncio.Event = field(default_factory=asyncio.Event)
    committed: bool | None = None


class DeepStateTaskQueue(Protocol):
    """Queue boundary for non-blocking deep analysis."""

    async def enqueue(self, task: DeepStateTask) -> None:
        """Schedule one deep-state analysis and return immediately."""

    def mark_committed(self, source_message_id: str) -> None:
        """Allow a completed result to persist after its turn commits."""

    def mark_rolled_back(self, source_message_id: str) -> None:
        """Discard a result whose foreground turn rolled back."""

    async def wait_for_session(self, user_id: str, session_id: str) -> None:
        """Wait for currently scheduled deep work for one session."""

    async def aclose(self) -> None:
        """Stop accepting work and drain owned tasks."""


class BackgroundDeepStateTaskQueue:
    """Run bounded deep analysis and persist only after foreground commit."""

    def __init__(
        self,
        handler: DeepStateHandler,
        *,
        result_handler: DeepStateResultHandler | None = None,
        failure_handler: DeepStateFailureHandler | None = None,
        max_concurrency: int = 1,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._handler = handler
        self._result_handler = result_handler
        self._failure_handler = failure_handler
        self._requires_commit = result_handler is not None or failure_handler is not None
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._session_locks: dict[SessionKey, asyncio.Lock] = {}
        self._latest_by_session: dict[
            SessionKey,
            asyncio.Task[StateDelta | None],
        ] = {}
        self._tasks: set[asyncio.Task[StateDelta | None]] = set()
        self._commit_gates: dict[str, _CommitGate] = {}
        self._closed = False
        self._completed_count = 0
        self._failed_count = 0
        self._discarded_count = 0

    @property
    def pending_count(self) -> int:
        """Return how many deep tasks are still running or queued."""

        return len(self._tasks)

    @property
    def completed_count(self) -> int:
        """Return how many deep tasks completed and persisted successfully."""

        return self._completed_count

    @property
    def failed_count(self) -> int:
        """Return how many deep tasks failed or were cancelled."""

        return self._failed_count

    @property
    def discarded_count(self) -> int:
        """Return how many results were discarded after foreground rollback."""

        return self._discarded_count

    async def enqueue(self, task: DeepStateTask) -> None:
        """Copy the payload and schedule analysis without awaiting inference."""

        if self._closed:
            raise RuntimeError("deep-state task queue is closed")
        if task.source_message_id in self._commit_gates:
            raise ValueError("deep-state task already exists for this source message")
        snapshot = DeepStateTask(
            user_id=task.user_id,
            session_id=task.session_id,
            source_message_id=task.source_message_id,
            source_message_sequence=task.source_message_sequence,
            base_state_version=task.base_state_version,
            payload=task.payload.model_copy(deep=True),
        )
        gate = _CommitGate()
        if not self._requires_commit:
            gate.committed = True
            gate.event.set()
        self._commit_gates[snapshot.source_message_id] = gate
        key = (snapshot.user_id, snapshot.session_id)
        background_task: asyncio.Task[StateDelta | None] = asyncio.create_task(
            self._run_serialized(snapshot),
            name=(
                f"deep-state:{snapshot.user_id}:{snapshot.session_id}:"
                f"{snapshot.source_message_id}"
            ),
        )
        self._tasks.add(background_task)
        self._latest_by_session[key] = background_task
        background_task.add_done_callback(
            lambda completed, session_key=key, source_id=snapshot.source_message_id: (
                self._task_finished(session_key, source_id, completed)
            )
        )

    def mark_committed(self, source_message_id: str) -> None:
        """Release persistence for a successfully committed foreground turn."""

        self._resolve_gate(source_message_id, committed=True)

    def mark_rolled_back(self, source_message_id: str) -> None:
        """Release and discard work from a rolled-back foreground turn."""

        self._resolve_gate(source_message_id, committed=False)

    async def wait_for_session(self, user_id: str, session_id: str) -> None:
        """Wait without propagating deep-task failures to dependent work."""

        task = self._latest_by_session.get((user_id, session_id))
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def aclose(self) -> None:
        """Stop accepting tasks and drain all owned work before shutdown."""

        if self._closed:
            return
        self._closed = True
        for gate in self._commit_gates.values():
            if gate.committed is None:
                gate.committed = False
                gate.event.set()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def _run_serialized(self, task: DeepStateTask) -> StateDelta | None:
        """Serialize one session, bound inference, then persist after commit."""

        key = (task.user_id, task.session_id)
        lock = self._session_locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                async with self._semaphore:
                    with timing_span(
                        "deep_state.inference",
                        base_state_version=task.base_state_version,
                    ):
                        result = await self._handler(task.payload)
            except BaseException as error:
                if await self._wait_for_commit(task.source_message_id):
                    if self._failure_handler is not None:
                        with timing_span("deep_state.persist_failed"):
                            await self._failure_handler(task, type(error).__name__)
                raise

            if not await self._wait_for_commit(task.source_message_id):
                self._discarded_count += 1
                return None
            if self._result_handler is not None:
                with timing_span("deep_state.persist_ready"):
                    await self._result_handler(task, result)
            self._completed_count += 1
            _LOGGER.info(
                "DEEP_STATE_READY %s",
                json.dumps(
                    {
                        "trace_id": current_trace_id() or "unbound",
                        "base_state_version": task.base_state_version,
                        "source_message_sequence": task.source_message_sequence,
                        "topic_update_count": len(result.topic_updates),
                        "goal_update_count": len(result.goal_updates),
                        "emotion_count": len(result.reported_emotions),
                        "correction_count": len(result.user_corrections),
                        "preference_count": len(result.strategy_preferences),
                        "hypothesis_count": len(result.hypotheses),
                        "status": "ready",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            return result

    async def _wait_for_commit(self, source_message_id: str) -> bool:
        gate = self._commit_gates[source_message_id]
        await gate.event.wait()
        return gate.committed is True

    def _resolve_gate(self, source_message_id: str, *, committed: bool) -> None:
        gate = self._commit_gates.get(source_message_id)
        if gate is None or gate.committed is not None:
            return
        gate.committed = committed
        gate.event.set()

    def _task_finished(
        self,
        key: SessionKey,
        source_message_id: str,
        task: asyncio.Task[StateDelta | None],
    ) -> None:
        """Consume exceptions and release completed task bookkeeping."""

        self._tasks.discard(task)
        self._commit_gates.pop(source_message_id, None)
        if self._latest_by_session.get(key) is task:
            self._latest_by_session.pop(key, None)
        if task.cancelled():
            self._failed_count += 1
            return
        error = task.exception()
        if error is not None:
            self._failed_count += 1
            _LOGGER.error(
                "deep_state_pipeline_failed",
                extra={"error_type": type(error).__name__},
            )


__all__ = [
    "BackgroundDeepStateTaskQueue",
    "DeepStateFailureHandler",
    "DeepStateResultHandler",
    "DeepStateTask",
    "DeepStateTaskQueue",
]
