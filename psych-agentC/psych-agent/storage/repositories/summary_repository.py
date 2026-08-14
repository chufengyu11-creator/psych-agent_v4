"""Rolling-summary repository protocol and in-memory implementation."""

from typing import Protocol

from schemas.common import SessionId
from schemas.summary import RollingSummary


class SummaryRepository(Protocol):
    """Persistence boundary for the current rolling summary."""

    async def get_current(self, session_id: SessionId) -> RollingSummary | None:
        """Return the current rolling summary, if one exists."""


class InMemorySummaryRepository:
    """Volatile summary repository used before the summarizer exists."""

    async def get_current(self, session_id: SessionId) -> RollingSummary | None:
        """Always return None until summary generation is implemented."""

        _ = session_id
        return None
