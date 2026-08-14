"""Session-state repository protocol, in-memory store, and SQLAlchemy implementation."""

from typing import Protocol, cast
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.common import MessageId, SessionId
from schemas.state import SessionState
from storage.models.session import SessionModel
from storage.models.session_state import SessionStateVersionModel
from storage.repositories.errors import SessionNotFoundError


class StateRepository(Protocol):
    """Persistence boundary for versioned SessionState objects."""

    async def get_current(self, session_id: SessionId) -> SessionState:
        """Return the current state, creating an empty state if needed."""

    async def save_version(
        self,
        state: SessionState,
        *,
        source_message_id: MessageId | None = None,
    ) -> None:
        """Persist a new state version."""


class InMemoryStateRepository:
    """Volatile state repository for tests and local fake pipelines."""

    def __init__(self) -> None:
        """Create an empty in-memory state store."""

        self._states: dict[SessionId, SessionState] = {}

    async def get_current(self, session_id: SessionId) -> SessionState:
        """Return the current session state or initialize version zero."""

        if session_id not in self._states:
            self._states[session_id] = SessionState(session_id=session_id)
        return self._states[session_id]

    async def save_version(
        self,
        state: SessionState,
        *,
        source_message_id: MessageId | None = None,
    ) -> None:
        """Replace the current state with the supplied new version."""

        _ = source_message_id
        self._states[state.session_id] = state


class SqlAlchemyStateRepository:
    """SQLAlchemy persistence for immutable session-state versions."""

    def __init__(self, session: AsyncSession) -> None:
        """Use the caller-owned asynchronous session."""

        self._session = session

    async def get_current(self, session_id: SessionId) -> SessionState:
        """Return the current persisted state or an empty version-zero state."""

        session_row = await self._session.get(SessionModel, str(session_id))
        if session_row is None:
            raise SessionNotFoundError(f"Session {str(session_id)!r} does not exist.")
        if session_row.current_state_version == 0:
            return SessionState(session_id=session_id)
        statement = (
            select(SessionStateVersionModel)
            .where(SessionStateVersionModel.session_id == str(session_id))
            .order_by(desc(SessionStateVersionModel.version))
            .limit(1)
        )
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        if row is None:
            return SessionState(session_id=session_id)
        return SessionState.model_validate(row.state_json)

    async def save_version(
        self,
        state: SessionState,
        *,
        source_message_id: MessageId | None = None,
    ) -> None:
        """Persist a new immutable state snapshot and update session counters."""

        session_row = await self._session.get(SessionModel, str(state.session_id))
        if session_row is None:
            raise SessionNotFoundError(
                f"Session {str(state.session_id)!r} does not exist."
            )
        previous_row = await self._latest_state_row(state.session_id)
        row = SessionStateVersionModel(
            id=f"state_{uuid4().hex}",
            session_id=str(state.session_id),
            version=state.version,
            state_json=cast(dict[str, object], state.model_dump(mode="json")),
            source_message_id=(
                str(source_message_id) if source_message_id is not None else None
            ),
            previous_version_id=previous_row.id if previous_row is not None else None,
        )
        self._session.add(row)
        session_row.current_state_version = state.version
        await self._session.flush()

    async def _latest_state_row(
        self,
        session_id: SessionId,
    ) -> SessionStateVersionModel | None:
        """Return the latest state row for previous-version linking."""

        statement = (
            select(SessionStateVersionModel)
            .where(SessionStateVersionModel.session_id == str(session_id))
            .order_by(desc(SessionStateVersionModel.version))
            .limit(1)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()


__all__ = [
    "InMemoryStateRepository",
    "SqlAlchemyStateRepository",
    "StateRepository",
]
