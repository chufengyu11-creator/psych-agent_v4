"""Deterministic handling for user questions about durable memory."""

from __future__ import annotations

import re

from schemas.context import ResponseContext
from schemas.memory import LongTermMemory, MemoryQueryIntent
from schemas.safety import DraftResponse
from schemas.state import ConversationPhase
from schemas.strategy import StrategyPlan, StrategyType

_MAX_LISTED_MEMORIES = 3
_LIST_MARKERS = (
    "长期记忆里有什么",
    "长期记忆里面有什么",
    "长期记忆有什么",
    "我的长期记忆",
    "我之前说过什么",
    "以前说过什么",
    "你记得我什么",
    "what do you remember about me",
    "what have i told you",
)
_EXISTENCE_MARKERS = (
    "你还记得我吗",
    "你记得我吗",
    "还记得我吗",
    "记不记得我",
    "do you remember me",
)
_RECALL_CONTEXT_MARKERS = (
    "之前",
    "以前",
    "上次",
    "我说过",
    "我提过",
    "还记得",
    "记得是谁",
    "记得什么",
    "remember when",
    "remember what",
)
_QUERY_FRAGMENTS = (
    *_LIST_MARKERS,
    *_EXISTENCE_MARKERS,
    "你还记得",
    "你记得",
    "还记得",
    "记不记得",
    "我之前说",
    "我以前说",
    "我说过",
    "我提过",
    "是谁的吗",
    "是什么吗",
    "是什么",
    "是谁",
)
_FALSE_DENIAL_MARKERS = (
    "没有任何记忆",
    "没有任何记录",
    "没有任何关于你的记录",
    "长期记忆库中还没有存储",
    "长期记忆里没有存储",
    "目前还没有关于你的记录",
    "没有找到你之前具体说过什么",
    "我不记得你",
    "i do not remember you",
    "i don't remember you",
    "no memory of you",
)
_CJK_STOP_CHARS = frozenset("我你他她它们的是了呢吗么啊呀在有过和与及还都很就也把被这那哪什之前以前上次说提记得")
_LATIN_STOP_WORDS = frozenset(
    {
        "about",
        "before",
        "remember",
        "said",
        "that",
        "the",
        "what",
        "when",
        "you",
    }
)


def detect_memory_query_intent(text: str) -> MemoryQueryIntent:
    """Classify high-confidence memory questions without a model call."""

    normalized = _normalize(text)
    if any(marker in normalized for marker in _LIST_MARKERS):
        return MemoryQueryIntent.LIST
    if any(marker in normalized for marker in _EXISTENCE_MARKERS):
        return MemoryQueryIntent.EXISTENCE
    asks_second_person_recall = "记得" in normalized and "你" in normalized
    has_recall_context = any(
        marker in normalized for marker in _RECALL_CONTEXT_MARKERS
    )
    if asks_second_person_recall and has_recall_context:
        return MemoryQueryIntent.RECALL
    if "remember" in normalized and any(
        marker in normalized for marker in ("did i", "i told", "who", "which")
    ):
        return MemoryQueryIntent.RECALL
    return MemoryQueryIntent.NONE


def select_relevant_memories(
    memories: list[LongTermMemory],
    query: str,
    intent: MemoryQueryIntent,
    *,
    limit: int = _MAX_LISTED_MEMORIES,
) -> list[LongTermMemory]:
    """Select memories relevant to this message without changing its route."""

    if limit < 1:
        return []
    return _select_memories(memories, query, intent)[:limit]


def build_memory_query_strategy(intent: MemoryQueryIntent) -> StrategyPlan:
    """Return a deterministic strategy that answers the memory question directly."""

    return StrategyPlan(
        conversation_phase=ConversationPhase.EXPLORATION,
        primary_strategy=StrategyType.SUMMARIZATION,
        objective="answer the user's memory question from current owned active records",
        reason=f"deterministic memory query intent: {intent.value}",
        avoid=[
            "invent memories",
            "treat pending records as active facts",
            "redirect the question into emotional exploration",
        ],
        expected_signals=["user can verify or correct the recalled record"],
        switch_conditions=["user corrects a memory", "risk appears"],
    )


