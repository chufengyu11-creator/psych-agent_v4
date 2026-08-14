"""Worker for finalizing sessions and applying policy-gated memory writes."""

import logging
from dataclasses import dataclass
from typing import Protocol

from agents.memory_curator import MemoryCuratorInput
from schemas.common import MessageId, SessionId
from schemas.events import MemoryCandidateCreatedEvent, SessionCloseRequestedEvent
from schemas.memory import MemoryCandidate, MemoryWriteResult
from schemas.summary import SessionFinalizerInput, SessionFinalizerResult
from services.memory_policy import MemoryPolicy
from storage.repositories.intervention_repository import InterventionRepository
from storage.repositories.memory_repository import MemoryRepository
from storage.repositories.message_repository import MessageRepository
from storage.repositories.session_repository import SessionRepository
from storage.repositories.state_repository import StateRepository
from storage.repositories.summary_repository import SummaryRepository
from workers.memory_worker import MemoryWorker

_CANDIDATE_LOGGER = logging.getLogger("memory.candidate_funnel")


class SessionFinalizer(Protocol):
    """Structural contract required by the session-close worker."""

    async def finalize(
        self,
        payload: SessionFinalizerInput,
    ) -> SessionFinalizerResult:
        """Build a typed final-session result."""


class MemoryCuratorProtocol(Protocol):
    """Structural contract for optional memory candidate curation."""

    async def curate(self, payload: MemoryCuratorInput) -> list[MemoryCandidate]:
        """Return grounded and deduplicated candidate memories."""


@dataclass(frozen=True)
class SessionCloseResult:
    """Observable result of one completed session-close workflow."""

    session_id: SessionId
    candidate_memory_count: int
    memory_write_count: int
    memory_write_results: list[MemoryWriteResult]
    finalizer_result: SessionFinalizerResult


class SessionCloseMemoryWorker:
    """Finalize an owned session, gate memories through policy, and close it."""

    def __init__(
        self,
        message_repository: MessageRepository,
        state_repository: StateRepository,
        summary_repository: SummaryRepository,
        intervention_repository: InterventionRepository,
        memory_repository: MemoryRepository,
        session_repository: SessionRepository,
        finalizer: SessionFinalizer,
        memory_policy: MemoryPolicy,
        message_limit: int = 50,
        user_memory_enabled: bool = True,
        memory_curator: MemoryCuratorProtocol | None = None,
    ) -> None:
        """Create a worker from injected finalization and persistence boundaries."""

        if message_limit < 1:
            raise ValueError("message_limit must be at least 1")
        self._message_repository = message_repository
        self._state_repository = state_repository
        self._summary_repository = summary_repository
        self._intervention_repository = intervention_repository
        self._memory_repository = memory_repository
        self._session_repository = session_repository
        self._finalizer = finalizer
        self._memory_policy = memory_policy
        self._message_limit = message_limit
        self._user_memory_enabled = user_memory_enabled
        self._memory_curator = memory_curator
        self._memory_worker = MemoryWorker(memory_repository, memory_policy)

    async def handle_session_close_requested(
        self,
        event: SessionCloseRequestedEvent,
    ) -> SessionCloseResult:
        """Finalize and close an owned session after policy-gated memory handling."""

        await self._session_repository.assert_owned_by(event.user_id, event.session_id)
        messages = await self._message_repository.get_recent(
            event.user_id,
            event.session_id,
            limit=self._message_limit,
        )
        final_state = await self._state_repository.get_current(
            event.user_id,
            event.session_id,
        )
        interventions = await self._intervention_repository.list_for_session(
            event.user_id,
            event.session_id
        )
        rolling_summary = await self._summary_repository.get_current(
            event.user_id,
            event.session_id,
        )
        finalizer_result = await self._finalizer.finalize(
            SessionFinalizerInput(
                session_id=event.session_id,
                messages=messages,
                final_state=final_state,
                interventions=interventions,
                rolling_summary=rolling_summary,
            )
        )
        finalizer_candidate_count = len(finalizer_result.candidate_memories)

        curator_candidate_count: int | None = None
        if self._memory_curator is not None:
            candidate_memories = await self._memory_curator.curate(
                MemoryCuratorInput(
                    session_id=event.session_id,
                    messages=messages,
                    final_state=final_state,
                    finalizer_result=finalizer_result,
                    rolling_summary=rolling_summary,
                )
            )
            curator_candidate_count = len(candidate_memories)
            finalizer_result = finalizer_result.model_copy(
                update={"candidate_memories": candidate_memories}
            )

        candidates = finalizer_result.candidate_memories
        memory_write_results = (
            await self._memory_worker.handle_memory_candidate_created(
                MemoryCandidateCreatedEvent(
                    session_id=event.session_id,
                    user_id=event.user_id,
                    candidate_count=len(candidates),
                    source_message_ids=_candidate_source_message_ids(candidates),
                ),
                candidates,
                user_memory_enabled=self._user_memory_enabled,
            )
        )
        _CANDIDATE_LOGGER.info(
            "finalizer=%d curator=%s total=%d created=%d reinforced=%d superseded=%d rejected=%d",
            finalizer_candidate_count,
            str(curator_candidate_count) if curator_candidate_count is not None else "N/A",
            len(candidates),
            sum(1 for r in memory_write_results if r.applied and r.operation.value == "CREATE"),
            sum(1 for r in memory_write_results if r.applied and r.operation.value == "REINFORCE"),
            sum(1 for r in memory_write_results if r.applied and r.operation.value == "SUPERSEDE"),
            sum(1 for r in memory_write_results if not r.applied),
        )

        await self._session_repository.close(event.user_id, event.session_id)
        return SessionCloseResult(
            session_id=event.session_id,
            candidate_memory_count=len(finalizer_result.candidate_memories),
            memory_write_count=sum(result.applied for result in memory_write_results),
            memory_write_results=memory_write_results,
            finalizer_result=finalizer_result,
    )


def _candidate_source_message_ids(
    candidates: list[MemoryCandidate],
) -> list[MessageId]:
    """Collect candidate sources in first-seen order for event validation."""

    result: list[MessageId] = []
    for candidate in candidates:
        for source_id in candidate.source_message_ids:
            if source_id not in result:
                result.append(source_id)
    return result


__all__ = [
    "MemoryCuratorProtocol",
    "SessionCloseMemoryWorker",
    "SessionCloseResult",
    "SessionFinalizer",
]
