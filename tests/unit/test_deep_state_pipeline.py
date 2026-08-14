"""Tests for bounded shadow deep-state execution."""

import asyncio

from orchestrator.deep_state_pipeline import (
    BackgroundDeepStateTaskQueue,
    DeepStateTask,
)
from schemas.common import MessageId, SessionId
from schemas.messages import Message, MessageRole
from schemas.state import SessionState, StateDelta, StateTrackerInput


def _deep_task(session_id: str) -> DeepStateTask:
    typed_session_id = SessionId(session_id)
    message = Message(
        id=MessageId(f"msg_{session_id}"),
        session_id=typed_session_id,
        role=MessageRole.USER,
        content="test message",
        sequence_number=1,
    )
    return DeepStateTask(
        user_id="user_1",
        session_id=session_id,
        source_message_id=str(message.id),
        source_message_sequence=message.sequence_number,
        base_state_version=0,
        payload=StateTrackerInput(
            current_message=message,
            previous_state=SessionState(session_id=typed_session_id),
        ),
    )


async def test_enqueue_returns_before_inference_and_session_wait_drains() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(payload: StateTrackerInput) -> StateDelta:
        started.set()
        await release.wait()
        return StateDelta(explicit_user_request=payload.current_message.content)

    queue = BackgroundDeepStateTaskQueue(handler)
    await queue.enqueue(_deep_task("session_one"))
    await asyncio.wait_for(started.wait(), timeout=1)

    waiter = asyncio.create_task(queue.wait_for_session("user_1", "session_one"))
    await asyncio.sleep(0)
    assert waiter.done() is False
    assert queue.pending_count == 1

    release.set()
    await asyncio.wait_for(waiter, timeout=1)
    assert queue.completed_count == 1
    assert queue.failed_count == 0
    assert queue.pending_count == 0
    await queue.aclose()


async def test_background_failure_is_consumed_and_non_fatal() -> None:
    async def handler(payload: StateTrackerInput) -> StateDelta:
        raise RuntimeError(payload.current_message.content)

    queue = BackgroundDeepStateTaskQueue(handler)
    await queue.enqueue(_deep_task("session_failure"))
    await queue.wait_for_session("user_1", "session_failure")
    await asyncio.sleep(0)

    assert queue.completed_count == 0
    assert queue.failed_count == 1
    assert queue.pending_count == 0
    await queue.aclose()


async def test_global_concurrency_limit_serializes_different_sessions() -> None:
    active = 0
    peak = 0
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def handler(payload: StateTrackerInput) -> StateDelta:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        first_started.set()
        await release.wait()
        active -= 1
        return StateDelta(explicit_user_request=payload.current_message.content)

    queue = BackgroundDeepStateTaskQueue(handler, max_concurrency=1)
    await queue.enqueue(_deep_task("session_a"))
    await queue.enqueue(_deep_task("session_b"))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert peak == 1

    release.set()
    await queue.wait_for_session("user_1", "session_a")
    await queue.wait_for_session("user_1", "session_b")
    assert peak == 1
    assert queue.completed_count == 2
    await queue.aclose()
async def test_result_persists_only_after_foreground_commit() -> None:
    inference_finished = asyncio.Event()
    persisted: list[str] = []

    async def handler(payload: StateTrackerInput) -> StateDelta:
        inference_finished.set()
        return StateDelta(explicit_user_request=payload.current_message.content)

    async def persist(task: DeepStateTask, result: StateDelta) -> None:
        _ = result
        persisted.append(task.source_message_id)

    task = _deep_task("session_commit")
    queue = BackgroundDeepStateTaskQueue(handler, result_handler=persist)
    await queue.enqueue(task)
    await asyncio.wait_for(inference_finished.wait(), timeout=1)
    await asyncio.sleep(0)

    assert persisted == []
    assert queue.pending_count == 1

    queue.mark_committed(task.source_message_id)
    await queue.wait_for_session("user_1", "session_commit")

    assert persisted == [task.source_message_id]
    assert queue.completed_count == 1
    assert queue.discarded_count == 0
    await queue.aclose()


async def test_rolled_back_turn_discards_completed_result() -> None:
    inference_finished = asyncio.Event()
    persisted: list[str] = []

    async def handler(payload: StateTrackerInput) -> StateDelta:
        inference_finished.set()
        return StateDelta(explicit_user_request=payload.current_message.content)

    async def persist(task: DeepStateTask, result: StateDelta) -> None:
        _ = result
        persisted.append(task.source_message_id)

    task = _deep_task("session_rollback")
    queue = BackgroundDeepStateTaskQueue(handler, result_handler=persist)
    await queue.enqueue(task)
    await asyncio.wait_for(inference_finished.wait(), timeout=1)

    queue.mark_rolled_back(task.source_message_id)
    await queue.wait_for_session("user_1", "session_rollback")

    assert persisted == []
    assert queue.completed_count == 0
    assert queue.discarded_count == 1
    await queue.aclose()
