"""Unit tests for policy-gated MemoryWorker orchestration."""

import pytest

from schemas.common import MemoryId, MessageId, SessionId, SourceReference, UserId
from schemas.events import MemoryCandidateCreatedEvent
from schemas.memory import (
    LongTermMemory,
    MemoryCandidate,
    MemoryOperation,
    MemoryPolicyDecision,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
    MemoryWriteResult,
)
from services.memory_policy import CONFLICT_REQUIRES_REVIEW, MEMORY_DISABLED, MemoryPolicy
from workers.memory_worker import MemoryWorker

SESSION_ID = SessionId("memory-worker-session")
USER_ID = UserId("memory-worker-user")


def _candidate(
    content: str = "User prefers one small step at a time.",
    *,
    source_id: str = "msg_candidate",
    candidate_type: MemoryType = MemoryType.INTERACTION_PREFERENCE,
    requires_confirmation: bool = False,
    sensitivity: MemorySensitivity = MemorySensitivity.LOW,
) -> MemoryCandidate:
    return MemoryCandidate(
        candidate_type=candidate_type,
        content=content,
        source_message_ids=[MessageId(source_id)],
        source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
        confidence=0.9,
        requires_user_confirmation=requires_confirmation,
        sensitivity=sensitivity,
        recommended_operation=MemoryOperation.CREATE,
    )


def _memory(memory_id: str, content: str) -> LongTermMemory:
    return LongTermMemory(
        id=MemoryId(memory_id),
        memory_type=MemoryType.INTERACTION_PREFERENCE,
        content=content,
        source=[SourceReference(message_id=MessageId("msg_existing"))],
    )


def _event(
    candidates: list[MemoryCandidate],
    *,
    count: int | None = None,
    source_ids: list[MessageId] | None = None,
) -> MemoryCandidateCreatedEvent:
    actual_sources = list(
        dict.fromkeys(
            source_id
            for candidate in candidates
            for source_id in candidate.source_message_ids
        )
    )
    return MemoryCandidateCreatedEvent(
        session_id=SESSION_ID,
        user_id=USER_ID,
        candidate_count=len(candidates) if count is None else count,
        source_message_ids=actual_sources if source_ids is None else source_ids,
    )


class RecordingMemoryRepository:
    """Public-protocol repository double with observable calls and batch state."""

    def __init__(
        self,
        active: list[LongTermMemory] | None = None,
        *,
        fail_create: bool = False,
    ) -> None:
        self.active = list(active or [])
        self.fail_create = fail_create
        self.list_calls = 0
        self.create_calls: list[
            tuple[UserId, MemoryCandidate, MemoryPolicyDecision]
        ] = []

    async def list_active(self, user_id: UserId) -> list[LongTermMemory]:
        assert user_id == USER_ID
        self.list_calls += 1
        return list(self.active)

    async def create(
        self,
        user_id: UserId,
        candidate: MemoryCandidate,
        decision: MemoryPolicyDecision,
    ) -> MemoryWriteResult:
        if self.fail_create:
            raise RuntimeError("persistence failed")
        self.create_calls.append((user_id, candidate, decision))
        memory_id = decision.target_memory_id
        if decision.operation == MemoryOperation.CREATE:
            memory_id = MemoryId(f"mem_created_{len(self.active) + 1}")
            self.active.append(
                LongTermMemory(
                    id=memory_id,
                    memory_type=candidate.candidate_type,
                    content=candidate.content,
                    sensitivity=candidate.sensitivity,
                    confidence=candidate.confidence,
                    source=[
                        SourceReference(message_id=source_id)
                        for source_id in candidate.source_message_ids
                    ],
                )
            )
        return MemoryWriteResult(
            memory_id=memory_id,
            operation=decision.operation or candidate.recommended_operation,
            applied=True,
            reason_codes=decision.reason_codes,
        )


async def test_empty_candidate_batch_returns_empty() -> None:
    repository = RecordingMemoryRepository()
    result = await MemoryWorker(repository, MemoryPolicy()).handle_memory_candidate_created(
        _event([]), [], user_memory_enabled=True
    )
    assert result == []
    assert repository.list_calls == 0


async def test_candidate_count_mismatch_raises() -> None:
    candidate = _candidate()
    with pytest.raises(ValueError, match="candidate_count"):
        await MemoryWorker(
            RecordingMemoryRepository(), MemoryPolicy()
        ).handle_memory_candidate_created(
            _event([candidate], count=2),
            [candidate],
            user_memory_enabled=True,
        )


