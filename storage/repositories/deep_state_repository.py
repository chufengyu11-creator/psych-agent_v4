"""Repository boundary for completed background deep-state results."""

from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.common import MessageId, SessionId, UserId, utc_now
from schemas.deep_state import DeepStateCandidate
from schemas.state import StateDelta
from storage.models.deep_state import (
    DEEP_STATE_PIPELINE_VERSION,
    DEEP_STATE_STATUS_APPLIED,
    DEEP_STATE_STATUS_EXPIRED,
    DEEP_STATE_STATUS_FAILED,
    DEEP_STATE_STATUS_READY,
    DeepStateResultModel,
)
from storage.models.message import MESSAGE_ROLE_USER, MessageModel
from storage.models.session import SessionModel
from storage.repositories.errors import SessionNotFoundError


class DeepStateRepository(Protocol):
    """Foreground access to ready deep-state results."""

    async def get_latest_ready(
        self,
        user_id: UserId,
        session_id: SessionId,
        *,
        before_sequence: int,
    ) -> DeepStateCandidate | None:
        """Return and lock the newest completed result before this message."""

    async def mark_applied(self, candidate_id: str) -> bool:
        """Mark a locked ready result as applied."""

    async def expire_older_ready(
        self,
        user_id: UserId,
        session_id: SessionId,
        *,
        before_sequence: int,
    ) -> int:
        """Expire ready results older than the result just applied."""


class InMemoryDeepStateRepository:
    """Small deterministic repository used by orchestration tests."""

    def __init__(self) -> None:
        self._candidates: dict[str, DeepStateCandidate] = {}
        self._statuses: dict[str, str] = {}

    def add_ready(self, candidate: DeepStateCandidate) -> None:
        self._candidates[candidate.id] = candidate
        self._statuses[candidate.id] = DEEP_STATE_STATUS_READY

    async def get_latest_ready(
        self,
        user_id: UserId,
        session_id: SessionId,
        *,
        before_sequence: int,
    ) -> DeepStateCandidate | None:
        eligible = [
            candidate
            for candidate in self._candidates.values()
            if candidate.user_id == str(user_id)
            and candidate.session_id == session_id
            and candidate.source_message_sequence < before_sequence
            and self._statuses[candidate.id] == DEEP_STATE_STATUS_READY
        ]
        return max(eligible, key=lambda item: item.source_message_sequence, default=None)

    async def mark_applied(self, candidate_id: str) -> bool:
        if self._statuses.get(candidate_id) != DEEP_STATE_STATUS_READY:
            return False
        self._statuses[candidate_id] = DEEP_STATE_STATUS_APPLIED
        return True

    async def expire_older_ready(
        self,
        user_id: UserId,
        session_id: SessionId,
        *,
        before_sequence: int,
    ) -> int:
        expired = 0
        for candidate in self._candidates.values():
            if (
                candidate.user_id == str(user_id)
                and candidate.session_id == session_id
                and candidate.source_message_sequence < before_sequence
                and self._statuses[candidate.id] == DEEP_STATE_STATUS_READY
            ):
                self._statuses[candidate.id] = DEEP_STATE_STATUS_EXPIRED
                expired += 1
        return expired

    def status_of(self, candidate_id: str) -> str | None:
        return self._statuses.get(candidate_id)


