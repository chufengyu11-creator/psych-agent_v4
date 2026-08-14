"""Unit tests for the session-close memory worker."""

from collections.abc import Mapping

from agents.memory_curator import MemoryCurator
from agents.session_finalizer import FakeSessionFinalizer, SessionFinalizer
from llm.structured_output import ModelT
from schemas.common import SessionId, UserId
from schemas.events import SessionCloseRequestedEvent
from schemas.memory import (
    MemoryCandidate,
    MemoryOperation,
    MemoryPolicyDecision,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
    MemoryWriteResult,
)
from schemas.state import SessionState
from schemas.summary import RollingSummary, SessionFinalizerInput, SessionFinalizerResult
from services.memory_policy import MEMORY_DISABLED, MemoryPolicy
from storage.repositories.intervention_repository import InMemoryInterventionRepository
from storage.repositories.memory_repository import InMemoryMemoryRepository
from storage.repositories.message_repository import InMemoryMessageRepository
from storage.repositories.state_repository import InMemoryStateRepository
from storage.repositories.summary_repository import InMemorySummaryRepository
from workers.session_close_worker import SessionCloseMemoryWorker


class RecordingSessionRepository:
    """Minimal session boundary recording ownership and close calls."""

    def __init__(self, session_id: SessionId, user_id: UserId) -> None:
        """Create a repository for one owned session."""

        self.session_id = session_id
        self.user_id = user_id
        self.assert_calls = 0
        self.close_calls = 0

    async def ensure_session(self, session_id: SessionId, user_id: UserId) -> None:
        """Accept the configured owned session."""

        assert (session_id, user_id) == (self.session_id, self.user_id)

    async def exists(self, session_id: SessionId) -> bool:
        """Return whether the configured session was requested."""

        return session_id == self.session_id

    async def assert_owned_by(self, session_id: SessionId, user_id: UserId) -> None:
        """Record and verify ownership."""

        self.assert_calls += 1
        assert (session_id, user_id) == (self.session_id, self.user_id)

    async def close(self, session_id: SessionId, user_id: UserId) -> None:
        """Record and verify the close request."""

        self.close_calls += 1
        assert (session_id, user_id) == (self.session_id, self.user_id)


class RecordingMemoryPolicy(MemoryPolicy):
    """Memory policy that records candidate evaluations."""

    def __init__(self) -> None:
        """Create a policy with an empty call record."""

        super().__init__()
        self.call_count = 0

    def evaluate_candidate(self, payload):  # type: ignore[no-untyped-def]
        """Record and delegate one policy evaluation."""

        self.call_count += 1
        return super().evaluate_candidate(payload)


class RecordingMemoryRepository(InMemoryMemoryRepository):
    """In-memory repository that exposes whether create was invoked."""

    def __init__(self) -> None:
        super().__init__()
        self.create_calls = 0

    async def create(
        self,
        user_id: UserId,
        candidate: MemoryCandidate,
        decision: MemoryPolicyDecision,
    ) -> MemoryWriteResult:
        self.create_calls += 1
        return await super().create(user_id, candidate, decision)


class EmptyFinalizer:
    """Finalizer returning no candidate memories."""

    async def finalize(
        self,
        payload: SessionFinalizerInput,
    ) -> SessionFinalizerResult:
        """Return a valid empty finalization result."""

        return SessionFinalizerResult(
            session_id=payload.session_id,
            session_summary="Session closed without memory candidates.",
        )


class CapturingFinalizer:
    """Finalizer recording the exact typed input assembled by the worker."""

    def __init__(self, result: SessionFinalizerResult) -> None:
        self.result = result
        self.payloads: list[SessionFinalizerInput] = []

    async def finalize(
        self,
        payload: SessionFinalizerInput,
    ) -> SessionFinalizerResult:
        self.payloads.append(payload)
        return self.result


class CandidateFinalizer:
    """Finalizer returning one configured candidate without other behavior."""

    def __init__(self, candidate: MemoryCandidate) -> None:
        self.candidate = candidate
        self.call_count = 0

    async def finalize(
        self,
        payload: SessionFinalizerInput,
    ) -> SessionFinalizerResult:
        self.call_count += 1
        return SessionFinalizerResult(
            session_id=payload.session_id,
            session_summary="Candidate close result",
            candidate_memories=[self.candidate],
            source_message_ids=self.candidate.source_message_ids,
        )


