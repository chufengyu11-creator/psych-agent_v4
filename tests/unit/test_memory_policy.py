"""Unit tests for deterministic memory conflict and policy services."""

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
    DUPLICATE_MEMORY,
    SUPERSEDES_EXISTING_MEMORY,
    ConflictResolver,
)
from services.memory_policy import (
    ALLOWED_EXPLICIT_INTERACTION_PREFERENCE,
    ALLOWED_EXPLICIT_LOW_SENSITIVITY_MEMORY,
    MEDIUM_SENSITIVITY_REQUIRES_CONFIRMATION,
    MEMORY_DISABLED,
    MODEL_INFERENCE_NOT_ALLOWED,
    SESSION_SUMMARY_REQUIRES_CONFIRMATION,
    MemoryPolicy,
)


def _candidate(
    content: str,
    *,
    candidate_type: MemoryType = MemoryType.INTERACTION_PREFERENCE,
    confidence: float = 0.9,
    operation: MemoryOperation = MemoryOperation.CREATE,
    requires_confirmation: bool = False,
    sensitivity: MemorySensitivity = MemorySensitivity.LOW,
    source_type: MemorySourceType = MemorySourceType.EXPLICIT_USER_STATEMENT,
) -> MemoryCandidate:
    """Build a policy-ready memory candidate."""

    return MemoryCandidate(
        candidate_type=candidate_type,
        content=content,
        source_message_ids=[MessageId("msg_memory_policy_1")],
        source_type=source_type,
        confidence=confidence,
        requires_user_confirmation=requires_confirmation,
        sensitivity=sensitivity,
        recommended_operation=operation,
    )


def _memory(
    content: str,
    *,
    memory_id: str = "mem_existing_1",
    memory_type: MemoryType = MemoryType.INTERACTION_PREFERENCE,
) -> LongTermMemory:
    """Build an existing authorized memory."""

    return LongTermMemory(
        id=MemoryId(memory_id),
        memory_type=memory_type,
        content=content,
        source=[SourceReference(message_id=MessageId("msg_existing_1"))],
    )


def test_conflict_resolver_detects_duplicate_memory() -> None:
    """Same-type repeated content should reinforce the existing memory."""

    candidate = _candidate("User prefers one small step at a time.")
    existing = _memory("User prefers one small step at a time.")

    conflicts = ConflictResolver().detect(candidate, [existing])

    assert len(conflicts) == 1
    assert conflicts[0].reason == DUPLICATE_MEMORY
    assert conflicts[0].recommended_operation == MemoryOperation.REINFORCE
    assert conflicts[0].existing_memory_id == MemoryId("mem_existing_1")


def test_conflict_resolver_detects_superseding_preference() -> None:
    """A new explicit update should supersede an opposite old preference."""

    candidate = _candidate("Now the user wants a complete plan all at once.")
    existing = _memory("User prefers one small step at a time.")

    conflicts = ConflictResolver().detect(candidate, [existing])

    assert len(conflicts) == 1
    assert conflicts[0].reason == SUPERSEDES_EXISTING_MEMORY
    assert conflicts[0].recommended_operation == MemoryOperation.SUPERSEDE


def test_memory_policy_allows_clear_explicit_low_sensitivity_preference() -> None:
    """A clear explicit interaction preference can be created without confirmation."""

    decision = MemoryPolicy().evaluate_candidate(
        MemoryPolicyInput(
            candidate=_candidate("User prefers one small step at a time."),
            user_memory_enabled=True,
        )
    )

    assert decision.allowed is True
    assert decision.operation == MemoryOperation.CREATE
    assert decision.requires_user_confirmation is False
    assert decision.reason_codes == [ALLOWED_EXPLICIT_INTERACTION_PREFERENCE]


def test_memory_policy_allows_clear_explicit_low_sensitivity_active_goal() -> None:
    """Explicit low-risk user goals should not always wait for confirmation."""

    decision = MemoryPolicy().evaluate_candidate(
        MemoryPolicyInput(
            candidate=_candidate(
                "用户是博士四年级即将五年级，正在准备毕业和就业。",
                candidate_type=MemoryType.ACTIVE_GOAL,
                requires_confirmation=True,
            ),
            user_memory_enabled=True,
        )
    )

    assert decision.allowed is True
    assert decision.operation == MemoryOperation.CREATE
    assert decision.requires_user_confirmation is False
    assert decision.reason_codes == [ALLOWED_EXPLICIT_LOW_SENSITIVITY_MEMORY]


def test_memory_policy_keeps_session_summary_unfinished_topic_pending() -> None:
    """Summary-derived topics should still require confirmation."""

    decision = MemoryPolicy().evaluate_candidate(
        MemoryPolicyInput(
            candidate=_candidate(
                "论文提交后的压力、博士毕业压力、写论文压力、找工作准备毕业",
                candidate_type=MemoryType.UNFINISHED_TOPIC,
                source_type=MemorySourceType.SESSION_SUMMARY,
            ),
            user_memory_enabled=True,
        )
    )

    assert decision.allowed is True
    assert decision.operation == MemoryOperation.CREATE
    assert decision.requires_user_confirmation is True
    assert SESSION_SUMMARY_REQUIRES_CONFIRMATION in decision.reason_codes


def test_memory_policy_keeps_medium_sensitivity_active_goal_pending() -> None:
    """Higher-sensitivity goals should still require confirmation."""

    decision = MemoryPolicy().evaluate_candidate(
        MemoryPolicyInput(
            candidate=_candidate(
                "用户正在处理一个较敏感的家庭目标。",
                candidate_type=MemoryType.ACTIVE_GOAL,
                sensitivity=MemorySensitivity.MEDIUM,
            ),
            user_memory_enabled=True,
        )
    )

    assert decision.allowed is True
    assert decision.operation == MemoryOperation.CREATE
    assert decision.requires_user_confirmation is True
    assert MEDIUM_SENSITIVITY_REQUIRES_CONFIRMATION in decision.reason_codes


def test_memory_policy_rejects_when_user_memory_is_disabled() -> None:
    """No long-term memory write is allowed when the user disabled memory."""

    decision = MemoryPolicy().evaluate_candidate(
        MemoryPolicyInput(
            candidate=_candidate("User prefers one small step at a time."),
            user_memory_enabled=False,
        )
    )

    assert decision.allowed is False
    assert decision.operation is None
    assert decision.reason_codes == [MEMORY_DISABLED]


def test_memory_policy_rejects_model_inference_for_durable_memory() -> None:
    """Model-only inferences should not become durable memories directly."""

    decision = MemoryPolicy().evaluate_candidate(
        MemoryPolicyInput(
            candidate=_candidate(
                "User probably avoids direct communication.",
                source_type=MemorySourceType.MODEL_INFERENCE,
            ),
            user_memory_enabled=True,
        )
    )

    assert decision.allowed is False
    assert decision.reason_codes == [MODEL_INFERENCE_NOT_ALLOWED]


def test_memory_policy_reinforces_duplicate_existing_memory() -> None:
    """A duplicate candidate should target the existing memory instead of creating."""

    existing = _memory("User prefers one small step at a time.")
    decision = MemoryPolicy().evaluate_candidate(
        MemoryPolicyInput(
            candidate=_candidate("User prefers one small step at a time."),
            existing_memories=[existing],
            user_memory_enabled=True,
        )
    )

    assert decision.allowed is True
    assert decision.operation == MemoryOperation.REINFORCE
    assert decision.target_memory_id == existing.id
    assert decision.requires_user_confirmation is False
