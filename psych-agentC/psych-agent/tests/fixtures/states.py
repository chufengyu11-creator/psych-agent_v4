"""Reusable SessionState fixtures."""

from schemas.common import MessageId, SessionId, SourceReference
from schemas.risk import RiskLevel
from schemas.state import ConversationPhase, RiskState, SessionState, StateItem


def empty_state(session_id: SessionId = SessionId("session_fixture_work_stress")) -> SessionState:
    """Return an empty version-zero SessionState."""

    return SessionState(session_id=session_id)


def work_stress_state() -> SessionState:
    """Return a state fixture after the work-stress topic has emerged."""

    return SessionState(
        session_id=SessionId("session_fixture_work_stress"),
        version=2,
        phase=ConversationPhase.INTERVENTION,
        session_goal="帮助用户选择一个低压力的沟通步骤",
        active_topics=[
            StateItem(
                value="和直属领导沟通紧张",
                source=SourceReference(message_id=MessageId("msg_001")),
            )
        ],
        reported_emotions=[
            StateItem(
                value="焦虑",
                source=SourceReference(message_id=MessageId("msg_001")),
            )
        ],
        user_preferences=[
            StateItem(
                value="每次只给一个小步骤",
                source=SourceReference(message_id=MessageId("msg_005")),
            )
        ],
        risk_state=RiskState(level=RiskLevel.LOW),
    )
