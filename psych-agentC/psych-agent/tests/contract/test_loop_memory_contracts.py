"""Contract tests for loop, summary, memory, and worker event schemas."""

from schemas.common import InterventionId, MemoryId, MessageId, SessionId, UserId
from schemas.events import PostTurnEvent
from schemas.intervention import InterventionRecord, InterventionStatus
from schemas.memory import (
    LongTermMemory,
    MemoryCandidate,
    MemoryOperation,
    MemoryPolicyDecision,
    MemoryPolicyInput,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from schemas.messages import Message, MessageRole
from schemas.risk import RiskLevel
from schemas.state import SessionState
from schemas.summary import (
    ActionItem,
    RollingSummarizerInput,
    SessionFinalizerInput,
    SessionFinalizerResult,
)


def _message(message_id: str, content: str, sequence_number: int) -> Message:
    """Create a message fixture for contract tests."""

    return Message(
        id=MessageId(message_id),
        session_id=SessionId("session_1"),
        role=MessageRole.USER,
        content=content,
        sequence_number=sequence_number,
    )


def test_rolling_summarizer_input_contract() -> None:
    """RollingSummarizerInput should bind messages, state, and interventions."""

    state = SessionState(session_id=SessionId("session_1"))
    intervention = InterventionRecord(
        intervention_id=InterventionId("int_1"),
        session_id=SessionId("session_1"),
        assistant_message_id=MessageId("msg_2"),
        strategy="reflective_listening",
        objective="帮助用户表达当前困扰",
        status=InterventionStatus.PENDING,
    )

    payload = RollingSummarizerInput(
        session_id=SessionId("session_1"),
        uncovered_messages=[_message("msg_1", "最近压力很大", 1)],
        current_state=state,
        interventions=[intervention],
    )

    assert payload.current_state.risk_state.level == RiskLevel.LOW
    assert payload.interventions[0].strategy == "reflective_listening"


def test_session_finalizer_result_contract() -> None:
    """SessionFinalizerResult should carry summary, actions, and candidates."""

    candidate = MemoryCandidate(
        candidate_type=MemoryType.INTERACTION_PREFERENCE,
        content="用户希望一次只收到少量建议",
        source_message_ids=[MessageId("msg_1")],
        source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
        confidence=0.9,
        requires_user_confirmation=False,
        sensitivity=MemorySensitivity.LOW,
        recommended_operation=MemoryOperation.CREATE,
    )
    result = SessionFinalizerResult(
        session_id=SessionId("session_1"),
        session_summary="用户讨论了工作压力，并希望获得低压力行动建议。",
        action_items=[ActionItem(content="选择一个低压力沟通步骤")],
        candidate_memories=[candidate],
        source_message_ids=[MessageId("msg_1")],
    )

    assert result.candidate_memories[0].recommended_operation == MemoryOperation.CREATE
    assert result.action_items[0].completed is False


def test_session_finalizer_input_contract() -> None:
    """SessionFinalizerInput should bundle all close-session source material."""

    state = SessionState(session_id=SessionId("session_1"))
    payload = SessionFinalizerInput(
        session_id=SessionId("session_1"),
        messages=[_message("msg_1", "我想先到这里", 1)],
        final_state=state,
    )

    assert payload.messages[0].id == "msg_1"
    assert payload.final_state.version == 0


def test_memory_policy_contract() -> None:
    """MemoryPolicyInput and MemoryPolicyDecision should describe write approval."""

    candidate = MemoryCandidate(
        candidate_type=MemoryType.ACTIVE_GOAL,
        content="用户想改善和直属领导的沟通",
        source_message_ids=[MessageId("msg_3")],
        source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
        confidence=0.85,
        requires_user_confirmation=False,
        sensitivity=MemorySensitivity.MEDIUM,
        recommended_operation=MemoryOperation.CREATE,
    )
    existing = LongTermMemory(
        id=MemoryId("mem_1"),
        memory_type=MemoryType.ACTIVE_GOAL,
        content="用户关注工作沟通问题",
        sensitivity=MemorySensitivity.MEDIUM,
    )
    payload = MemoryPolicyInput(
        candidate=candidate,
        existing_memories=[existing],
        user_memory_enabled=True,
        session_id=SessionId("session_1"),
    )
    decision = MemoryPolicyDecision(
        allowed=True,
        operation=MemoryOperation.CREATE,
        reason_codes=["explicit_user_statement"],
    )

    assert payload.existing_memories[0].id == "mem_1"
    assert decision.allowed is True


def test_post_turn_event_contract() -> None:
    """PostTurnEvent should carry the IDs workers need after a turn."""

    event = PostTurnEvent(
        session_id=SessionId("session_1"),
        user_id=UserId("user_1"),
        user_message_id=MessageId("msg_1"),
        assistant_message_id=MessageId("msg_2"),
        state_version=1,
    )

    assert event.event_type == "post_turn"
    assert event.state_version == 1

