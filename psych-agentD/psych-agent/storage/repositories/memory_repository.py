"""Memory repository protocol, in-memory store, and SQLAlchemy implementation."""

from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.common import MemoryId, MessageId, SourceReference, UserId, utc_now
from schemas.memory import (
    LongTermMemory,
    MemoryCandidate,
    MemoryOperation,
    MemoryPolicyDecision,
    MemorySensitivity,
    MemoryType,
    MemoryWriteResult,
)
from storage.models.memory import (
    MEMORY_STATUS_ACTIVE,
    MEMORY_STATUS_CONFLICTED,
    MEMORY_STATUS_DELETED,
    MEMORY_STATUS_EXPIRED,
    MEMORY_STATUS_PENDING_CONFIRMATION,
    MEMORY_STATUS_SUPERSEDED,
    LongTermMemoryModel,
)
from storage.models.message import MessageModel
from storage.models.session import SessionModel

_CONTENT_WRITING_OPERATIONS = frozenset(
    {
        MemoryOperation.CREATE,
        MemoryOperation.REINFORCE,
        MemoryOperation.SUPERSEDE,
    }
)
_TARGET_OPERATIONS = frozenset(
    {
        MemoryOperation.REINFORCE,
        MemoryOperation.SUPERSEDE,
        MemoryOperation.MARK_CONFLICT,
        MemoryOperation.EXPIRE,
        MemoryOperation.DELETE,
    }
)


class MemoryRepository(Protocol):
    """Persistence boundary for policy-approved long-term memories."""

    async def list_active(self, user_id: UserId) -> list[LongTermMemory]:
        """Return active memories available for policy and retrieval."""

    async def create(
        self,
        user_id: UserId,
        candidate: MemoryCandidate,
        decision: MemoryPolicyDecision,
    ) -> MemoryWriteResult:
        """Apply one policy-approved candidate to the memory store."""


class InMemoryMemoryRepository:
    """Volatile memory repository for local fake close-session tests."""

    def __init__(self) -> None:
        """Create an empty memory store."""

        self._records: dict[MemoryId, LongTermMemory] = {}
        self._owners: dict[MemoryId, UserId] = {}

    async def list_active(self, user_id: UserId) -> list[LongTermMemory]:
        """Return active memories for the user.

        Ownership stays internal because the public LongTermMemory contract does
        not expose user_id.
        """

        return [
            record
            for memory_id, record in self._records.items()
            if self._owners[memory_id] == user_id
        ]

    async def create(
        self,
        user_id: UserId,
        candidate: MemoryCandidate,
        decision: MemoryPolicyDecision,
    ) -> MemoryWriteResult:
        """Apply one memory decision in memory."""

        if not decision.allowed or decision.operation is None:
            return _not_applied(candidate.recommended_operation, decision)
        operation = decision.operation
        if not _target_contract_is_valid(operation, decision.target_memory_id):
            return _not_applied(operation, decision)

        content = _resolved_content(candidate, decision)
        if operation in _CONTENT_WRITING_OPERATIONS and content is None:
            return _not_applied(operation, decision)
        if operation == MemoryOperation.CREATE:
            assert content is not None
            return self._create_record(user_id, candidate, decision, content)

        target_memory_id = decision.target_memory_id
        assert target_memory_id is not None
        target = self._records.get(target_memory_id)
        if target is None or self._owners.get(target_memory_id) != user_id:
            return _not_applied(operation, decision)
        if operation == MemoryOperation.REINFORCE:
            assert content is not None
            combined_sources = _dedupe_message_ids(
                [
                    *(source.message_id for source in target.source),
                    *candidate.source_message_ids,
                ]
            )
            self._records[target_memory_id] = target.model_copy(
                update={
                    "content": content,
                    "source": [
                        SourceReference(message_id=message_id)
                        for message_id in combined_sources
                    ],
                }
            )
            return _applied(target_memory_id, operation, decision)
        if operation == MemoryOperation.SUPERSEDE:
            assert content is not None
            result = self._create_record(user_id, candidate, decision, content)
            if result.applied and not decision.requires_user_confirmation:
                del self._records[target_memory_id]
                del self._owners[target_memory_id]
            return result

        del self._records[target_memory_id]
        del self._owners[target_memory_id]
        return _applied(target_memory_id, operation, decision)

    def _create_record(
        self,
        user_id: UserId,
        candidate: MemoryCandidate,
        decision: MemoryPolicyDecision,
        content: str,
    ) -> MemoryWriteResult:
        """Store one in-memory record after contract validation."""

        operation = decision.operation
        assert operation is not None
        memory_id = MemoryId(f"mem_{uuid4().hex}")
        self._records[memory_id] = LongTermMemory(
            id=memory_id,
            memory_type=candidate.candidate_type,
            content=content,
            sensitivity=candidate.sensitivity,
            confidence=candidate.confidence,
            source=[
                SourceReference(message_id=message_id)
                for message_id in _dedupe_message_ids(candidate.source_message_ids)
            ],
        )
        self._owners[memory_id] = user_id
        return _applied(memory_id, operation, decision)


