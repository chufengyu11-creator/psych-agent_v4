"""Tests for deterministic foreground state extraction."""

from agents.state_tracker import FastStateTracker
from schemas.common import MessageId, SessionId, SourceReference
from schemas.messages import Message, MessageRole
from schemas.risk import RiskLevel, RiskResult, RiskRoute
from schemas.state import SessionState, StateItem, StateTrackerInput
from services.state_reducer import StateReducer


def _payload(content: str, previous_state: SessionState | None = None) -> StateTrackerInput:
    message = Message(
        id=MessageId("msg_fast_1"),
        session_id=SessionId("session_fast"),
        role=MessageRole.USER,
        content=content,
        sequence_number=1,
    )
    return StateTrackerInput(
        current_message=message,
        previous_state=previous_state or SessionState(session_id=message.session_id),
    )


def _low_risk() -> RiskResult:
    return RiskResult(
        risk_level=RiskLevel.LOW,
        route=RiskRoute.NORMAL,
        reason_codes=["test"],
        confidence=1.0,
    )


async def test_fast_state_tracker_extracts_explicit_current_and_session_goal() -> None:
    tracker = FastStateTracker()
    content = "\u6211\u7684\u76ee\u6807\u662f\u6539\u5584\u5bb6\u5ead\u6c9f\u901a\uff0c\u6211\u73b0\u5728\u60f3\u5148\u7406\u6e05\u4eca\u665a\u600e\u4e48\u5f00\u53e3\u3002"

    delta = await tracker.extract_delta(_payload(content))

    assert delta.explicit_user_request == content
    assert delta.current_turn_goal == content
    assert [update.goal for update in delta.goal_updates] == [content]


async def test_correction_replaces_old_emotion_without_reactivating_it() -> None:
    previous = SessionState(
        session_id=SessionId("session_fast"),
        reported_emotions=[
            StateItem(
                value="\u7126\u8651",
                source=SourceReference(message_id=MessageId("msg_old")),
            )
        ],
    )
    tracker = FastStateTracker()

    delta = await tracker.extract_delta(
        _payload(
            "\u6211\u73b0\u5728\u60f3\u5148\u804a\u5bb6\u5ead\uff0c"
            "\u4e0d\u662f\u7126\u8651\uff0c\u800c\u662f\u751f\u6c14\u3002",
            previous,
        )
    )
    current = StateReducer().apply(previous, delta, feedback=None, risk=_low_risk())

    assert [emotion.label for emotion in delta.reported_emotions] == ["\u751f\u6c14"]
    assert [(item.value, item.active) for item in current.reported_emotions] == [
        ("\u7126\u8651", False),
        ("\u751f\u6c14", True),
    ]
    assert previous.reported_emotions[0].active is True


async def test_ambiguous_text_does_not_infer_a_goal() -> None:
    delta = await FastStateTracker().extract_delta(
        _payload("\u4eca\u5929\u4e8b\u60c5\u5f88\u591a\u3002")
    )

    assert delta.current_turn_goal is None
    assert delta.goal_updates == []
