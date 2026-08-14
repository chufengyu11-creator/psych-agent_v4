"""Rolling-summary repository protocol and implementations."""

from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.common import SessionId, UserId
from schemas.summary import RollingSummary
from storage.models.session import SessionModel
from storage.models.summary import RollingSummaryVersionModel
from storage.repositories.errors import SessionNotFoundError


class SummaryRepository(Protocol):
    """Persistence boundary for rolling summary versions."""

    async def get_current(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> RollingSummary | None:
        """Return the current rolling summary, if one exists."""

    async def save_version(self, user_id: UserId, summary: RollingSummary) -> None:
        """Persist a new rolling summary version."""


class InMemorySummaryRepository:
    """Volatile summary repository used by local fake pipelines."""

    def __init__(self) -> None:
        """Create an empty in-memory summary store."""

        self._summaries: dict[tuple[UserId, SessionId], RollingSummary] = {}

    async def get_current(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> RollingSummary | None:
        """Return the current summary for the session, if present."""

        return self._summaries.get((user_id, session_id))

    async def save_version(self, user_id: UserId, summary: RollingSummary) -> None:
        """Replace the current in-memory summary with the supplied version."""

        self._summaries[(user_id, summary.session_id)] = summary


class SqlAlchemySummaryRepository:
    """SQLAlchemy persistence for immutable rolling-summary versions."""

    def __init__(self, session: AsyncSession) -> None:
        """Use the caller-owned asynchronous session."""

        self._session = session

    async def get_current(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> RollingSummary | None:
        """Return the latest persisted rolling summary, if one exists."""

        session_row = await self._get_owned_session(user_id, session_id)
        if session_row is None:
            raise SessionNotFoundError(f"Session {str(session_id)!r} does not exist.")
        if session_row.current_summary_version == 0:
            return None
        latest = await self._latest_summary_row(session_row.id)
        if latest is None:
            return None
        return RollingSummary.model_validate(latest.summary_json)

    async def save_version(self, user_id: UserId, summary: RollingSummary) -> None:
        """Persist one immutable summary snapshot and update session counters."""

        session_row = await self._get_owned_session(user_id, summary.session_id)
        if session_row is None:
            raise SessionNotFoundError(
                f"Session {str(summary.session_id)!r} does not exist."
            )
        previous_row = await self._latest_summary_row(session_row.id)
        row = RollingSummaryVersionModel(
            id=f"summary_{uuid4().hex}",
            session_pk=session_row.id,
            summary_version=summary.summary_version,
            summary_json=cast(dict[str, object], summary.model_dump(mode="json")),
            covered_from_message_id=str(summary.covered_from),
            covered_to_message_id=str(summary.covered_to),
            previous_version_id=previous_row.id if previous_row is not None else None,
        )
        self._session.add(row)
        session_row.current_summary_version = summary.summary_version
        await self._session.flush()

    async def _latest_summary_row(
        self,
        session_pk: UUID,
    ) -> RollingSummaryVersionModel | None:
        """Return the latest rolling-summary row for a session."""

        statement = (
            select(RollingSummaryVersionModel)
            .where(RollingSummaryVersionModel.session_pk == session_pk)
            .order_by(desc(RollingSummaryVersionModel.summary_version))
            .limit(1)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def _get_owned_session(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> SessionModel | None:
        """Resolve the internal session key through the owner-scoped pair."""

        statement = select(SessionModel).where(
            SessionModel.user_id == str(user_id),
            SessionModel.session_id == str(session_id),
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()


__all__ = [
    "InMemorySummaryRepository",
    "SqlAlchemySummaryRepository",
    "SummaryRepository",
]
