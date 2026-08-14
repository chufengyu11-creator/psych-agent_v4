"""Unit tests for the rolling-summary worker."""

from collections.abc import Mapping

import pytest

from agents.rolling_summarizer import FakeRollingSummarizer, RollingSummarizer
from llm.structured_output import ModelT
from schemas.common import MessageId, SessionId, SourceReference, UserId
from schemas.events import RollingSummaryRequestedEvent
from schemas.state import SessionState, StateItem
from schemas.summary import RollingSummary
from storage.repositories.intervention_repository import InMemoryInterventionRepository
from storage.repositories.message_repository import InMemoryMessageRepository
from storage.repositories.state_repository import InMemoryStateRepository
from storage.repositories.summary_repository import InMemorySummaryRepository
from workers.summary_worker import SummaryWorker

USER_ID = UserId("summary-worker-user")


class CountingSummaryRepository(InMemorySummaryRepository):
    """In-memory repository that records persistence calls."""

    def __init__(self) -> None:
        """Create an empty repository and call counter."""

        super().__init__()
        self.save_calls = 0

    async def save_version(
        self,
        user_id: UserId,
        summary: RollingSummary,
    ) -> None:
        """Count and persist a summary version."""

        self.save_calls += 1
        await super().save_version(user_id, summary)


class WorkerStructuredClient:
    """Structured client returning a grounded model-backed worker summary."""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Return a model result grounded in the worker's first in-memory message."""

        assert "INPUT_JSON" in prompt
        assert model_name is None
        assert metadata == {"agent": "rolling_summarizer"}
        return output_model.model_validate(
            {
                "session_id": "model-controlled-session",
                "summary_version": 99,
                "covered_from": "msg_1",
                "covered_to": "msg_1",
                "current_problem": "model-backed worker summary",
                "source_message_ids": ["msg_1"],
            }
        )


def make_worker(
    message_repository: InMemoryMessageRepository,
    summary_repository: CountingSummaryRepository,
) -> SummaryWorker:
    """Build a worker from deterministic in-memory dependencies."""

    return SummaryWorker(
        message_repository=message_repository,
        state_repository=InMemoryStateRepository(),
        summary_repository=summary_repository,
        intervention_repository=InMemoryInterventionRepository(),
        summarizer=FakeRollingSummarizer(),
    )


@pytest.mark.asyncio
async def test_summary_worker_generates_and_saves_grounded_summary() -> None:
    """New messages should produce one persisted, source-grounded summary."""

    session_id = SessionId("summary-worker-unit")
    messages = InMemoryMessageRepository()
    summaries = CountingSummaryRepository()
    first = await messages.create_user_message(
        USER_ID, session_id, "I feel stuck at work."
    )
    last = await messages.create_assistant_message(
        USER_ID, session_id, "What feels most difficult?"
    )

    result = await make_worker(messages, summaries).handle_summary_requested(
        RollingSummaryRequestedEvent(
            user_id=USER_ID,
            session_id=session_id,
            trigger="unit-test",
            after_message_id=last.id,
        )
    )

    assert result is not None
    assert result.summary_version == 1
    assert first.id in result.source_message_ids
    assert summaries.save_calls == 1
    assert await summaries.get_current(USER_ID, session_id) == result


@pytest.mark.asyncio
async def test_summary_worker_increments_version_and_does_not_resave_without_new_messages() -> None:
    """A later message increments once, while an identical request is idempotent."""

    session_id = SessionId("summary-worker-increment")
    messages = InMemoryMessageRepository()
    summaries = CountingSummaryRepository()
    worker = make_worker(messages, summaries)
    first = await messages.create_user_message(USER_ID, session_id, "First update")
    initial = await worker.handle_summary_requested(
        RollingSummaryRequestedEvent(
            user_id=USER_ID,
            session_id=session_id,
            trigger="first",
            after_message_id=first.id,
        )
    )
    second = await messages.create_user_message(USER_ID, session_id, "Second update")

    updated = await worker.handle_summary_requested(
        RollingSummaryRequestedEvent(
            user_id=USER_ID,
            session_id=session_id,
            trigger="second",
            after_message_id=second.id,
        )
    )
    repeated = await worker.handle_summary_requested(
        RollingSummaryRequestedEvent(
            user_id=USER_ID,
            session_id=session_id,
            trigger="repeat",
            after_message_id=second.id,
        )
    )

    assert initial is not None
    assert updated is not None
    assert updated.summary_version == 2
    assert updated.source_message_ids == [MessageId(first.id), MessageId(second.id)]
    assert repeated == updated
    assert summaries.save_calls == 2


