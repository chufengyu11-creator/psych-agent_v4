"""Memory retrieval service boundary."""

import logging
from typing import Protocol

_RAG_FILE_LOG = "/tmp/rag_scores.log"
_RAG_LOGGER = logging.getLogger("memory.rag_scores")
if not _RAG_LOGGER.handlers:
    _handler = logging.FileHandler(_RAG_FILE_LOG, encoding="utf-8")
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(message)s")
    )
    _RAG_LOGGER.addHandler(_handler)
    _RAG_LOGGER.setLevel(logging.INFO)
    _RAG_LOGGER.propagate = False

from schemas.common import UserId
from schemas.memory import (
    LongTermMemory,
    MemoryType,
    RetrievedMemories,
)
from schemas.state import SessionState
from services.embedding_service import SimilarityService, create_similarity_service
from services.memory_query import detect_memory_query_intent, select_relevant_memories


class MemoryRetriever:
    """Retrieves user-authorized memories relevant to the current turn."""

    async def retrieve(
        self,
        user_id: str,
        query: str,
        session_state: SessionState,
        limit: int = 8,
    ) -> RetrievedMemories:
        """Return an empty memory bundle for runtimes without durable memory."""

        _ = (user_id, query, session_state, limit)
        return RetrievedMemories()


class ActiveMemoryRepository(Protocol):
    """Repository contract needed for simple active-memory retrieval."""

    async def list_active(self, user_id: UserId) -> list[LongTermMemory]:
        """Return active memories owned by the user."""


class RepositoryMemoryRetriever:
    """Retrieve active long-term memories from a persistence repository."""

    def __init__(
        self,
        repository: ActiveMemoryRepository,
        similarity: SimilarityService | None = None,
    ) -> None:
        """Create a retriever from a caller-owned repository/session boundary."""

        self._repository = repository
        self._similarity = similarity or create_similarity_service()

    async def retrieve(
        self,
        user_id: str,
        query: str,
        session_state: SessionState,
        limit: int = 8,
    ) -> RetrievedMemories:
        """Return active memories grouped into the context contract."""

        _ = session_state
        owner_id = UserId(user_id)
        limited_reader = getattr(self._repository, "list_active_limited", None)
        if callable(limited_reader):
            # SQL-backed V4 uses this bounded query; semantic ranking remains
            # a read-side enhancement for lightweight repositories only.
            memories = await limited_reader(owner_id, limit)
        else:
            all_memories = await self._repository.list_active(owner_id)
            memories = _rank_by_relevance(all_memories, query, self._similarity, limit)

        intent = detect_memory_query_intent(query)
        pending_memories: list[LongTermMemory] = []
        pending_count = 0
        pending_page_reader = getattr(
            self._repository,
            "list_pending_confirmation_page",
            None,
        )
        if callable(pending_page_reader):
            pending_candidates, pending_count = await pending_page_reader(
                owner_id,
                max(limit * 2, limit),
            )
            pending_memories = select_relevant_memories(
                pending_candidates,
                query,
                intent,
                limit=limit,
            )
        else:
            pending_counter = getattr(
                self._repository,
                "count_pending_confirmation",
                None,
            )
            if callable(pending_counter):
                pending_count = int(await pending_counter(owner_id))
        semantic: list[LongTermMemory] = []
        episodic: list[LongTermMemory] = []
        active_goals: list[str] = []
        interaction_preferences: list[str] = []
        active_goal_memories: list[LongTermMemory] = []
        interaction_preference_memories: list[LongTermMemory] = []
        for memory in memories:
            if memory.memory_type == MemoryType.INTERACTION_PREFERENCE:
                interaction_preferences.append(memory.content)
                interaction_preference_memories.append(memory)
            elif memory.memory_type == MemoryType.ACTIVE_GOAL:
                active_goals.append(memory.content)
                active_goal_memories.append(memory)
            elif memory.memory_type == MemoryType.EPISODIC:
                episodic.append(memory)
            else:
                semantic.append(memory)
        return RetrievedMemories(
            active_memories=memories,
            pending_confirmation_count=pending_count,
            pending_confirmation_memories=pending_memories,
            semantic_memories=semantic,
            episodic_memories=episodic,
            active_goal_memories=active_goal_memories,
            interaction_preference_memories=interaction_preference_memories,
            active_goals=active_goals,
            interaction_preferences=interaction_preferences,
        )


def _rank_by_relevance(
    memories: list[LongTermMemory],
    query: str,
    similarity: SimilarityService,
    limit: int,
) -> list[LongTermMemory]:
    """Use semantic similarity only to select context candidates, never to mutate."""

    if not query.strip() or len(memories) <= limit:
        return memories
    scored = [
        (similarity.similarity(query, memory.content), index, memory)
        for index, memory in enumerate(memories)
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    _RAG_LOGGER.info("query=%s", query[:60])
    for score, index, memory in scored:
        _RAG_LOGGER.info(
            "  %.3f [%s] %s",
            score,
            "取" if score >= 0.35 else "弃",
            memory.content[:60],
        )
    selected = [item[2] for item in scored if item[0] >= 0.35][: max(1, limit - 1)]
    if not selected:
        _RAG_LOGGER.info("  -> 无命中，回退最新 %d 条", limit)
        return memories[:limit]
    if memories[0] not in selected:
        selected.append(memories[0])
    return selected[:limit]
