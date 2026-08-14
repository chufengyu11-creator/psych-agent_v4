"""Reusable SessionState fixtures."""

from schemas import common, risk, state

DEFAULT_SESSION_ID = common.SessionId("session_fixture_work_stress")


def empty_state(session_id: common.SessionId | None = None) -> state.SessionState:
    """Return an empty version-zero SessionState."""

    return state.SessionState(session_id=session_id or DEFAULT_SESSION_ID)


def work_stress_state() -> state.SessionState:
    """Return a state fixture after the work-stress topic has emerged."""

    return state.SessionState(
        session_id=common.SessionId("session_fixture_work_stress"),
        version=2,
        phase=state.ConversationPhase.INTERVENTION,
        session_goal="帮助用户选择一个低压力的沟通步骤",
        active_topics=[
            state.StateItem(
                value="和直属领导沟通紧张",
                source=common.SourceReference(message_id=common.MessageId("msg_001")),
            )
        ],
        reported_emotions=[
            state.StateItem(
                value="焦虑",
                source=common.SourceReference(message_id=common.MessageId("msg_001")),
            )
        ],
        user_preferences=[
            state.StateItem(
                value="每次只给一个小步骤",
                source=common.SourceReference(message_id=common.MessageId("msg_005")),
            )
        ],
        risk_state=state.RiskState(level=risk.RiskLevel.LOW),
    )