@pytest.mark.asyncio
async def test_summary_worker_saves_model_backed_summary() -> None:
    """A model-backed summarizer can be injected without changing worker persistence."""

    session_id = SessionId("summary-worker-model-backed")
    messages = InMemoryMessageRepository()
    summaries = CountingSummaryRepository()
    first = await messages.create_user_message(
        USER_ID, session_id, "Please summarize this."
    )
    worker = SummaryWorker(
        message_repository=messages,
        state_repository=InMemoryStateRepository(),
        summary_repository=summaries,
        intervention_repository=InMemoryInterventionRepository(),
        summarizer=RollingSummarizer(WorkerStructuredClient()),
    )

    result = await worker.handle_summary_requested(
        RollingSummaryRequestedEvent(
            user_id=USER_ID,
            session_id=session_id,
            trigger="model-backed-test",
            after_message_id=first.id,
        )
    )

    assert result is not None
    assert result.current_problem == "model-backed worker summary"
    assert result.session_id == session_id
    assert result.summary_version == 1
    assert result.covered_from == first.id
    assert result.covered_to == first.id
    assert result.source_message_ids == [first.id]
    assert summaries.save_calls == 1
    assert await summaries.get_current(USER_ID, session_id) == result


@pytest.mark.asyncio
async def test_automatic_summary_retains_the_latest_turn() -> None:
    """Automatic mode should summarize the older turn and retain the latest pair."""

    session_id = SessionId("summary-worker-retain-turn")
    messages = InMemoryMessageRepository()
    summaries = CountingSummaryRepository()
    interventions = InMemoryInterventionRepository()
    first_user = await messages.create_user_message(
        USER_ID, session_id, "First user statement"
    )
    first_assistant = await messages.create_assistant_message(
        USER_ID, session_id, "First response"
    )
    await interventions.create_pending(
        USER_ID,
        session_id,
        first_assistant.id,
        "reflective_listening",
        "understand the situation",
        [],
    )
    retained_user = await messages.create_user_message(
        USER_ID, session_id, "Retained user statement"
    )
    retained_assistant = await messages.create_assistant_message(
        USER_ID, session_id, "Retained response"
    )
    worker = SummaryWorker(
        message_repository=messages,
        state_repository=InMemoryStateRepository(),
        summary_repository=summaries,
        intervention_repository=interventions,
        summarizer=FakeRollingSummarizer(),
    )

    result = await worker.handle_summary_requested(
        RollingSummaryRequestedEvent(
            user_id=USER_ID,
            session_id=session_id,
            trigger="post_turn",
        )
    )

    assert result is not None
    assert result.covered_from == first_user.id
    assert result.covered_to == first_assistant.id
    assert first_user.id in result.source_message_ids
    assert first_assistant.id in result.source_message_ids
    assert retained_user.id not in result.source_message_ids
    assert retained_assistant.id not in result.source_message_ids
    assert retained_user.content not in result.important_user_statements


@pytest.mark.asyncio
async def test_automatic_summary_skips_first_turn_when_nothing_is_old_enough() -> None:
    """One retained turn should not create or persist an empty summary."""

    session_id = SessionId("summary-worker-first-turn")
    messages = InMemoryMessageRepository()
    summaries = CountingSummaryRepository()
    await messages.create_user_message(USER_ID, session_id, "First user message")
    await messages.create_assistant_message(
        USER_ID, session_id, "First assistant message"
    )

    result = await make_worker(messages, summaries).handle_summary_requested(
        RollingSummaryRequestedEvent(
            user_id=USER_ID,
            session_id=session_id,
            trigger="post_turn",
        )
    )

    assert result is None
    assert summaries.save_calls == 0
    assert await summaries.get_current(USER_ID, session_id) is None


@pytest.mark.asyncio
async def test_automatic_summary_advances_by_one_old_turn() -> None:
    """Each later automatic run should cover one more turn and retain the newest."""

    session_id = SessionId("summary-worker-auto-version")
    messages = InMemoryMessageRepository()
    summaries = CountingSummaryRepository()
    worker = make_worker(messages, summaries)
    first_user = await messages.create_user_message(USER_ID, session_id, "u1")
    first_assistant = await messages.create_assistant_message(
        USER_ID, session_id, "a1"
    )
    await messages.create_user_message(USER_ID, session_id, "u2")
    second_assistant = await messages.create_assistant_message(
        USER_ID, session_id, "a2"
    )

    first_summary = await worker.handle_summary_requested(
        RollingSummaryRequestedEvent(
            user_id=USER_ID,
            session_id=session_id,
            trigger="post_turn",
        )
    )
    await messages.create_user_message(USER_ID, session_id, "u3")
    third_assistant = await messages.create_assistant_message(
        USER_ID, session_id, "a3"
    )
    second_summary = await worker.handle_summary_requested(
        RollingSummaryRequestedEvent(
            user_id=USER_ID,
            session_id=session_id,
            trigger="post_turn",
        )
    )

    assert first_summary is not None
    assert first_summary.summary_version == 1
    assert first_summary.covered_from == first_user.id
    assert first_summary.covered_to == first_assistant.id
    assert second_summary is not None
    assert second_summary.summary_version == 2
    assert second_summary.covered_from == first_user.id
    assert second_summary.covered_to == second_assistant.id
    assert third_assistant.id not in second_summary.source_message_ids
    assert summaries.save_calls == 2


