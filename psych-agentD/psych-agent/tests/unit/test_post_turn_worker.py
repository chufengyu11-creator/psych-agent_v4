"""Unit tests for post-turn summary dispatch."""

from schemas.common import MessageId, SessionId, UserId
from schemas.events import RollingSummaryRequestedEvent
from schemas.summary import RollingSummary
from workers.post_turn_worker import PostTurnWorker


class RecordingSummaryWorker:
    """Summary worker double recording dispatched events."""

    def __init__(self, result: RollingSummary | None) -> None:
        """Store the result to return and an empty event log."""

        self.result = result
        self.events: list[RollingSummaryRequestedEvent] = []

    async def handle_summary_requested(
        self,
        event: RollingSummaryRequestedEvent,
    ) -> RollingSummary | None:
        """Record the event and return the configured result."""

        self.events.append(event)
        return self.result


def _summary(session_id: SessionId) -> RollingSummary:
    """Build a compact rolling summary result."""

    return RollingSummary(
        session_id=session_id,
        summary_version=3,
        covered_from=MessageId("msg_1"),
        covered_to=MessageId("msg_2"),
    )


async def test_post_turn_worker_can_disable_summary_dispatch() -> None:
    """A disabled summary stage should not invoke its dependency."""

    session_id = SessionId("post-turn-disabled")
    summary_worker = RecordingSummaryWorker(_summary(session_id))

    result = await PostTurnWorker(
        summary_worker,
        enable_summary=False,
    ).handle_post_turn(user_id=UserId("user-1"), session_id=session_id)

    assert result.summary_updated is False
    assert result.summary_version is None
    assert summary_worker.events == []


async def test_post_turn_worker_dispatches_post_turn_summary_event() -> None:
    """An enabled stage should emit a post-turn event and report its version."""

    session_id = SessionId("post-turn-enabled")
    summary_worker = RecordingSummaryWorker(_summary(session_id))

    result = await PostTurnWorker(summary_worker).handle_post_turn(
        user_id=UserId("user-2"),
        session_id=session_id,
    )

    assert result.summary_updated is True
    assert result.summary_version == 3
    assert len(summary_worker.events) == 1
    event = summary_worker.events[0]
    assert event.session_id == session_id
    assert event.trigger == "post_turn"
    assert event.after_message_id is None


async def test_post_turn_worker_reports_no_summary_result() -> None:
    """A summary dependency returning None should be reported without error."""

    session_id = SessionId("post-turn-none")
    summary_worker = RecordingSummaryWorker(None)

    result = await PostTurnWorker(summary_worker).handle_post_turn(
        user_id=UserId("user-3"),
        session_id=session_id,
    )

    assert result.summary_updated is False
    assert result.summary_version is None
    assert len(summary_worker.events) == 1