@pytest.mark.parametrize(
    "source_ids",
    [[], [MessageId("msg_candidate"), MessageId("unknown")]],
)
async def test_event_source_mismatch_raises(source_ids: list[MessageId]) -> None:
    candidate = _candidate()
    with pytest.raises(ValueError, match="source_message_ids"):
        await MemoryWorker(
            RecordingMemoryRepository(), MemoryPolicy()
        ).handle_memory_candidate_created(
            _event([candidate], source_ids=source_ids),
            [candidate],
            user_memory_enabled=True,
        )


async def test_memory_disabled_returns_rejection_without_repository_create() -> None:
    candidate = _candidate()
    repository = RecordingMemoryRepository()
    result = await MemoryWorker(repository, MemoryPolicy()).handle_memory_candidate_created(
        _event([candidate]), [candidate], user_memory_enabled=False
    )
    assert result[0].applied is False
    assert MEMORY_DISABLED in result[0].reason_codes
    assert repository.create_calls == []


async def test_valid_create_passes_original_candidate_and_decision() -> None:
    candidate = _candidate()
    repository = RecordingMemoryRepository()
    result = await MemoryWorker(repository, MemoryPolicy()).handle_memory_candidate_created(
        _event([candidate]), [candidate], user_memory_enabled=True
    )
    assert repository.list_calls == 1
    assert len(repository.create_calls) == 1
    _, passed_candidate, decision = repository.create_calls[0]
    assert passed_candidate is candidate
    assert decision.operation == MemoryOperation.CREATE
    assert result[0].operation == MemoryOperation.CREATE
    assert repository.active[0].source[0].message_id == candidate.source_message_ids[0]


async def test_duplicate_candidate_reinforces_target_memory() -> None:
    candidate = _candidate()
    existing = _memory("mem_existing", candidate.content)
    repository = RecordingMemoryRepository([existing])
    result = await MemoryWorker(repository, MemoryPolicy()).handle_memory_candidate_created(
        _event([candidate]), [candidate], user_memory_enabled=True
    )
    assert result[0].operation == MemoryOperation.REINFORCE
    assert result[0].memory_id == existing.id
    assert repository.create_calls[0][2].target_memory_id == existing.id


async def test_conflict_is_recorded_by_repository() -> None:
    candidate = _candidate("User wants a complete plan all at once.")
    repository = RecordingMemoryRepository(
        [_memory("mem_existing", "User prefers one small step at a time.")]
    )
    result = await MemoryWorker(repository, MemoryPolicy()).handle_memory_candidate_created(
        _event([candidate]), [candidate], user_memory_enabled=True
    )
    assert result[0].applied is True
    assert result[0].operation == MemoryOperation.MARK_CONFLICT
    assert CONFLICT_REQUIRES_REVIEW in result[0].reason_codes
    assert repository.create_calls[0][2].target_memory_id == MemoryId("mem_existing")


async def test_confirmation_decision_is_passed_to_repository() -> None:
    candidate = _candidate(
        requires_confirmation=True,
        sensitivity=MemorySensitivity.MEDIUM,
    )
    repository = RecordingMemoryRepository()
    await MemoryWorker(repository, MemoryPolicy()).handle_memory_candidate_created(
        _event([candidate]), [candidate], user_memory_enabled=True
    )
    decision = repository.create_calls[0][2]
    assert decision.allowed is True
    assert decision.requires_user_confirmation is True


async def test_second_duplicate_in_batch_sees_first_create() -> None:
    first = _candidate(source_id="msg_first")
    second = _candidate(source_id="msg_second")
    repository = RecordingMemoryRepository()
    results = await MemoryWorker(repository, MemoryPolicy()).handle_memory_candidate_created(
        _event([first, second]), [first, second], user_memory_enabled=True
    )
    assert repository.list_calls == 2
    assert [result.operation for result in results] == [
        MemoryOperation.CREATE,
        MemoryOperation.REINFORCE,
    ]


async def test_repository_exception_propagates() -> None:
    candidate = _candidate()
    with pytest.raises(RuntimeError, match="persistence failed"):
        await MemoryWorker(
            RecordingMemoryRepository(fail_create=True), MemoryPolicy()
        ).handle_memory_candidate_created(
            _event([candidate]), [candidate], user_memory_enabled=True
        )


async def test_result_order_matches_candidate_order() -> None:
    first = _candidate("First preference", source_id="msg_first")
    second = _candidate(
        "Second active goal",
        source_id="msg_second",
        candidate_type=MemoryType.ACTIVE_GOAL,
        requires_confirmation=True,
    )
    results = await MemoryWorker(
        RecordingMemoryRepository(), MemoryPolicy()
    ).handle_memory_candidate_created(
        _event([first, second]), [first, second], user_memory_enabled=True
    )
    assert [result.operation for result in results] == [
        MemoryOperation.CREATE,
        MemoryOperation.CREATE,
    ]
