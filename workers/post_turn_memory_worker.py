"""Post-turn durable memory extraction for low-risk explicit user statements."""

from dataclasses import dataclass

from agents.memory_curator import MemoryCuratorInput
from schemas.common import MessageId, SessionId, UserId
from schemas.events import MemoryCandidateCreatedEvent
from schemas.memory import MemoryCandidate, MemoryWriteResult
from schemas.summary import SessionFinalizerResult
from services.memory_policy import MemoryPolicy
from storage.models.user import UserModel
from storage.repositories.memory_repository import MemoryRepository
from storage.repositories.message_repository import MessageRepository
from storage.repositories.state_repository import StateRepository
from storage.repositories.summary_repository import SummaryRepository
from workers.memory_worker import MemoryWorker
from workers.session_close_worker import MemoryCuratorProtocol


@dataclass(frozen=True)
class PostTurnMemoryResult:
    """Observable result of one post-turn memory pass."""

    candidate_count: int
    memory_write_count: int
    memory_write_results: list[MemoryWriteResult]


class PostTurnMemoryWorker:
    """Extract and policy-gate durable memories after an active turn commits."""

    def __init__(
        self,
        *,
        message_repository: MessageRepository,
        state_repository: StateRepository,
        summary_repository: SummaryRepository,
        memory_repository: MemoryRepository,
        memory_curator: MemoryCuratorProtocol,
        memory_policy: MemoryPolicy,
        user_model_getter,
        message_limit: int = 12,
    ) -> None:
        if message_limit < 1:
            raise ValueError("message_limit must be at least 1")
        self._message_repository = message_repository
        self._state_repository = state_repository
        self._summary_repository = summary_repository
        self._memory_repository = memory_repository
        self._memory_curator = memory_curator
        self._memory_policy = memory_policy
        self._user_model_getter = user_model_getter
        self._message_limit = message_limit
        self._memory_worker = MemoryWorker(memory_repository, memory_policy)

    async def handle_post_turn_memory(
        self,
        *,
        user_id: UserId,
        session_id: SessionId,
    ) -> PostTurnMemoryResult:
        """Create policy-approved memory writes from recent explicit user evidence."""

        user_row: UserModel | None = await self._user_model_getter(str(user_id))
        user_memory_enabled = bool(user_row and user_row.memory_enabled)
        if not user_memory_enabled:
            return PostTurnMemoryResult(
                candidate_count=0,
                memory_write_count=0,
                memory_write_results=[],
            )

        messages = await self._message_repository.get_recent(
            user_id,
            session_id,
            limit=self._message_limit,
        )
        if not messages:
            return PostTurnMemoryResult(
                candidate_count=0,
                memory_write_count=0,
                memory_write_results=[],
            )
        current_state = await self._state_repository.get_current(user_id, session_id)
        rolling_summary = await self._summary_repository.get_current(user_id, session_id)
        empty_finalizer_result = SessionFinalizerResult(
            session_id=session_id,
            session_summary="",
            candidate_memories=[],
            source_message_ids=[],
        )
        candidates = await self._memory_curator.curate(
            MemoryCuratorInput(
                session_id=session_id,
                messages=messages,
                final_state=current_state,
                finalizer_result=empty_finalizer_result,
                rolling_summary=rolling_summary,
            )
        )
        candidates = _post_turn_allowed_candidates(candidates)
        if not candidates:
            return PostTurnMemoryResult(
                candidate_count=0,
                memory_write_count=0,
                memory_write_results=[],
            )
        results = await self._memory_worker.handle_memory_candidate_created(
            MemoryCandidateCreatedEvent(
                session_id=session_id,
                user_id=user_id,
                candidate_count=len(candidates),
                source_message_ids=_candidate_source_message_ids(candidates),
            ),
            candidates,
            user_memory_enabled=user_memory_enabled,
        )
        return PostTurnMemoryResult(
            candidate_count=len(candidates),
            memory_write_count=sum(result.applied for result in results),
            memory_write_results=results,
        )


def _post_turn_allowed_candidates(
    candidates: list[MemoryCandidate],
) -> list[MemoryCandidate]:
    """Let policy handle final decisions after removing obviously non-durable items."""

    return [candidate for candidate in candidates if candidate.source_message_ids]


def _candidate_source_message_ids(candidates: list[MemoryCandidate]) -> list[MessageId]:
    result: list[MessageId] = []
    for candidate in candidates:
        for source_id in candidate.source_message_ids:
            if source_id not in result:
                result.append(source_id)
    return result


__all__ = ["PostTurnMemoryResult", "PostTurnMemoryWorker"]
