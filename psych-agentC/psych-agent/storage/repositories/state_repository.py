"""Session-state repository protocol and in-memory implementation."""

from typing import Protocol

from schemas.common import SessionId
from schemas.state import SessionState


class StateRepository(Protocol):
    """Persistence boundary for versioned SessionState objects."""

    async def get_current(self, session_id: SessionId) -> SessionState:
        """Return the current state, creating an empty state if needed."""

    async def save_version(self, state: SessionState) -> None:
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

    async def save_version(self, state: SessionState) -> None:
        """Replace the current state with the supplied new version."""

        self._states[state.session_id] = state
