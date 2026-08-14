"""Independent tests for deterministic memory conflict resolution."""

import inspect

from schemas.common import MemoryId, MessageId, SourceReference
from schemas.memory import (
    LongTermMemory,
    MemoryCandidate,
    MemoryOperation,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from services import conflict_resolver as resolver_module
from services.conflict_resolver import (
    CONFLICTS_WITH_EXISTING_MEMORY,
    DUPLICATE_MEMORY,
    REINFORCES_EXISTING_MEMORY,
    SUPERSEDES_EXISTING_MEMORY,
    ConflictResolver,
    detect_memory_conflicts,
)


def _candidate(
    content: str,
    *,
    candidate_type: MemoryType = MemoryType.INTERACTION_PREFERENCE,
) -> MemoryCandidate:
    return MemoryCandidate(
        candidate_type=candidate_type,
        content=content,
        source_message_ids=[MessageId("msg_candidate")],
        source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
        confidence=0.9,
        requires_user_confirmation=False,
        sensitivity=MemorySensitivity.LOW,
        recommended_operation=MemoryOperation.CREATE,
    )


def _memory(
    memory_id: str,
    content: str,
    *,
    memory_type: MemoryType = MemoryType.INTERACTION_PREFERENCE,
) -> LongTermMemory:
    return LongTermMemory(
        id=MemoryId(memory_id),
        memory_type=memory_type,
        content=content,
        source=[SourceReference(message_id=MessageId("msg_existing"))],
    )


def test_no_existing_memories_returns_empty() -> None:
    assert ConflictResolver().detect(_candidate("one small step"), []) == []


def test_exact_duplicate_reinforces() -> None:
    candidate = _candidate("User prefers one small step at a time.")
    result = ConflictResolver().detect(
        candidate,
        [_memory("mem_duplicate", candidate.content)],
    )
    assert result[0].reason == DUPLICATE_MEMORY
    assert result[0].recommended_operation == MemoryOperation.REINFORCE


def test_containment_duplicate_reinforces() -> None:
    result = ConflictResolver().detect(
        _candidate("User prefers one small step at a time."),
        [_memory("mem_contained", "User prefers one small step at a time")],
    )
    assert result[0].reason == DUPLICATE_MEMORY


def test_compatible_paraphrase_reinforces() -> None:
    result = ConflictResolver().detect(
        _candidate("User prefers one small step at a time."),
        [_memory("mem_paraphrase", "User likes one small step at a time.")],
    )
    assert result[0].reason == REINFORCES_EXISTING_MEMORY
    assert result[0].recommended_operation == MemoryOperation.REINFORCE


def test_opposing_preference_marks_conflict() -> None:
    result = ConflictResolver().detect(
        _candidate("User wants a complete plan with all steps at once."),
        [_memory("mem_steps", "User prefers one small step at a time.")],
    )
    assert result[0].reason == CONFLICTS_WITH_EXISTING_MEMORY
    assert result[0].recommended_operation == MemoryOperation.MARK_CONFLICT


def test_explicit_update_supersedes() -> None:
    result = ConflictResolver().detect(
        _candidate("Now the user wants a complete plan with all steps at once."),
        [_memory("mem_old", "User prefers one small step at a time.")],
    )
    assert result[0].reason == SUPERSEDES_EXISTING_MEMORY
    assert result[0].recommended_operation == MemoryOperation.SUPERSEDE


def test_unrelated_type_and_topic_are_ignored() -> None:
    result = ConflictResolver().detect(
        _candidate("User prefers one small step at a time."),
        [
            _memory(
                "mem_goal",
                "User wants to improve workplace communication.",
                memory_type=MemoryType.ACTIVE_GOAL,
            )
        ],
    )
    assert result == []


def test_multiple_memories_return_only_related_relations() -> None:
    candidate = _candidate("User prefers one small step at a time.")
    result = ConflictResolver().detect(
        candidate,
        [
            _memory(
                "mem_unrelated",
                "User wants to improve workplace communication.",
                memory_type=MemoryType.ACTIVE_GOAL,
            ),
            _memory("mem_related", "User likes one small step at a time."),
        ],
    )
    assert [relation.existing_memory_id for relation in result] == [
        MemoryId("mem_related")
    ]


def test_relation_preserves_original_candidate() -> None:
    candidate = _candidate("User prefers one small step at a time.")
    result = ConflictResolver().detect(candidate, [_memory("mem", candidate.content)])
    assert result[0].candidate is candidate


def test_active_goal_continue_stop_marks_conflict() -> None:
    result = ConflictResolver().detect(
        _candidate(
            "User says do not continue talking about workplace communication.",
            candidate_type=MemoryType.ACTIVE_GOAL,
        ),
        [
            _memory(
                "mem_goal",
                "User wants to continue talking about workplace communication.",
                memory_type=MemoryType.ACTIVE_GOAL,
            )
        ],
    )
    assert result[0].recommended_operation == MemoryOperation.MARK_CONFLICT


def test_unknown_does_not_trigger_now_supersede_signal() -> None:
    result = ConflictResolver().detect(
        _candidate("Unknown preference: a complete plan with all steps at once."),
        [_memory("mem_old", "User prefers one small step at a time.")],
    )
    assert result[0].recommended_operation == MemoryOperation.MARK_CONFLICT
    assert result[0].reason == CONFLICTS_WITH_EXISTING_MEMORY


def test_function_and_service_entrypoints_are_equivalent() -> None:
    candidate = _candidate("User prefers one small step at a time.")
    memories = [_memory("mem", candidate.content)]
    assert detect_memory_conflicts(candidate, memories) == ConflictResolver().detect(
        candidate, memories
    )


def test_resolver_has_only_schema_level_imports() -> None:
    source = inspect.getsource(resolver_module)
    assert "from llm" not in source.casefold()
    assert "from storage" not in source.casefold()
