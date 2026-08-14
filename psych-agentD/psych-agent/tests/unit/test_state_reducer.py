"""Unit tests for deterministic SessionState reduction."""

from __future__ import annotations

from schemas.common import MessageId, SessionId
from schemas.risk import RiskLevel, RiskResult, RiskRoute
from schemas.state import SessionState, StateDelta, StateOperation, TopicUpdate
from services.state_reducer import StateReducer


def test_state_reducer_does_not_mutate_previous_state() -> None:
    """Applying a delta should return a new version without changing input state."""

    previous = SessionState(session_id=SessionId("session_1"))
    delta = StateDelta(
        explicit_user_request="talk about work stress",
        topic_updates=[
            TopicUpdate(
                operation=StateOperation.ADD,
                topic="work stress",
                source_message_id=MessageId("msg_1"),
            )
        ],
    )
    risk = RiskResult(
        risk_level=RiskLevel.LOW,
        route=RiskRoute.NORMAL,
        reason_codes=["test"],
        confidence=0.9,
    )

    current = StateReducer().apply(previous, delta, feedback=None, risk=risk)

    assert previous.version == 0
    assert previous.active_topics == []
    assert current.version == 1
    assert [item.value for item in current.active_topics] == ["work stress"]
    assert current.session_goal is None

