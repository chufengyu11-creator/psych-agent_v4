"""Reusable memory fixtures."""

from schemas.common import MemoryId, MessageId, SessionId, SourceReference, UserId
from schemas.memory import (
    LongTermMemory,
    MemoryCandidate,
    MemoryOperation,
    MemoryPolicyInput,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
    RetrievedMemories,
)


def interaction_preference_candidate() -> MemoryCandidate:
    """Return a candidate memory for a user interaction preference."""

    return MemoryCandidate(
        candidate_type=MemoryType.INTERACTION_PREFERENCE,
        content="用户希望一次只收到一个小步骤，不喜欢一次太多建议。",
        source_message_ids=[MessageId("msg_005")],
        source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
        confidence=0.94,
        requires_user_confirmation=False,
        sensitivity=MemorySensitivity.LOW,
        recommended_operation=MemoryOperation.CREATE,
    )


def active_goal_memory() -> LongTermMemory:
    """Return an existing active-goal long-term memory."""

    return LongTermMemory(
        id=MemoryId("mem_001"),
        memory_type=MemoryType.ACTIVE_GOAL,
        content="用户想改善和直属领导的沟通。",
        sensitivity=MemorySensitivity.MEDIUM,
        confidence=0.86,
        source=[SourceReference(message_id=MessageId("msg_001"))],
    )


def retrieved_work_stress_memories() -> RetrievedMemories:
    """Return memories relevant to the work-stress fixture dialogue."""

    return RetrievedMemories(
        semantic_memories=[],
        episodic_memories=[active_goal_memory()],
        active_goals=["改善和直属领导的沟通"],
        interaction_preferences=["每次只给一个小步骤"],
        previous_session_summary="上次用户提到和直属领导沟通紧张，想寻找低压力沟通方式。",
    )


def memory_policy_input() -> MemoryPolicyInput:
    """Return policy input for the interaction-preference candidate."""

    return MemoryPolicyInput(
        candidate=interaction_preference_candidate(),
        existing_memories=[active_goal_memory()],
        user_memory_enabled=True,
        session_id=SessionId("session_fixture_work_stress"),
    )


def fixture_user_id() -> UserId:
    """Return the default user ID for memory fixtures."""

    return UserId("user_fixture_001")
