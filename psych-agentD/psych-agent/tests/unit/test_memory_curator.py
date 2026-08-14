"""Unit tests for deterministic memory candidate curation."""

from agents.memory_curator import MemoryCurator, MemoryCuratorInput
from schemas.common import MessageId, SessionId, SourceReference
from schemas.memory import (
    MemoryCandidate,
    MemoryOperation,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from schemas.messages import Message, MessageRole
from schemas.state import SessionState, StateItem
from schemas.summary import SessionFinalizerResult

SESSION_ID = SessionId("memory-curator-session")
CANONICAL_PREFERENCE = (
    "用户希望每次只收到一个小步骤，不要一次给太多建议。"
)


def _message(
    message_id: str,
    role: MessageRole,
    content: str,
    sequence_number: int = 1,
) -> Message:
    """Build one curator input message."""

    return Message(
        id=MessageId(message_id),
        session_id=SESSION_ID,
        role=role,
        content=content,
        sequence_number=sequence_number,
    )


def _candidate(message_id: str = "msg_preference") -> MemoryCandidate:
    """Build one canonical explicit preference candidate."""

    return MemoryCandidate(
        candidate_type=MemoryType.INTERACTION_PREFERENCE,
        content=CANONICAL_PREFERENCE,
        source_message_ids=[MessageId(message_id)],
        source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
        confidence=0.94,
        requires_user_confirmation=False,
        sensitivity=MemorySensitivity.LOW,
        recommended_operation=MemoryOperation.CREATE,
    )


def _payload(
    *,
    messages: list[Message] | None = None,
    final_state: SessionState | None = None,
    candidates: list[MemoryCandidate] | None = None,
) -> MemoryCuratorInput:
    """Build a compact curator input."""

    return MemoryCuratorInput(
        session_id=SESSION_ID,
        messages=messages or [],
        final_state=final_state or SessionState(session_id=SESSION_ID),
        finalizer_result=SessionFinalizerResult(
            session_id=SESSION_ID,
            session_summary="fixture session summary",
            candidate_memories=candidates or [],
        ),
    )


async def test_curator_preserves_grounded_finalizer_candidate() -> None:
    """An existing grounded finalizer candidate should survive curation."""

    message = _message("msg_preference", MessageRole.USER, "unrelated explicit text")
    candidate = _candidate()

    result = await MemoryCurator().curate(
        _payload(messages=[message], candidates=[candidate])
    )

    assert result == [candidate]


async def test_curator_extracts_user_small_step_preference() -> None:
    """Supported explicit user wording should become a low-risk preference candidate."""

    message = _message(
        "msg_preference",
        MessageRole.USER,
        "请每次一个小步骤，不要一次太多。",
    )

    result = await MemoryCurator().curate(_payload(messages=[message]))

    assert len(result) == 1
    candidate = result[0]
    assert candidate.candidate_type == MemoryType.INTERACTION_PREFERENCE
    assert candidate.content == CANONICAL_PREFERENCE
    assert candidate.source_message_ids == [message.id]
    assert candidate.source_type == MemorySourceType.EXPLICIT_USER_STATEMENT
    assert candidate.sensitivity == MemorySensitivity.LOW
    assert candidate.confidence >= 0.8


async def test_curator_extracts_sourced_state_preference() -> None:
    """A supported final-state preference should retain its state source ID."""

    source_id = MessageId("msg_state_preference")
    state = SessionState(
        session_id=SESSION_ID,
        user_preferences=[
            StateItem(
                value="prefers one step at a time",
                source=SourceReference(message_id=source_id),
            )
        ],
    )

    result = await MemoryCurator().curate(_payload(final_state=state))

    assert len(result) == 1
    assert result[0].source_message_ids == [source_id]


async def test_curator_ignores_assistant_only_preference() -> None:
    """Assistant-authored preference language must not create durable candidates."""

    message = _message(
        "msg_assistant",
        MessageRole.ASSISTANT,
        "I will give one small step at a time.",
    )

    result = await MemoryCurator().curate(_payload(messages=[message]))

    assert result == []


async def test_curator_discards_finalizer_candidate_with_unknown_source() -> None:
    """Finalizer candidates citing IDs outside curator inputs must be removed."""

    known = _message("msg_known", MessageRole.USER, "ordinary message")

    result = await MemoryCurator().curate(
        _payload(messages=[known], candidates=[_candidate("msg_unknown")])
    )

    assert result == []


async def test_curator_deduplicates_same_candidate_and_source() -> None:
    """Finalizer and deterministic extraction must not duplicate identical provenance."""

    message = _message(
        "msg_preference",
        MessageRole.USER,
        "每次一个小步骤，不要一次太多。",
    )

    result = await MemoryCurator().curate(
        _payload(messages=[message], candidates=[_candidate()])
    )

    assert result == [_candidate()]