def build_memory_query_draft(context: ResponseContext) -> DraftResponse:
    """Build a truthful response from retrieved owned records without an LLM."""

    active = _active_memories(context)
    selected = select_relevant_memories(
        active,
        context.current_user_message,
        context.memory_query_intent,
    )
    pending_count = context.memories.pending_confirmation_count

    if selected:
        if context.memory_query_intent == MemoryQueryIntent.RECALL:
            intro = "我找到了与你这次问题相关的当前可用长期记忆："
        elif context.memory_query_intent == MemoryQueryIntent.EXISTENCE:
            intro = "记得。我当前能读取到这些长期记忆："
        else:
            intro = "我当前能读取到这些长期记忆："
        lines = [intro]
        lines.extend(f"{index}. {_display_content(memory.content)}" for index, memory in enumerate(selected, 1))
        if pending_count:
            lines.append(
                f"另外还有 {pending_count} 条待确认记录；在确认前，我不会把它们当作已生效事实。"
            )
        return DraftResponse(
            text="\n".join(lines),
            referenced_memory_ids=[str(memory.id) for memory in selected],
        )

    if active:
        text = (
            f"我目前有 {len(active)} 条可用长期记忆，但没有找到与这次问题直接匹配的记录。"
        )
        if pending_count:
            text += (
                f"另外还有 {pending_count} 条待确认记录；在确认前，我不会把它们当作已生效事实。"
            )
        return DraftResponse(text=text)

    if pending_count:
        return DraftResponse(
            text=(
                "我目前没有已生效的长期记忆，但有 "
                f"{pending_count} 条待确认记录。确认前，我不会把它们当作事实使用。"
            )
        )

    return DraftResponse(text="我目前没有可用或待确认的长期记忆记录。")


def contains_false_memory_denial(text: str) -> bool:
    """Return whether text categorically denies memory availability."""

    normalized = _normalize(text)
    return any(marker in normalized for marker in _FALSE_DENIAL_MARKERS)


def _active_memories(context: ResponseContext) -> list[LongTermMemory]:
    if context.memories.active_memories:
        return context.memories.active_memories
    fallback = (
        context.memories.semantic_memories + context.memories.episodic_memories
    )
    deduped: list[LongTermMemory] = []
    seen: set[str] = set()
    for memory in fallback:
        if str(memory.id) not in seen:
            seen.add(str(memory.id))
            deduped.append(memory)
    return deduped


def _select_memories(
    memories: list[LongTermMemory],
    query: str,
    intent: MemoryQueryIntent,
) -> list[LongTermMemory]:
    if intent in {MemoryQueryIntent.EXISTENCE, MemoryQueryIntent.LIST}:
        return memories[:_MAX_LISTED_MEMORIES]
    query_units = _topic_units(query)
    if not query_units:
        return memories[:_MAX_LISTED_MEMORIES]
    scored = [
        (len(query_units & _topic_units(memory.content)), index, memory)
        for index, memory in enumerate(memories)
    ]
    matched = [item for item in scored if item[0] > 0]
    matched.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in matched[:_MAX_LISTED_MEMORIES]]


def _topic_units(text: str) -> set[str]:
    normalized = _normalize(text)
    for fragment in _QUERY_FRAGMENTS:
        normalized = normalized.replace(fragment, "")
    cjk_chars = [
        char
        for char in normalized
        if "\u4e00" <= char <= "\u9fff" and char not in _CJK_STOP_CHARS
    ]
    units = {
        "".join(cjk_chars[index : index + 2])
        for index in range(max(0, len(cjk_chars) - 1))
    }
    units.update(
        token
        for token in re.findall(r"[a-z0-9]+", normalized)
        if len(token) >= 3 and token not in _LATIN_STOP_WORDS
    )
    return units


def _display_content(content: str) -> str:
    compact = " ".join(content.split())
    return compact if len(compact) <= 180 else f"{compact[:177]}..."


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


__all__ = [
    "build_memory_query_draft",
    "build_memory_query_strategy",
    "contains_false_memory_denial",
    "detect_memory_query_intent",
    "select_relevant_memories",
]