class WorkerGroundedFinalizerClient:
    """Structured client returning one policy-ready explicit preference."""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Return a grounded candidate for the worker's first message."""

        assert "INPUT_JSON" in prompt
        assert model_name is None
        assert metadata == {"agent": "session_finalizer"}
        return output_model.model_validate(
            {
                "session_id": "wrong-model-session",
                "session_summary": "model-backed close worker summary",
                "candidate_memories": [
                    {
                        "candidate_type": "interaction_preference",
                        "content": "用户希望每次只收到一个小步骤。",
                        "source_message_ids": ["msg_1"],
                        "source_type": "explicit_user_statement",
                        "confidence": 0.94,
                        "requires_user_confirmation": False,
                        "sensitivity": "low",
                        "recommended_operation": "CREATE",
                    }
                ],
                "source_message_ids": ["msg_1"],
            }
        )


class WorkerFinalizerStructuredClient:
    """Structured client returning one policy-ready private draft."""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Return a grounded private finalizer draft."""

        assert "INPUT_JSON" in prompt
        assert model_name is None
        assert metadata == {"agent": "session_finalizer"}
        return output_model.model_validate(
            {
                "session_summary": {
                    "value": "model-backed close worker summary",
                    "evidence": [{"message_id": "msg_1", "quote": "小步骤"}],
                },
                "provisional_memories": [
                    {
                        "candidate_type": "interaction_preference",
                        "content": "The user prefers one small step at a time.",
                        "source_type": "explicit_user_statement",
                        "confidence": 0.94,
                        "requires_user_confirmation": False,
                        "sensitivity": "low",
                        "recommended_operation": "CREATE",
                        "evidence": [{"message_id": "msg_1", "quote": "小步骤"}],
                    }
                ],
            }
        )


async def test_worker_writes_allowed_candidate_and_preserves_sources() -> None:
    """An explicit preference should pass policy, persist, and retain its sources."""

    session_id = SessionId("close-worker-unit")
    user_id = UserId("close-worker-user")
    messages = InMemoryMessageRepository()
    source = await messages.create_user_message(
        session_id,
        "我希望每次一个小步骤，不要一次太多建议。",
    )
    memory_repository = InMemoryMemoryRepository()
    session_repository = RecordingSessionRepository(session_id, user_id)
    policy = RecordingMemoryPolicy()
    worker = SessionCloseMemoryWorker(
        message_repository=messages,
        state_repository=InMemoryStateRepository(),
        summary_repository=InMemorySummaryRepository(),
        intervention_repository=InMemoryInterventionRepository(),
        memory_repository=memory_repository,
        session_repository=session_repository,
        finalizer=FakeSessionFinalizer(),
        memory_policy=policy,
        user_memory_enabled=True,
    )

    result = await worker.handle_session_close_requested(
        SessionCloseRequestedEvent(session_id=session_id, user_id=user_id)
    )

    assert result.candidate_memory_count == 1
    assert result.memory_write_count == 1
    assert policy.call_count == 1
    assert len(result.memory_write_results) == 1
    assert result.memory_write_results[0].applied is True
    assert result.finalizer_result.candidate_memories[0].source_message_ids == [source.id]
    stored = await memory_repository.list_active(user_id)
    assert stored[0].source[0].message_id == source.id
    assert session_repository.assert_calls == 1
    assert session_repository.close_calls == 1


async def test_worker_closes_session_when_finalizer_has_no_candidates() -> None:
    """An empty candidate list should still complete the session close."""

    session_id = SessionId("close-worker-empty")
    user_id = UserId("close-worker-empty-user")
    session_repository = RecordingSessionRepository(session_id, user_id)
    policy = RecordingMemoryPolicy()
    worker = SessionCloseMemoryWorker(
        message_repository=InMemoryMessageRepository(),
        state_repository=InMemoryStateRepository(),
        summary_repository=InMemorySummaryRepository(),
        intervention_repository=InMemoryInterventionRepository(),
        memory_repository=InMemoryMemoryRepository(),
        session_repository=session_repository,
        finalizer=EmptyFinalizer(),
        memory_policy=policy,
        user_memory_enabled=True,
    )

    result = await worker.handle_session_close_requested(
        SessionCloseRequestedEvent(session_id=session_id, user_id=user_id)
    )

    assert result.candidate_memory_count == 0
    assert result.memory_write_count == 0
    assert result.memory_write_results == []
    assert policy.call_count == 0
    assert session_repository.close_calls == 1


async def test_worker_assembles_complete_finalizer_input_and_returns_result() -> None:
    """The worker should call Finalizer once with all current repository material."""

    session_id = SessionId("close-worker-capturing")
    user_id = UserId("close-worker-capturing-user")
    messages = InMemoryMessageRepository()
    user_message = await messages.create_user_message(session_id, "A grounded concern")
    assistant_message = await messages.create_assistant_message(session_id, "A grounded response")
    states = InMemoryStateRepository()
    final_state = SessionState(
        session_id=session_id,
        version=1,
        session_goal="Clarify one next step",
    )
    await states.save_version(final_state)
    interventions = InMemoryInterventionRepository()
    intervention = await interventions.create_pending(
        session_id,
        assistant_message.id,
        "clarification",
        "clarify the next step",
        [],
    )
    summaries = InMemorySummaryRepository()
    rolling_summary = RollingSummary(
        session_id=session_id,
        summary_version=1,
        covered_from=user_message.id,
        covered_to=assistant_message.id,
        current_problem="A grounded concern",
        source_message_ids=[user_message.id],
    )
    await summaries.save_version(rolling_summary)
    finalizer_result = SessionFinalizerResult(
        session_id=session_id,
        session_summary="Captured close result",
        candidate_memories=[],
        source_message_ids=[user_message.id],
    )
    finalizer = CapturingFinalizer(finalizer_result)
    sessions = RecordingSessionRepository(session_id, user_id)
    worker = SessionCloseMemoryWorker(
        message_repository=messages,
        state_repository=states,
        summary_repository=summaries,
        intervention_repository=interventions,
        memory_repository=InMemoryMemoryRepository(),
        session_repository=sessions,
        finalizer=finalizer,
        memory_policy=RecordingMemoryPolicy(),
        user_memory_enabled=True,
    )

    result = await worker.handle_session_close_requested(
        SessionCloseRequestedEvent(session_id=session_id, user_id=user_id)
    )

    assert len(finalizer.payloads) == 1
    captured = finalizer.payloads[0]
    assert captured.session_id == session_id
    assert captured.messages == [user_message, assistant_message]
    assert captured.final_state == final_state
    assert captured.interventions == [intervention]
    assert captured.rolling_summary == rolling_summary
    assert result.finalizer_result is finalizer_result
    assert result.candidate_memory_count == 0
    assert result.memory_write_count == 0
    assert sessions.close_calls == 1


async def test_worker_policy_gates_model_backed_finalizer_candidate() -> None:
    """Model candidates should still pass policy and repository boundaries."""

    session_id = SessionId("close-worker-model-backed")
    user_id = UserId("close-worker-model-backed-user")
    messages = InMemoryMessageRepository()
    source = await messages.create_user_message(session_id, "每次只给我一个小步骤。")
    memories = InMemoryMemoryRepository()
    sessions = RecordingSessionRepository(session_id, user_id)
    policy = RecordingMemoryPolicy()
    worker = SessionCloseMemoryWorker(
        message_repository=messages,
        state_repository=InMemoryStateRepository(),
        summary_repository=InMemorySummaryRepository(),
        intervention_repository=InMemoryInterventionRepository(),
        memory_repository=memories,
        session_repository=sessions,
        finalizer=SessionFinalizer(WorkerGroundedFinalizerClient()),
        memory_policy=policy,
        user_memory_enabled=True,
    )

    result = await worker.handle_session_close_requested(
        SessionCloseRequestedEvent(session_id=session_id, user_id=user_id)
    )

    assert result.finalizer_result.session_id == session_id
    assert result.memory_write_count == 1
    assert policy.call_count == 1
    stored = await memories.list_active(user_id)
    assert stored[0].source[0].message_id == source.id
    assert sessions.close_calls == 1


async def test_worker_policy_gates_candidate_generated_by_curator() -> None:
    """Curated explicit preferences still pass policy and repository boundaries."""

    session_id = SessionId("close-worker-curator")
    user_id = UserId("close-worker-curator-user")
    messages = InMemoryMessageRepository()
    source = await messages.create_user_message(
        session_id,
        "我希望每次一个小步骤，不要一次太多。",
    )
    memories = InMemoryMemoryRepository()
    sessions = RecordingSessionRepository(session_id, user_id)
    policy = RecordingMemoryPolicy()
    worker = SessionCloseMemoryWorker(
        message_repository=messages,
        state_repository=InMemoryStateRepository(),
        summary_repository=InMemorySummaryRepository(),
        intervention_repository=InMemoryInterventionRepository(),
        memory_repository=memories,
        session_repository=sessions,
        finalizer=EmptyFinalizer(),
        memory_policy=policy,
        user_memory_enabled=True,
        memory_curator=MemoryCurator(),
    )

    result = await worker.handle_session_close_requested(
        SessionCloseRequestedEvent(session_id=session_id, user_id=user_id)
    )

    assert result.candidate_memory_count == 1
    assert result.memory_write_count == 1
    assert policy.call_count == 1
    assert result.finalizer_result.candidate_memories[0].source_message_ids == [source.id]
    stored = await memories.list_active(user_id)
    assert stored[0].source[0].message_id == source.id
    assert sessions.close_calls == 1


async def test_worker_does_not_persist_policy_rejected_candidate() -> None:
    """Delegated MemoryWorker must not call create for a rejected candidate."""

    session_id = SessionId("close-worker-rejected")
    user_id = UserId("close-worker-rejected-user")
    messages = InMemoryMessageRepository()
    source = await messages.create_user_message(session_id, "A source message")
    candidate = MemoryCandidate(
        candidate_type=MemoryType.INTERACTION_PREFERENCE,
        content="The user's hidden motive is avoidance.",
        source_message_ids=[source.id],
        source_type=MemorySourceType.MODEL_INFERENCE,
        confidence=0.9,
        requires_user_confirmation=False,
        sensitivity=MemorySensitivity.LOW,
        recommended_operation=MemoryOperation.CREATE,
    )
    memories = RecordingMemoryRepository()
    sessions = RecordingSessionRepository(session_id, user_id)
    result = await SessionCloseMemoryWorker(
        message_repository=messages,
        state_repository=InMemoryStateRepository(),
        summary_repository=InMemorySummaryRepository(),
        intervention_repository=InMemoryInterventionRepository(),
        memory_repository=memories,
        session_repository=sessions,
        finalizer=CandidateFinalizer(candidate),
        memory_policy=MemoryPolicy(),
        user_memory_enabled=True,
    ).handle_session_close_requested(
        SessionCloseRequestedEvent(session_id=session_id, user_id=user_id)
    )

    assert result.candidate_memory_count == 1
    assert result.memory_write_count == 0
    assert len(result.memory_write_results) == 1
    assert result.memory_write_results[0].applied is False
    assert await memories.list_active(user_id) == []
    assert memories.create_calls == 0
    assert sessions.close_calls == 1


async def test_worker_audits_memory_disabled_without_skipping_close() -> None:
    """Disabled memory still finalizes and returns one auditable rejection."""

    session_id = SessionId("close-worker-memory-disabled")
    user_id = UserId("close-worker-memory-disabled-user")
    messages = InMemoryMessageRepository()
    source = await messages.create_user_message(
        session_id, "我希望每次一个小步骤，不要一次太多建议。"
    )
    candidate = MemoryCandidate(
        candidate_type=MemoryType.INTERACTION_PREFERENCE,
        content="用户希望每次一个小步骤，不要一次太多建议。",
        source_message_ids=[source.id],
        source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
        confidence=0.9,
        requires_user_confirmation=False,
        sensitivity=MemorySensitivity.LOW,
        recommended_operation=MemoryOperation.CREATE,
    )
    finalizer = CandidateFinalizer(candidate)
    memories = RecordingMemoryRepository()
    sessions = RecordingSessionRepository(session_id, user_id)
    result = await SessionCloseMemoryWorker(
        message_repository=messages,
        state_repository=InMemoryStateRepository(),
        summary_repository=InMemorySummaryRepository(),
        intervention_repository=InMemoryInterventionRepository(),
        memory_repository=memories,
        session_repository=sessions,
        finalizer=finalizer,
        memory_policy=MemoryPolicy(),
        user_memory_enabled=False,
    ).handle_session_close_requested(
        SessionCloseRequestedEvent(session_id=session_id, user_id=user_id)
    )

    assert result.candidate_memory_count == 1
    assert result.memory_write_count == 0
    assert len(result.memory_write_results) == 1
    assert result.memory_write_results[0].applied is False
    assert result.memory_write_results[0].reason_codes == [MEMORY_DISABLED]
    assert await memories.list_active(user_id) == []
    assert memories.create_calls == 0
    assert finalizer.call_count == 1
    assert sessions.close_calls == 1