@pytest.mark.asyncio
async def test_explicit_boundary_does_not_retain_recent_messages() -> None:
    """A caller-supplied cutoff should remain an exact inclusive boundary."""

    session_id = SessionId("summary-worker-explicit-boundary")
    messages = InMemoryMessageRepository()
    summaries = CountingSummaryRepository()
    first_user = await messages.create_user_message(USER_ID, session_id, "u1")
    await messages.create_assistant_message(USER_ID, session_id, "a1")
    await messages.create_user_message(USER_ID, session_id, "u2")
    boundary = await messages.create_assistant_message(USER_ID, session_id, "a2")

    result = await make_worker(messages, summaries).handle_summary_requested(
        RollingSummaryRequestedEvent(
            user_id=USER_ID,
            session_id=session_id,
            trigger="manual",
            after_message_id=boundary.id,
        )
    )

    assert result is not None
    assert result.covered_from == first_user.id
    assert result.covered_to == boundary.id


@pytest.mark.asyncio
async def test_session_close_without_boundary_covers_the_remaining_messages() -> None:
    """Retention is a post-turn rule, so session close can summarize the final turn."""

    session_id = SessionId("summary-worker-session-close")
    messages = InMemoryMessageRepository()
    summaries = CountingSummaryRepository()
    first_user = await messages.create_user_message(USER_ID, session_id, "u1")
    final_assistant = await messages.create_assistant_message(
        USER_ID, session_id, "a1"
    )

    result = await make_worker(messages, summaries).handle_summary_requested(
        RollingSummaryRequestedEvent(
            user_id=USER_ID,
            session_id=session_id,
            trigger="session_close",
        )
    )

    assert result is not None
    assert result.covered_from == first_user.id
    assert result.covered_to == final_assistant.id


@pytest.mark.asyncio
async def test_retained_state_source_does_not_leak_into_summary() -> None:
    """State derived from a retained user message must be filtered from input."""

    session_id = SessionId("summary-worker-state-filter")
    messages = InMemoryMessageRepository()
    summaries = CountingSummaryRepository()
    states = InMemoryStateRepository()
    first_user = await messages.create_user_message(
        USER_ID, session_id, "older grounded topic"
    )
    await messages.create_assistant_message(
        USER_ID, session_id, "older assistant response"
    )
    retained_user = await messages.create_user_message(
        USER_ID, session_id, "retained secret topic"
    )
    await messages.create_assistant_message(
        USER_ID, session_id, "retained assistant response"
    )
    await states.save_version(
        USER_ID,
        SessionState(
            session_id=session_id,
            session_goal="latest untraceable goal",
            active_topics=[
                StateItem(
                    value="retained secret topic",
                    source=SourceReference(message_id=retained_user.id),
                )
            ],
        )
    )
    worker = SummaryWorker(
        message_repository=messages,
        state_repository=states,
        summary_repository=summaries,
        intervention_repository=InMemoryInterventionRepository(),
        summarizer=FakeRollingSummarizer(),
    )

    result = await worker.handle_summary_requested(
        RollingSummaryRequestedEvent(
            user_id=USER_ID,
            session_id=session_id,
            trigger="post_turn",
        )
    )

    assert result is not None
    assert result.current_problem == first_user.content
    assert result.session_goal is None
    assert retained_user.id not in result.source_message_ids
    assert "retained secret topic" not in result.important_user_statements


@pytest.mark.asyncio
async def test_retained_pending_intervention_does_not_leak_into_summary() -> None:
    """A pending strategy on the retained assistant message must stay outside coverage."""

    session_id = SessionId("summary-worker-intervention-filter")
    messages = InMemoryMessageRepository()
    summaries = CountingSummaryRepository()
    interventions = InMemoryInterventionRepository()
    await messages.create_user_message(USER_ID, session_id, "older user message")
    await messages.create_assistant_message(
        USER_ID, session_id, "older assistant message"
    )
    await messages.create_user_message(USER_ID, session_id, "retained user message")
    retained_assistant = await messages.create_assistant_message(
        USER_ID, session_id, "retained assistant message"
    )
    await interventions.create_pending(
        USER_ID,
        session_id,
        retained_assistant.id,
        "retained_strategy",
        "latest objective",
        [],
    )
    worker = SummaryWorker(
        message_repository=messages,
        state_repository=InMemoryStateRepository(),
        summary_repository=summaries,
        intervention_repository=interventions,
        summarizer=FakeRollingSummarizer(),
    )

    result = await worker.handle_summary_requested(
        RollingSummaryRequestedEvent(
            user_id=USER_ID,
            session_id=session_id,
            trigger="post_turn",
        )
    )

    assert result is not None
    assert "retained_strategy" not in result.strategies_attempted
    assert result.strategy_responses == []
    assert retained_assistant.id not in result.source_message_ids
