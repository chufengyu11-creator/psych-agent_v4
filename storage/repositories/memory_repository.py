"""Memory repository protocol, in-memory store, and SQLAlchemy implementation."""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol
from uuid import uuid4

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.common import MemoryId, MessageId, SessionId, SourceReference, UserId, utc_now
from schemas.memory import (
    LongTermMemory,
    MemoryCandidate,
    MemoryOperation,
    MemoryPolicyDecision,
    PendingMemoryResolution,
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


@dataclass(frozen=True)
class LongTermMemoryRecord:
    """User-visible durable memory metadata for the V4 management panel."""

    id: str
    memory_type: str
    content: str
    sensitivity: str
    confidence: float
    source_message_ids: list[str]
    source_type: str
    status: str
    requires_user_confirmation: bool
    user_confirmed: bool
    reinforcement_count: int
    created_at: datetime
    updated_at: datetime

_CONTENT_WRITING_OPERATIONS = frozenset(
    {
        MemoryOperation.CREATE,
        MemoryOperation.REINFORCE,
        MemoryOperation.MERGE,
        MemoryOperation.SUPERSEDE,
    }
)
_TARGET_OPERATIONS = frozenset(
    {
        MemoryOperation.REINFORCE,
        MemoryOperation.MERGE,
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
        *,
        session_id: SessionId | None = None,
    ) -> MemoryWriteResult:
        """Apply one policy-approved candidate to the memory store."""

    async def list_records(
        self, user_id: UserId, *, status: str | None = MEMORY_STATUS_ACTIVE,
        limit: int = 100,
    ) -> list[LongTermMemoryRecord]:
        """Return rows required by the existing V4 memory-management API."""

    async def soft_delete(self, user_id: UserId, memory_id: MemoryId) -> bool:
        """Soft-delete one owned memory."""


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
        *,
        session_id: SessionId | None = None,
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
        if operation == MemoryOperation.MERGE:
            assert content is not None
            merged = _merge_contents(target.content, content)
            result = self._create_record(user_id, candidate, decision, merged)
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
            .order_by(desc(LongTermMemoryModel.updated_at))
        )
        result = await self._session.execute(statement)
        return [self._to_schema(row) for row in result.scalars().all()]

    async def list_records(
        self,
        user_id: UserId,
        *,
        status: str | None = MEMORY_STATUS_ACTIVE,
        limit: int = 100,
    ) -> list[LongTermMemoryRecord]:
        """Preserve V4's list/status/limit memory-management behavior."""

        if limit < 1:
            return []
        statement = select(LongTermMemoryModel).where(
            LongTermMemoryModel.user_id == str(user_id)
        )
        if status is not None:
            statement = statement.where(LongTermMemoryModel.status == status)
        result = await self._session.execute(
            statement.order_by(desc(LongTermMemoryModel.updated_at)).limit(limit)
        )
        return [self._to_record(row) for row in result.scalars().all()]

    async def soft_delete(self, user_id: UserId, memory_id: MemoryId) -> bool:
        """Soft-delete one owned memory without removing its audit history."""

        row = await self._get_owned_target(user_id, memory_id)
        if row is None:
            return False
        now = datetime.now(UTC)
        row.status = MEMORY_STATUS_DELETED
        row.deleted_at = now
        row.valid_to = now
        await self._session.flush()
        return True

    async def list_active_limited(
        self,
        user_id: UserId,
        limit: int,
    ) -> list[LongTermMemory]:
        """Return the newest active memories with the limit enforced in SQL."""

        if limit < 1:
            return []
        statement = (
            select(LongTermMemoryModel)
            .where(
                LongTermMemoryModel.user_id == str(user_id),
                LongTermMemoryModel.status == MEMORY_STATUS_ACTIVE,
            )
            .order_by(desc(LongTermMemoryModel.updated_at))
            .limit(limit)
        )
        result = await self._session.execute(statement)
        return [self._to_schema(row) for row in result.scalars().all()]

    async def list_pending_confirmation_page(
        self,
        user_id: UserId,
        limit: int,
    ) -> tuple[list[LongTermMemory], int]:
        """Return newest pending candidates and their total count in one query."""

        if limit < 1:
            return [], await self.count_pending_confirmation(user_id)
        total_count = func.count(LongTermMemoryModel.id).over().label("total_count")
        statement = (
            select(LongTermMemoryModel, total_count)
            .where(
                LongTermMemoryModel.user_id == str(user_id),
                LongTermMemoryModel.status == MEMORY_STATUS_PENDING_CONFIRMATION,
            )
            .order_by(desc(LongTermMemoryModel.updated_at))
            .limit(limit)
        )
        rows = (await self._session.execute(statement)).all()
        if not rows:
            return [], 0
        return (
            [self._to_schema(row[0]) for row in rows],
            int(rows[0][1]),
        )

    async def count_pending_confirmation(self, user_id: UserId) -> int:
        """Count pending records owned by the user without loading their content."""

        statement = (
            select(func.count())
            .select_from(LongTermMemoryModel)
            .where(
                LongTermMemoryModel.user_id == str(user_id),
                LongTermMemoryModel.status == MEMORY_STATUS_PENDING_CONFIRMATION,
            )
        )
        value = await self._session.scalar(statement)
        return int(value or 0)

    async def create(
        self,
        user_id: UserId,
        candidate: MemoryCandidate,
        decision: MemoryPolicyDecision,
        *,
        session_id: SessionId | None = None,
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
            content_hash = _content_hash(content)
            existing = await self._find_active_by_hash(user_id, content_hash)
            if existing is not None:
                return await self._reinforce(existing, candidate, decision, content)
            row = self._new_row(
                user_id,
                candidate,
                decision,
                content,
                content_hash=content_hash,
                session_id=session_id,
            )
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
        if operation in {MemoryOperation.SUPERSEDE, MemoryOperation.MERGE}:
            assert content is not None
            if operation == MemoryOperation.MERGE:
                content = _merge_contents(target.content, content)
            row = self._new_row(
                user_id,
                candidate,
                decision,
                content,
                content_hash=_content_hash(content),
                session_id=session_id,
            )
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

    async def resolve_pending_confirmation(
        self,
        user_id: UserId,
        memory_id: MemoryId,
        *,
        confirmed: bool,
    ) -> PendingMemoryResolution | None:
        """Confirm or reject one user-owned pending memory atomically."""

        statement = (
            select(LongTermMemoryModel)
            .where(
                LongTermMemoryModel.id == str(memory_id),
                LongTermMemoryModel.user_id == str(user_id),
                LongTermMemoryModel.status == MEMORY_STATUS_PENDING_CONFIRMATION,
            )
            .with_for_update()
        )
        row = (await self._session.execute(statement)).scalar_one_or_none()
        if row is None:
            return None

        now = datetime.now(UTC)
        superseded_memory_id = (
            MemoryId(row.supersedes_memory_id)
            if row.supersedes_memory_id is not None
            else None
        )
        if not confirmed:
            row.status = MEMORY_STATUS_DELETED
            row.requires_user_confirmation = False
            row.deleted_at = now
            row.valid_to = now
            await self._session.flush()
            return PendingMemoryResolution(
                memory_id=MemoryId(row.id),
                confirmed=False,
                status=row.status,
                superseded_memory_id=superseded_memory_id,
            )

        if row.supersedes_memory_id is not None:
            target = await self._get_owned_target(
                user_id,
                MemoryId(row.supersedes_memory_id),
            )
            if target is None or target.status != MEMORY_STATUS_ACTIVE:
                row.status = MEMORY_STATUS_CONFLICTED
                row.requires_user_confirmation = False
                row.conflict_memory_ids = [row.supersedes_memory_id]
                await self._session.flush()
                return PendingMemoryResolution(
                    memory_id=MemoryId(row.id),
                    confirmed=False,
                    status=row.status,
                    superseded_memory_id=superseded_memory_id,
                )
            target.status = MEMORY_STATUS_SUPERSEDED
            target.valid_to = now

        row.status = MEMORY_STATUS_ACTIVE
        row.requires_user_confirmation = False
        row.user_confirmed = True
        row.valid_from = now
        await self._session.flush()
        return PendingMemoryResolution(
            memory_id=MemoryId(row.id),
            confirmed=True,
            status=row.status,
            superseded_memory_id=superseded_memory_id,
        )

    def _new_row(
        self,
        user_id: UserId,
        candidate: MemoryCandidate,
        decision: MemoryPolicyDecision,
        content: str,
        *,
        content_hash: str,
        session_id: SessionId | None = None,
    ) -> LongTermMemoryModel:
        """Build one new owned memory row without flushing it."""

        return LongTermMemoryModel(
            id=f"mem_{uuid4().hex}",
            user_id=str(user_id),
            memory_type=candidate.candidate_type.value,
            content=content,
            content_hash=content_hash,
            source_session_id=str(session_id) if session_id is not None else None,
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
                if decision.operation in {MemoryOperation.SUPERSEDE, MemoryOperation.MERGE}
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
        row.content_hash = _content_hash(content)
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

        statement = (
            select(LongTermMemoryModel)
            .where(
                LongTermMemoryModel.id == str(memory_id),
                LongTermMemoryModel.user_id == str(user_id),
            )
            .with_for_update()
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def _find_active_by_hash(
        self,
        user_id: UserId,
        content_hash: str,
    ) -> LongTermMemoryModel | None:
        """Return the active row that collided with a concurrent duplicate write."""

        statement = select(LongTermMemoryModel).where(
            LongTermMemoryModel.user_id == str(user_id),
            LongTermMemoryModel.status == MEMORY_STATUS_ACTIVE,
            LongTermMemoryModel.content_hash == content_hash,
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
            .join(SessionModel, MessageModel.session_pk == SessionModel.id)
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

    def _to_record(self, row: LongTermMemoryModel) -> LongTermMemoryRecord:
        return LongTermMemoryRecord(
            id=row.id,
            memory_type=row.memory_type,
            content=row.content,
            sensitivity=row.sensitivity,
            confidence=row.confidence,
            source_message_ids=list(row.source_message_ids),
            source_type=row.source_type,
            status=row.status,
            requires_user_confirmation=row.requires_user_confirmation,
            user_confirmed=row.user_confirmed,
            reinforcement_count=row.reinforcement_count,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def _target_contract_is_valid(
    operation: MemoryOperation,
    target_memory_id: MemoryId | None,
) -> bool:
    """Enforce the operation/target contract before any mutation."""

    if operation == MemoryOperation.CREATE:
        return target_memory_id is None
    return operation in _TARGET_OPERATIONS and target_memory_id is not None


def _content_hash(content: str) -> str:
    """Return a stable normalized-content hash for deduplication."""

    normalized = " ".join(content.casefold().strip().split())
    return sha256(normalized.encode("utf-8")).hexdigest()


def _merge_contents(existing: str, candidate: str) -> str:
    """Create a lossless, deterministic merged fact pending user confirmation."""

    parts = list(dict.fromkeys(item.strip() for item in (existing, candidate) if item.strip()))
    return "；".join(parts)


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
    "LongTermMemoryRecord",
    "MemoryRepository",
    "SqlAlchemyMemoryRepository",
]
