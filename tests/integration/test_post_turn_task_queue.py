"""Integration test for inline post-turn summary dispatch."""

from agents.rolling_summarizer import FakeRollingSummarizer
from orchestrator.post_turn_pipeline import InlinePostTurnTaskQueue
from schemas.common import SessionId, UserId
from storage.repositories.intervention_repository import InMemoryInterventionRepository
from storage.repositories.message_repository import InMemoryMessageRepository
from storage.repositories.state_repository import InMemoryStateRepository
from storage.repositories.summary_repository import InMemorySummaryRepository
from workers.post_turn_worker import PostTurnWorker
from workers.summary_worker import SummaryWorker


async def test_inline_post_turn_queue_retains_latest_turn_before_summarizing() -> None:
    """The first turn stays raw and the second task summarizes only the older turn."""

    session_id = SessionId("inline-post-turn-session")
    user_id = UserId("inline-post-turn-user")
    messages = InMemoryMessageRepository()
    summaries = InMemorySummaryRepository()
    await messages.create_user_message(user_id, session_id, "First user message")
    first_assistant = await messages.create_assistant_message(
        user_id, session_id, "First assistant message"
    )
    queue = InlinePostTurnTaskQueue(
        PostTurnWorker(
            SummaryWorker(
                message_repository=messages,
                state_repository=InMemoryStateRepository(),
                summary_repository=summaries,
                intervention_repository=InMemoryInterventionRepository(),
                summarizer=FakeRollingSummarizer(),
            )
        )
    )

    await queue.enqueue("post_turn", str(user_id), str(session_id))

    assert await summaries.get_current(user_id, session_id) is None
    assert len(queue.results) == 1
    assert queue.results[0].summary_updated is False

    await messages.create_user_message(user_id, session_id, "Second user message")
    await messages.create_assistant_message(
        user_id,
        session_id,
        "Second assistant message",
    )
    await queue.enqueue("post_turn", str(user_id), str(session_id))

    current = await summaries.get_current(user_id, session_id)
    assert current is not None
    assert current.summary_version == 1
    assert current.covered_to == first_assistant.id
    assert len(queue.results) == 2
    assert queue.results[1].summary_updated is True
    assert queue.results[1].summary_version == 1
