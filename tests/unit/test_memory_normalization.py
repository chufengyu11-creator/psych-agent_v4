"""Tests for write-side memory normalization layers L0-L3.

Covers ephemeral emotion interception (L0), semantic similarity dedup (L2),
and attribute value conflicts (L3). Exact duplicate handling (L1) is already
covered by test_conflict_resolver.py.
"""

from schemas.common import MemoryId, MessageId, SourceReference
from schemas.memory import (
    LongTermMemory,
    MemoryCandidate,
    MemoryOperation,
    MemoryPolicyInput,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from services.conflict_resolver import (
    ATTRIBUTE_VALUE_CONFLICT,
    ATTRIBUTE_VALUE_SUPERSEDE,
    DUPLICATE_MEMORY,
    REINFORCES_EXISTING_MEMORY,
    ConflictResolver,
)
from services.memory_policy import (
    ALLOWED_EXPLICIT_LOW_SENSITIVITY_MEMORY,
    EPHEMERAL_EMOTION_NOT_DURABLE,
    UNFINISHED_TOPIC_REQUIRES_CONFIRMATION,
    MemoryPolicy,
)


def _candidate(
    content: str,
    *,
    candidate_type: MemoryType = MemoryType.SEMANTIC,
    source_type: MemorySourceType = MemorySourceType.EXPLICIT_USER_STATEMENT,
) -> MemoryCandidate:
    """Build one explicit low-sensitivity semantic candidate."""

    return MemoryCandidate(
        candidate_type=candidate_type,
        content=content,
        source_message_ids=[MessageId("msg_normalization_candidate")],
        source_type=source_type,
        confidence=0.9,
        requires_user_confirmation=False,
        sensitivity=MemorySensitivity.LOW,
        recommended_operation=MemoryOperation.CREATE,
    )


def _memory(
    content: str,
    *,
    memory_id: str = "mem_normalization_existing",
    memory_type: MemoryType = MemoryType.SEMANTIC,
) -> LongTermMemory:
    """Build one existing active memory."""

    return LongTermMemory(
        id=MemoryId(memory_id),
        memory_type=memory_type,
        content=content,
        source=[SourceReference(message_id=MessageId("msg_normalization_existing"))],
    )


# ---------------------------------------------------------------------------
# L0: ephemeral emotion interception
# ---------------------------------------------------------------------------


def test_ephemeral_emotion_is_not_durable() -> None:
    """A momentary emotion must never become a cross-session semantic fact."""

    decision = MemoryPolicy().evaluate_candidate(
        MemoryPolicyInput(
            candidate=_candidate("用户今天很难过"),
            user_memory_enabled=True,
        )
    )

    assert decision.allowed is False
    assert decision.reason_codes == [EPHEMERAL_EMOTION_NOT_DURABLE]


def test_positive_ephemeral_emotion_is_not_durable() -> None:
    """Shared emotion vocabulary must also reject momentary positive states."""

    decision = MemoryPolicy().evaluate_candidate(
        MemoryPolicyInput(
            candidate=_candidate("\u7528\u6237\u4eca\u5929\u5f88\u5f00\u5fc3"),
            user_memory_enabled=True,
        )
    )

    assert decision.allowed is False
    assert decision.reason_codes == [EPHEMERAL_EMOTION_NOT_DURABLE]


def test_emotion_with_stability_marker_can_proceed() -> None:
    """A lasting emotional state may pass the ephemeral gate."""

    decision = MemoryPolicy().evaluate_candidate(
        MemoryPolicyInput(
            candidate=_candidate("用户自从母亲去世后一直很难过"),
            user_memory_enabled=True,
        )
    )

    assert decision.allowed is True
    assert decision.operation == MemoryOperation.CREATE
    assert ALLOWED_EXPLICIT_LOW_SENSITIVITY_MEMORY in decision.reason_codes


def test_unfinished_topic_pressure_is_not_emotion_blocked() -> None:
    """Emotion interception must not block non-fact memory types."""

    decision = MemoryPolicy().evaluate_candidate(
        MemoryPolicyInput(
            candidate=_candidate(
                "论文提交后的压力、博士毕业压力、找工作准备毕业",
                candidate_type=MemoryType.UNFINISHED_TOPIC,
                source_type=MemorySourceType.SESSION_SUMMARY,
            ),
            user_memory_enabled=True,
        )
    )

    assert decision.allowed is True
    assert decision.operation == MemoryOperation.CREATE
    assert UNFINISHED_TOPIC_REQUIRES_CONFIRMATION in decision.reason_codes


# ---------------------------------------------------------------------------
# L2: semantic similarity dedup
# ---------------------------------------------------------------------------


def test_semantic_paraphrase_reinforces_existing_memory() -> None:
    """A near-duplicate restatement should reinforce, not create a new row."""

    result = ConflictResolver().detect(
        _candidate("用户爱吃辣"),
        [_memory("用户超爱吃辣")],
    )

    assert len(result) == 1
    assert result[0].recommended_operation == MemoryOperation.REINFORCE
    assert result[0].reason == REINFORCES_EXISTING_MEMORY


def test_semantically_unrelated_memories_are_ignored() -> None:
    """Different topics with little overlap must not conflict."""

    result = ConflictResolver().detect(
        _candidate("用户爱吃辣"),
        [_memory("用户喜欢跑步锻炼")],
    )

    assert result == []


# ---------------------------------------------------------------------------
# L3: attribute value conflicts
# ---------------------------------------------------------------------------


def test_attribute_value_with_update_signal_supersedes() -> None:
    """An explicit correction of the same attribute must supersede the old value."""

    result = ConflictResolver().detect(
        _candidate("小明现在18岁"),
        [_memory("小明今年40岁")],
    )

    assert len(result) == 1
    assert result[0].reason == ATTRIBUTE_VALUE_SUPERSEDE
    assert result[0].recommended_operation == MemoryOperation.SUPERSEDE


def test_attribute_value_without_signal_marks_conflict() -> None:
    """A contradictory value without a correction signal must wait for review."""

    result = ConflictResolver().detect(
        _candidate("小明18岁"),
        [_memory("小明40岁")],
    )

    assert len(result) == 1
    assert result[0].reason == ATTRIBUTE_VALUE_CONFLICT
    assert result[0].recommended_operation == MemoryOperation.MARK_CONFLICT


def test_different_subject_same_attribute_is_not_conflict() -> None:
    """Age values of different people must not be treated as one conflict."""

    result = ConflictResolver().detect(
        _candidate("小红18岁"),
        [_memory("小明40岁")],
    )

    assert result == []


def test_same_attribute_value_is_exact_duplicate() -> None:
    """Restating the same value for the same attribute is a duplicate."""

    result = ConflictResolver().detect(
        _candidate("小明今年40岁"),
        [_memory("小明今年40岁")],
    )

    assert result[0].reason == DUPLICATE_MEMORY
    assert result[0].recommended_operation == MemoryOperation.REINFORCE


def test_policy_supersede_requires_confirmation_and_targets_old_memory() -> None:
    """The policy decision for a superseding candidate must be confirmable."""

    existing = _memory("小明今年40岁")
    decision = MemoryPolicy().evaluate_candidate(
        MemoryPolicyInput(
            candidate=_candidate("小明现在18岁"),
            existing_memories=[existing],
            user_memory_enabled=True,
        )
    )

    assert decision.allowed is True
    assert decision.operation == MemoryOperation.SUPERSEDE
    assert decision.requires_user_confirmation is True
    assert decision.target_memory_id == existing.id
