"""Tests for deterministic long-term-memory query handling."""

import pytest

from agents.output_guard import FakeOutputGuard, OutputGuard
from schemas.common import MemoryId, MessageId, SourceReference
from schemas.memory import (
    LongTermMemory,
    MemoryQueryIntent,
    MemorySensitivity,
    MemoryType,
    RetrievedMemories,
)
from schemas.risk import RiskLevel, RiskResult, RiskRoute
from schemas.safety import DraftResponse, GuardDecision, GuardInput, GuardResult
from schemas.state import SessionState
from services.context_builder import ContextBuilder
from services.memory_query import (
    build_memory_query_draft,
    build_memory_query_strategy,
    detect_memory_query_intent,
)


def _memory(memory_id: str, memory_type: MemoryType, content: str) -> LongTermMemory:
    return LongTermMemory(
        id=MemoryId(memory_id),
        memory_type=memory_type,
        content=content,
        sensitivity=MemorySensitivity.LOW,
        confidence=0.9,
        source=[SourceReference(message_id=MessageId(f"msg_{memory_id}"))],
    )


def _risk() -> RiskResult:
    return RiskResult(
        risk_level=RiskLevel.LOW,
        route=RiskRoute.NORMAL,
        reason_codes=["fixture_low"],
        confidence=0.95,
    )


async def _context(
    memories: RetrievedMemories,
    query: str,
    intent: MemoryQueryIntent,
):
    return await ContextBuilder().build(
        session_state=SessionState(session_id="memory-query-session"),
        rolling_summary=None,
        recent_messages=[],
        memories=memories,
        strategy=build_memory_query_strategy(intent),
        risk=_risk(),
        current_user_message=query,
        memory_query_intent=intent,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("你还记得我吗？", MemoryQueryIntent.EXISTENCE),
        ("我的长期记忆里面有什么?", MemoryQueryIntent.LIST),
        ("我之前说我抢到了音乐节门票，你还记得是谁的吗？", MemoryQueryIntent.RECALL),
        ("Do you remember me?", MemoryQueryIntent.EXISTENCE),
        ("我记得今天要买菜。", MemoryQueryIntent.NONE),
        ("今天有点累。", MemoryQueryIntent.NONE),
    ],
)
def test_detect_memory_query_intent(text: str, expected: MemoryQueryIntent) -> None:
    assert detect_memory_query_intent(text) == expected


async def test_recall_selects_related_active_memory_and_real_id() -> None:
    festival = _memory(
        "mem_festival",
        MemoryType.INTERACTION_PREFERENCE,
        "用户希望系统记住音乐节门票以及当时兴奋和期待的心情。",
    )
    work = _memory(
        "mem_work",
        MemoryType.ACTIVE_GOAL,
        "用户希望改善与直属领导的沟通。",
    )
    memories = RetrievedMemories(
        active_memories=[work, festival],
        pending_confirmation_count=2,
    )
    context = await _context(
        memories,
        "我之前说过音乐节门票，你还记得吗？",
        MemoryQueryIntent.RECALL,
    )

    draft = build_memory_query_draft(context)

    assert "音乐节" in draft.text
    assert "直属领导" not in draft.text
    assert "2 条待确认" in draft.text
    assert draft.referenced_memory_ids == ["mem_festival"]


async def test_list_query_returns_active_records_with_ids() -> None:
    preference = _memory(
        "mem_pref",
        MemoryType.INTERACTION_PREFERENCE,
        "用户希望每次只收到一个简短步骤。",
    )
    context = await _context(
        RetrievedMemories(active_memories=[preference]),
        "我的长期记忆里面有什么？",
        MemoryQueryIntent.LIST,
    )

    draft = build_memory_query_draft(context)

    assert "简短步骤" in draft.text
    assert draft.referenced_memory_ids == ["mem_pref"]


async def test_pending_only_and_empty_are_distinguished() -> None:
    pending_context = await _context(
        RetrievedMemories(pending_confirmation_count=2),
        "你还记得我吗？",
        MemoryQueryIntent.EXISTENCE,
    )
    empty_context = await _context(
        RetrievedMemories(),
        "你还记得我吗？",
        MemoryQueryIntent.EXISTENCE,
    )

    pending = build_memory_query_draft(pending_context)
    empty = build_memory_query_draft(empty_context)

    assert "2 条待确认" in pending.text
    assert "没有已生效" in pending.text
    assert "没有可用或待确认" in empty.text


