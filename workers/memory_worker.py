"""Policy-gated orchestration for one batch of memory candidates."""

from schemas.events import MemoryCandidateCreatedEvent
from schemas.memory import (
    MemoryCandidate,
    MemoryPolicyInput,
    MemoryWriteResult,
)
from services.memory_policy import MemoryPolicy
from storage.repositories.memory_repository import MemoryRepository


class MemoryWorker:
    """Validate an event, evaluate each candidate, and persist only allowed writes."""

    def __init__(
        self,
        memory_repository: MemoryRepository,
        memory_policy: MemoryPolicy,
    ) -> None:
        self._memory_repository = memory_repository
        self._memory_policy = memory_policy

    async def handle_memory_candidate_created(
        self,
        event: MemoryCandidateCreatedEvent,
        candidates: list[MemoryCandidate],
        *,
        user_memory_enabled: bool,
    ) -> list[MemoryWriteResult]:
        """Process candidates in order after validating event metadata."""

        _validate_event(event, candidates)
        results: list[MemoryWriteResult] = []
        for candidate in candidates:
            existing_memories = await self._memory_repository.list_active(event.user_id)
            decision = self._memory_policy.evaluate_candidate(
                MemoryPolicyInput(
                    candidate=candidate,
                    existing_memories=existing_memories,
                    user_memory_enabled=user_memory_enabled,
                    session_id=event.session_id,
                )
            )
            if not decision.allowed:
                results.append(
                    MemoryWriteResult(
                        operation=decision.operation or candidate.recommended_operation,
                        applied=False,
                        reason_codes=decision.reason_codes,
                    )
                )
                continue
            results.append(
                await self._memory_repository.create(
                    event.user_id,
                    candidate,
                    decision,
                )
            )
        return results


def _validate_event(
    event: MemoryCandidateCreatedEvent,
    candidates: list[MemoryCandidate],
) -> None:
    if event.candidate_count != len(candidates):
        raise ValueError("event candidate_count does not match candidates")
    candidate_source_ids = {
        source_id
        for candidate in candidates
        for source_id in candidate.source_message_ids
    }
    if set(event.source_message_ids) != candidate_source_ids:
        raise ValueError("event source_message_ids do not match candidate sources")


__all__ = ["MemoryWorker"]
