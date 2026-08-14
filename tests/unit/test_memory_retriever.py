"""Tests for long-term memory retrieval grouping."""

from schemas.common import MemoryId, MessageId, SourceReference, UserId
from schemas.memory import (
    LongTermMemory,
    MemorySensitivity,
    MemoryType,
)
from schemas.state import SessionState
from services.memory_retriever import RepositoryMemoryRetriever


class FakeActiveMemoryRepository:
    """Small repository stub for retriever tests."""

    def __init__(self, memories: list[LongTermMemory]) -> None:
        self.memories = memories
        self.user_ids: list[UserId] = []

    async def list_active(self, user_id: UserId) -> list[LongTermMemory]:
        """Return configured memories and record the requested owner."""

        self.user_ids.append(user_id)
        return self.memories


def _memory(
    memory_id: str,
    memory_type: MemoryType,
    content: str,
) -> LongTermMemory:
    """Build one active memory fixture."""

    return LongTermMemory(
        id=MemoryId(memory_id),
        memory_type=memory_type,
        content=content,
        sensitivity=MemorySensitivity.LOW,
        confidence=0.9,
        source=[SourceReference(message_id=MessageId("msg_memory_source"))],
    )


async def test_repository_memory_retriever_groups_active_memories() -> None:
    """Active memories should be grouped into the context contract."""

    repository = FakeActiveMemoryRepository(
        [
            _memory(
                "mem_pref",
                MemoryType.INTERACTION_PREFERENCE,
                "用户希望每次只收到一个小步骤。",
            ),
            _memory("mem_goal", MemoryType.ACTIVE_GOAL, "准备博士毕业和就业。"),
            _memory("mem_episode", MemoryType.EPISODIC, "上次提交论文后压力仍然很大。"),
            _memory("mem_semantic", MemoryType.SEMANTIC, "用户是博士高年级学生。"),
        ]
    )
    retriever = RepositoryMemoryRetriever(repository)

    result = await retriever.retrieve(
        user_id="user_memory",
        query="今天还是有压力",
        session_state=SessionState(session_id="session_memory"),
    )

    assert repository.user_ids == [UserId("user_memory")]
    assert result.interaction_preferences == ["用户希望每次只收到一个小步骤。"]
    assert result.active_goals == ["准备博士毕业和就业。"]
    assert [item.content for item in result.episodic_memories] == [
        "上次提交论文后压力仍然很大。"
    ]
    assert [item.content for item in result.semantic_memories] == [
        "用户是博士高年级学生。"
    ]


async def test_repository_memory_retriever_respects_limit() -> None:
    """The retriever should cap active memories before grouping."""

    repository = FakeActiveMemoryRepository(
        [
            _memory("mem_pref", MemoryType.INTERACTION_PREFERENCE, "preference"),
            _memory("mem_goal", MemoryType.ACTIVE_GOAL, "goal"),
        ]
    )
    retriever = RepositoryMemoryRetriever(repository)

    result = await retriever.retrieve(
        user_id="user_memory",
        query="hello",
        session_state=SessionState(session_id="session_memory"),
        limit=1,
    )

    assert result.interaction_preferences == ["preference"]
    assert result.active_goals == []

class FakeOptimizedMemoryRepository:
    """Repository stub exposing SQL-limited active and pending retrieval."""

    def __init__(
        self,
        memories: list[LongTermMemory],
        pending_memories: list[LongTermMemory],
        pending_count: int,
    ) -> None:
        self.memories = memories
        self.pending_memories = pending_memories
        self.pending_count = pending_count
        self.limited_calls: list[tuple[UserId, int]] = []
        self.pending_calls: list[tuple[UserId, int]] = []

    async def list_active(self, user_id: UserId) -> list[LongTermMemory]:
        raise AssertionError(f"unlimited retrieval used for {user_id}")

    async def list_active_limited(
        self,
        user_id: UserId,
        limit: int,
    ) -> list[LongTermMemory]:
        self.limited_calls.append((user_id, limit))
        return self.memories[:limit]

    async def list_pending_confirmation_page(
        self,
        user_id: UserId,
        limit: int,
    ) -> tuple[list[LongTermMemory], int]:
        self.pending_calls.append((user_id, limit))
        return self.pending_memories[:limit], self.pending_count

    async def count_pending_confirmation(self, user_id: UserId) -> int:
        raise AssertionError(f"separate pending count used for {user_id}")


async def test_optimized_retrieval_reads_pending_on_every_turn_and_filters_relevance() -> None:
    repository = FakeOptimizedMemoryRepository(
        [
            _memory("mem_pref", MemoryType.INTERACTION_PREFERENCE, "pref"),
            _memory("mem_goal", MemoryType.ACTIVE_GOAL, "goal"),
        ],
        [
            _memory("mem_pending_festival", MemoryType.EPISODIC, "华晨宇烟台音乐节待确认。"),
            _memory("mem_pending_move", MemoryType.SEMANTIC, "明年搬去上海待确认。"),
        ],
        pending_count=2,
    )
    retriever = RepositoryMemoryRetriever(repository)

    ordinary = await retriever.retrieve(
        user_id="user_memory",
        query="音乐节让我有点累。",
        session_state=SessionState(session_id="session_memory"),
        limit=1,
    )
    memory_query = await retriever.retrieve(
        user_id="user_memory",
        query="我的长期记忆里面有什么？",
        session_state=SessionState(session_id="session_memory"),
        limit=2,
    )

    assert repository.limited_calls == [
        (UserId("user_memory"), 1),
        (UserId("user_memory"), 2),
    ]
    assert repository.pending_calls == [
        (UserId("user_memory"), 2),
        (UserId("user_memory"), 4),
    ]
    assert [str(item.id) for item in ordinary.active_memories] == ["mem_pref"]
    assert ordinary.pending_confirmation_count == 2
    assert [str(item.id) for item in ordinary.pending_confirmation_memories] == [
        "mem_pending_festival"
    ]
    assert [str(item.id) for item in memory_query.active_memories] == [
        "mem_pref",
        "mem_goal",
    ]
    assert [
        str(item.id) for item in memory_query.pending_confirmation_memories
    ] == ["mem_pending_festival", "mem_pending_move"]
    assert memory_query.pending_confirmation_count == 2
    assert memory_query.interaction_preferences == ["pref"]
    assert memory_query.active_goals == ["goal"]
    serialized = memory_query.model_dump()
    assert "active_memories" not in serialized
    assert "pending_confirmation_memories" in serialized