async def test_guard_rewrites_false_denial_when_active_memory_exists() -> None:
    memory = _memory(
        "mem_owned",
        MemoryType.SEMANTIC,
        "用户说自己抢到了音乐节门票。",
    )
    context = await _context(
        RetrievedMemories(active_memories=[memory]),
        "你还记得我吗？",
        MemoryQueryIntent.EXISTENCE,
    )

    result = await FakeOutputGuard().review(
        GuardInput(
            draft=DraftResponse(text="我目前没有任何关于你的记录。"),
            context=context,
            risk=_risk(),
        )
    )

    assert result.decision == GuardDecision.REWRITE
    assert result.rewritten_response is not None
    assert "音乐节门票" in result.rewritten_response
    assert "memory_query_false_denial" in result.violations


async def test_guard_accepts_owned_preference_id_and_blocks_unknown_id() -> None:
    memory = _memory(
        "mem_owned_pref",
        MemoryType.INTERACTION_PREFERENCE,
        "用户希望回复简短。",
    )
    context = await _context(
        RetrievedMemories(active_memories=[memory]),
        "今天有点累。",
        MemoryQueryIntent.NONE,
    )
    guard = FakeOutputGuard()

    allowed = await guard.review(
        GuardInput(
            draft=DraftResponse(
                text="我会尽量简短。",
                referenced_memory_ids=["mem_owned_pref"],
            ),
            context=context,
            risk=_risk(),
        )
    )
    blocked = await guard.review(
        GuardInput(
            draft=DraftResponse(
                text="我会尽量简短。",
                referenced_memory_ids=["mem_other_user"],
            ),
            context=context,
            risk=_risk(),
        )
    )
    assert allowed.decision == GuardDecision.ALLOW
    assert blocked.decision == GuardDecision.BLOCK
    assert blocked.violations == ["inappropriate_memory_use"]


async def test_guard_requires_pending_memory_to_be_labeled_unconfirmed() -> None:
    pending = _memory(
        "mem_pending_festival",
        MemoryType.EPISODIC,
        "用户可能去过华晨宇烟台音乐节。",
    )
    context = await _context(
        RetrievedMemories(pending_confirmation_memories=[pending]),
        "音乐节让我有点累。",
        MemoryQueryIntent.NONE,
    )
    guard = FakeOutputGuard()

    labeled = await guard.review(
        GuardInput(
            draft=DraftResponse(
                text="我这里有一条关于音乐节的待确认记录，你可以确认一下。",
                referenced_memory_ids=["mem_pending_festival"],
            ),
            context=context,
            risk=_risk(),
        )
    )
    asserted = await guard.review(
        GuardInput(
            draft=DraftResponse(
                text="你之前去过华晨宇烟台音乐节。",
                referenced_memory_ids=["mem_pending_festival"],
            ),
            context=context,
            risk=_risk(),
        )
    )

    assert labeled.decision == GuardDecision.ALLOW
    assert asserted.decision == GuardDecision.REWRITE
    assert asserted.rewritten_response is not None
    assert "待确认" in asserted.rewritten_response
    assert "pending_memory_presented_as_confirmed" in asserted.violations

class AllowingGuardModel:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_structured(self, *args, **kwargs):
        self.calls += 1
        return GuardResult(decision=GuardDecision.ALLOW)


async def test_model_guard_runs_semantic_call_after_memory_rules_pass() -> None:
    memory = _memory(
        "mem_guard_skip",
        MemoryType.INTERACTION_PREFERENCE,
        "用户希望系统记住音乐节门票。",
    )
    context = await _context(
        RetrievedMemories(active_memories=[memory]),
        "你还记得我吗？",
        MemoryQueryIntent.EXISTENCE,
    )
    draft = build_memory_query_draft(context)
    client = AllowingGuardModel()

    result = await OutputGuard(client).review(
        GuardInput(draft=draft, context=context, risk=_risk())
    )

    assert result.decision == GuardDecision.ALLOW
    assert client.calls == 1

async def test_missing_memory_id_metadata_reaches_semantic_guard() -> None:
    memory = _memory(
        "mem_missing_metadata",
        MemoryType.EPISODIC,
        "用户说自己抢到了音乐节门票。",
    )
    context = await _context(
        RetrievedMemories(active_memories=[memory]),
        "你还记得我吗？",
        MemoryQueryIntent.EXISTENCE,
    )
    client = AllowingGuardModel()

    result = await OutputGuard(client).review(
        GuardInput(
            draft=DraftResponse(text="记得，你之前和我聊过音乐节门票。"),
            context=context,
            risk=_risk(),
        )
    )

    assert result.decision == GuardDecision.ALLOW
    assert client.calls == 1