class SqlAlchemyDeepStateRepository:
    """SQLAlchemy persistence for deep-state results and foreground consumption."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_ready(
        self,
        user_id: UserId,
        session_id: SessionId,
        *,
        source_message_id: MessageId,
        source_message_sequence: int,
        base_state_version: int,
        delta: StateDelta,
        pipeline_version: str = DEEP_STATE_PIPELINE_VERSION,
    ) -> str:
        """Idempotently persist one completed deep result as ready."""

        session_row, message_row = await self._resolve_source(
            user_id,
            session_id,
            source_message_id,
            source_message_sequence,
        )
        row = await self._get_source_row(
            session_row.id,
            source_message_id,
            pipeline_version,
        )
        if row is None:
            row = DeepStateResultModel(
                id=f"deep_{uuid4().hex}",
                user_id=str(user_id),
                session_pk=session_row.id,
                source_message_id=message_row.id,
                source_message_sequence=source_message_sequence,
                base_state_version=base_state_version,
                pipeline_version=pipeline_version,
                result_json=cast(dict[str, object], delta.model_dump(mode="json")),
                status=DEEP_STATE_STATUS_READY,
                finished_at=utc_now(),
            )
            self._session.add(row)
        elif row.status not in {
            DEEP_STATE_STATUS_APPLIED,
            DEEP_STATE_STATUS_EXPIRED,
        }:
            row.base_state_version = base_state_version
            row.result_json = cast(
                dict[str, object],
                delta.model_dump(mode="json"),
            )
            row.status = DEEP_STATE_STATUS_READY
            row.error_category = None
            row.finished_at = utc_now()
        await self._session.flush()
        return row.id

    async def save_failed(
        self,
        user_id: UserId,
        session_id: SessionId,
        *,
        source_message_id: MessageId,
        source_message_sequence: int,
        base_state_version: int,
        error_category: str,
        pipeline_version: str = DEEP_STATE_PIPELINE_VERSION,
    ) -> str:
        """Idempotently record a deep-model failure without user content."""

        session_row, message_row = await self._resolve_source(
            user_id,
            session_id,
            source_message_id,
            source_message_sequence,
        )
        row = await self._get_source_row(
            session_row.id,
            source_message_id,
            pipeline_version,
        )
        if row is None:
            row = DeepStateResultModel(
                id=f"deep_{uuid4().hex}",
                user_id=str(user_id),
                session_pk=session_row.id,
                source_message_id=message_row.id,
                source_message_sequence=source_message_sequence,
                base_state_version=base_state_version,
                pipeline_version=pipeline_version,
                result_json={},
                status=DEEP_STATE_STATUS_FAILED,
                error_category=error_category[:128],
                finished_at=utc_now(),
            )
            self._session.add(row)
        elif row.status not in {
            DEEP_STATE_STATUS_APPLIED,
            DEEP_STATE_STATUS_EXPIRED,
        }:
            row.status = DEEP_STATE_STATUS_FAILED
            row.error_category = error_category[:128]
            row.finished_at = utc_now()
        await self._session.flush()
        return row.id

    async def get_latest_ready(
        self,
        user_id: UserId,
        session_id: SessionId,
        *,
        before_sequence: int,
    ) -> DeepStateCandidate | None:
        session_row = await self._get_owned_session(user_id, session_id)
        if session_row is None:
            raise SessionNotFoundError(f"Session {str(session_id)!r} does not exist.")
        statement = (
            select(DeepStateResultModel)
            .where(
                DeepStateResultModel.user_id == str(user_id),
                DeepStateResultModel.session_pk == session_row.id,
                DeepStateResultModel.status == DEEP_STATE_STATUS_READY,
                DeepStateResultModel.source_message_sequence < before_sequence,
            )
            .order_by(
                desc(DeepStateResultModel.source_message_sequence),
                desc(DeepStateResultModel.finished_at),
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return DeepStateCandidate(
            id=row.id,
            user_id=row.user_id,
            session_id=session_id,
            source_message_id=MessageId(row.source_message_id),
            source_message_sequence=row.source_message_sequence,
            base_state_version=row.base_state_version,
            pipeline_version=row.pipeline_version,
            delta=StateDelta.model_validate(row.result_json),
            finished_at=row.finished_at,
        )

    async def mark_applied(self, candidate_id: str) -> bool:
        row = await self._session.get(DeepStateResultModel, candidate_id)
        if row is None or row.status != DEEP_STATE_STATUS_READY:
            return False
        row.status = DEEP_STATE_STATUS_APPLIED
        row.applied_at = utc_now()
        await self._session.flush()
        return True

    async def expire_older_ready(
        self,
        user_id: UserId,
        session_id: SessionId,
        *,
        before_sequence: int,
    ) -> int:
        session_row = await self._get_owned_session(user_id, session_id)
        if session_row is None:
            raise SessionNotFoundError(f"Session {str(session_id)!r} does not exist.")
        statement = (
            update(DeepStateResultModel)
            .where(
                DeepStateResultModel.user_id == str(user_id),
                DeepStateResultModel.session_pk == session_row.id,
                DeepStateResultModel.status == DEEP_STATE_STATUS_READY,
                DeepStateResultModel.source_message_sequence < before_sequence,
            )
            .values(status=DEEP_STATE_STATUS_EXPIRED)
        )
        result = await self._session.execute(statement)
        return int(result.rowcount or 0)

    async def _resolve_source(
        self,
        user_id: UserId,
        session_id: SessionId,
        source_message_id: MessageId,
        source_message_sequence: int,
    ) -> tuple[SessionModel, MessageModel]:
        session_row = await self._get_owned_session(user_id, session_id)
        if session_row is None:
            raise SessionNotFoundError(f"Session {str(session_id)!r} does not exist.")
        statement = select(MessageModel).where(
            MessageModel.id == str(source_message_id),
            MessageModel.session_pk == session_row.id,
            MessageModel.role == MESSAGE_ROLE_USER,
            MessageModel.sequence_number == source_message_sequence,
        )
        result = await self._session.execute(statement)
        message_row = result.scalar_one_or_none()
        if message_row is None:
            raise ValueError("deep-state source message does not exist in this session")
        return session_row, message_row

    async def _get_source_row(
        self,
        session_pk: UUID,
        source_message_id: MessageId,
        pipeline_version: str,
    ) -> DeepStateResultModel | None:
        statement = select(DeepStateResultModel).where(
            DeepStateResultModel.session_pk == session_pk,
            DeepStateResultModel.source_message_id == str(source_message_id),
            DeepStateResultModel.pipeline_version == pipeline_version,
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def _get_owned_session(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> SessionModel | None:
        statement = select(SessionModel).where(
            SessionModel.user_id == str(user_id),
            SessionModel.session_id == str(session_id),
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()


__all__ = [
    "DeepStateRepository",
    "InMemoryDeepStateRepository",
    "SqlAlchemyDeepStateRepository",
]
