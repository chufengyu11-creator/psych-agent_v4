"""Memory retrieval service boundary."""

from schemas.memory import RetrievedMemories
from schemas.state import SessionState


class MemoryRetriever:
    """Retrieves user-authorized memories relevant to the current turn."""

    async def retrieve(
        self,
        user_id: str,
        query: str,
        session_state: SessionState,
        limit: int = 8,
    ) -> RetrievedMemories:
        """Return an empty memory bundle until persistence and embeddings exist."""

        _ = (user_id, query, session_state, limit)
        return RetrievedMemories()