class SqlAlchemyMemoryRepository:
    """SQLAlchemy persistence for policy-controlled long-term memories."""

    def __init__(self, session: AsyncSession) -> None:
        """Use the caller-owned asynchronous session."""

        self._session = session

    async def list_active(self, user_id: UserId) -> list[LongTermMemory]:
        """Return active memories for a user."""

        statement = (
            select(LongTermMemoryModel)
            .where(
                LongTermMemoryModel.user_id == str(user_id),
                LongTermMemoryModel.status == MEMORY_STATUS_ACTIVE,
            )
            .order_by(LongTermMemoryModel.updated_at)
        )
        result = await self._session.execute(statement)
        return [self._to_schema(row) for row in result.scalars().all()]

    async def create(
        self,
        user_id: UserId,
        candidate: MemoryCandidate,
        decision: MemoryPolicyDecision,
    ) -> MemoryWriteResult:
        """Apply one policy decision and flush the memory change."""

        if not decision.allowed or decision.operation is None:
            return _not_applied(candidate.recommended_operation, decision)
        operation = decision.operation
        if not _target_contract_is_valid(operation, decision.target_memory_id):
            return _not_applied(operation, decision)

        content = _resolved_content(candidate, decision)
        if operation in _CONTENT_WRITING_OPERATIONS:
            if content is None:
                return _not_applied(operation, decision)
            if not await self._sources_belong_to_user(
                user_id,
                candidate.source_message_ids,
            ):
                return _not_applied(operation, decision)
        if operation == MemoryOperation.CREATE:
            assert content is not None
            row = self._new_row(user_id, candidate, decision, content)
            self._session.add(row)
            await self._session.flush()
            return _applied(MemoryId(row.id), operation, decision)

        target_memory_id = decision.target_memory_id
        assert target_memory_id is not None
        target = await self._get_owned_target(user_id, target_memory_id)
        if target is None:
            return _not_applied(operation, decision)
        if operation == MemoryOperation.REINFORCE:
            assert content is not None
            return await self._reinforce(target, candidate, decision, content)
        if operation == MemoryOperation.SUPERSEDE:
            assert content is not None
            row = self._new_row(user_id, candidate, decision, content)
            self._session.add(row)
            if not decision.requires_user_confirmation:
                target.status = MEMORY_STATUS_SUPERSEDED
            await self._session.flush()
            return _applied(MemoryId(row.id), operation, decision)
        if decision.requires_user_confirmation:
            return _not_applied(operation, decision)

        now = datetime.now(UTC)
        if operation == MemoryOperation.MARK_CONFLICT:
            target.status = MEMORY_STATUS_CONFLICTED
        elif operation == MemoryOperation.EXPIRE:
            target.status = MEMORY_STATUS_EXPIRED
            target.expired_at = now
            target.valid_to = now
        elif operation == MemoryOperation.DELETE:
            target.status = MEMORY_STATUS_DELETED
            target.deleted_at = now
            target.valid_to = now
        await self._session.flush()
        return _applied(MemoryId(target.id), operation, decision)

    def _new_row(
        self,
        user_id: UserId,
        candidate: MemoryCandidate,
        decision: MemoryPolicyDecision,
        content: str,
    ) -> LongTermMemoryModel:
        """Build one new owned memory row without flushing it."""

        return LongTermMemoryModel(
            id=f"mem_{uuid4().hex}",
            user_id=str(user_id),
            memory_type=candidate.candidate_type.value,
            content=content,
            sensitivity=candidate.sensitivity.value,
            confidence=candidate.confidence,
            source_message_ids=[
                str(message_id)
                for message_id in _dedupe_message_ids(candidate.source_message_ids)
            ],
            source_type=candidate.source_type.value,
            status=(
                MEMORY_STATUS_PENDING_CONFIRMATION
                if decision.requires_user_confirmation
                else MEMORY_STATUS_ACTIVE
            ),
            requires_user_confirmation=decision.requires_user_confirmation,
            user_confirmed=not decision.requires_user_confirmation,
            supersedes_memory_id=(
                str(decision.target_memory_id)
                if decision.operation == MemoryOperation.SUPERSEDE
                else None
            ),
            conflict_memory_ids=[],
            reinforcement_count=0,
            valid_from=None if decision.requires_user_confirmation else utc_now(),
        )

    async def _reinforce(
        self,
        row: LongTermMemoryModel,
        candidate: MemoryCandidate,
        decision: MemoryPolicyDecision,
        content: str,
    ) -> MemoryWriteResult:
        """Increment reinforcement metadata on an existing memory."""

        if decision.requires_user_confirmation:
            return _not_applied(MemoryOperation.REINFORCE, decision)
        row.content = content
        row.source_message_ids = [
            str(message_id)
            for message_id in _dedupe_message_ids(
                [
                    *(MessageId(message_id) for message_id in row.source_message_ids),
                    *candidate.source_message_ids,
                ]
            )
        ]
        row.reinforcement_count += 1
        row.last_reinforced_at = datetime.now(UTC)
        await self._session.flush()
        return _applied(
            MemoryId(row.id),
            MemoryOperation.REINFORCE,
            decision,
        )

    async def _get_owned_target(
        self,
        user_id: UserId,
        memory_id: MemoryId,
    ) -> LongTermMemoryModel | None:
        """Return a target only when both ID and owner match in SQL."""

        statement = select(LongTermMemoryModel).where(
            LongTermMemoryModel.id == str(memory_id),
            LongTermMemoryModel.user_id == str(user_id),
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def _sources_belong_to_user(
        self,
        user_id: UserId,
        message_ids: list[MessageId],
    ) -> bool:
        """Require every persisted source message to belong to the current user."""

        source_ids = {str(message_id) for message_id in message_ids}
        if not source_ids:
            return False
        statement = (
            select(MessageModel.id)
            .join(SessionModel, MessageModel.session_id == SessionModel.id)
            .where(
                MessageModel.id.in_(source_ids),
                SessionModel.user_id == str(user_id),
            )
        )
        result = await self._session.execute(statement)
        owned_source_ids = set(result.scalars().all())
        return owned_source_ids == source_ids

    def _to_schema(self, row: LongTermMemoryModel) -> LongTermMemory:
        """Convert one ORM row into the public memory contract."""

        return LongTermMemory(
            id=MemoryId(row.id),
            memory_type=MemoryType(row.memory_type),
            content=row.content,
            sensitivity=MemorySensitivity(row.sensitivity),
            confidence=row.confidence,
            source=[
                SourceReference(message_id=MessageId(message_id))
                for message_id in row.source_message_ids
            ],
        )


def _target_contract_is_valid(
    operation: MemoryOperation,
    target_memory_id: MemoryId | None,
) -> bool:
    """Enforce the operation/target contract before any mutation."""

    if operation == MemoryOperation.CREATE:
        return target_memory_id is None
    return operation in _TARGET_OPERATIONS and target_memory_id is not None


def _resolved_content(
    candidate: MemoryCandidate,
    decision: MemoryPolicyDecision,
) -> str | None:
    """Select final persistence content while preserving None semantics."""

    content = (
        decision.sanitized_content
        if decision.sanitized_content is not None
        else candidate.content
    )
    return content if content.strip() else None


def _dedupe_message_ids(message_ids: list[MessageId]) -> list[MessageId]:
    """Preserve source order while removing repeated identifiers."""

    result: list[MessageId] = []
    for message_id in message_ids:
        if message_id not in result:
            result.append(message_id)
    return result


def _not_applied(
    operation: MemoryOperation,
    decision: MemoryPolicyDecision,
) -> MemoryWriteResult:
    """Return a safe failure without target details or memory content."""

    return MemoryWriteResult(
        operation=operation,
        applied=False,
        reason_codes=decision.reason_codes,
    )


def _applied(
    memory_id: MemoryId,
    operation: MemoryOperation,
    decision: MemoryPolicyDecision,
) -> MemoryWriteResult:
    """Return the stable successful write result."""

    return MemoryWriteResult(
        memory_id=memory_id,
        operation=operation,
        applied=True,
        reason_codes=decision.reason_codes,
    )


__all__ = [
    "InMemoryMemoryRepository",
    "MemoryRepository",
    "SqlAlchemyMemoryRepository",
]
