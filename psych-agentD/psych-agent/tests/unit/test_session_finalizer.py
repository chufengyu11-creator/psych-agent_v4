"""Unit tests for close-session finalization."""

from agents.session_finalizer import FakeSessionFinalizer
from schemas.memory import (
    MemoryOperation,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from schemas.messages import MessageRole
from schemas.summary import SessionFinalizerInput
from tests.fixtures.messages import work_stress_dialogue
from tests.fixtures.states import empty_state


async def test_fake_session_finalizer_extracts_explicit_memory_candidate() -> None:
    """Finalizer should turn explicit user preferences into memory candidates."""

    messages = work_stress_dialogue()
    result = await FakeSessionFinalizer().finalize(
        SessionFinalizerInput(
            session_id=messages[0].session_id,
            messages=messages,
            final_state=empty_state(messages[0].session_id),
        )
    )

    assert result.session_id == messages[0].session_id
    assert result.session_summary
    assert result.source_message_ids
    assert len(result.candidate_memories) == 1
    candidate = result.candidate_memories[0]
    assert candidate.candidate_type == MemoryType.INTERACTION_PREFERENCE
    assert candidate.source_type == MemorySourceType.EXPLICIT_USER_STATEMENT
    assert candidate.sensitivity == MemorySensitivity.LOW
    assert candidate.recommended_operation == MemoryOperation.CREATE
    assert candidate.requires_user_confirmation is False
    assert candidate.source_message_ids == [messages[-1].id]


async def test_fake_session_finalizer_ignores_assistant_only_preferences() -> None:
    """Only user-authored preference statements should become memories."""

    messages = work_stress_dialogue()
    assistant_only_messages = [
        message for message in messages if message.role == MessageRole.ASSISTANT
    ]

    result = await FakeSessionFinalizer().finalize(
        SessionFinalizerInput(
            session_id=messages[0].session_id,
            messages=assistant_only_messages,
            final_state=empty_state(messages[0].session_id),
        )
    )

    assert result.candidate_memories == []