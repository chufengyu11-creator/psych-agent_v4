"""Reusable message fixtures for multi-turn dialogue tests."""

from schemas.common import MessageId, SessionId
from schemas.messages import Message, MessageRole

DEFAULT_SESSION_ID = SessionId("session_fixture_work_stress")


def make_message(
    message_id: str,
    role: MessageRole,
    content: str,
    sequence_number: int,
    session_id: SessionId = DEFAULT_SESSION_ID,
) -> Message:
    """Create a typed Message fixture."""

    return Message(
        id=MessageId(message_id),
        session_id=session_id,
        role=role,
        content=content,
        sequence_number=sequence_number,
    )


def work_stress_dialogue() -> list[Message]:
    """Return a multi-turn work-stress dialogue for adaptive-loop tests."""

    return [
        make_message(
            "msg_001",
            MessageRole.USER,
            "我最近和直属领导沟通很紧张，每次开会前都会焦虑。",
            1,
        ),
        make_message(
            "msg_002",
            MessageRole.ASSISTANT,
            "听起来这段沟通关系让你一直处在紧绷里。",
            2,
        ),
        make_message(
            "msg_003",
            MessageRole.USER,
            "我不想继续分析情绪，我现在更想知道下一步具体怎么做。",
            3,
        ),
        make_message(
            "msg_004",
            MessageRole.ASSISTANT,
            "那我们先选一个低压力的小步骤，比如写下想沟通的一句话。",
            4,
        ),
        make_message(
            "msg_005",
            MessageRole.USER,
            "可以，而且我希望建议不要一次太多，每次一个小步骤就好。",
            5,
        ),
    ]


def safety_check_dialogue() -> list[Message]:
    """Return a short dialogue containing a safety-risk user message."""

    return [
        make_message(
            "msg_safety_001",
            MessageRole.USER,
            "我有点撑不住了，刚才真的想伤害自己。",
            1,
            SessionId("session_fixture_safety"),
        )
    ]
