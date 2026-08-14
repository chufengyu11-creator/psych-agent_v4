"""Response agent implementations."""

from __future__ import annotations

import json
from pathlib import Path

from llm.structured_client import StructuredLLMClientProtocol
from schemas.context import ResponseContext
from schemas.safety import DraftResponse
from schemas.memory import MemoryQueryIntent
from schemas.strategy import StrategyType
from services.memory_query import build_memory_query_draft

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "response_agent.md"

_QUESTION_MARKERS = ("?", "？", "吗", "么", "哪一个", "哪个", "是否", "能不能", "要不要")
_ACTION_MARKERS = (
    "下一步",
    "先",
    "试试",
    "可以把",
    "建议",
    "行动",
    "计划",
    "步骤",
    "step",
    "try",
    "plan",
)


class FakeResponseAgent:
    """Template-based response generator for integration testing."""

    async def run(self, payload: ResponseContext) -> DraftResponse:
        """Alias for generate so the class satisfies Agent protocol."""

        return await self.generate(payload)

    async def generate(self, context: ResponseContext) -> DraftResponse:
        """Generate a safe, minimal support response from typed context."""

        if context.strategy.primary_strategy == StrategyType.CLARIFICATION:
            text = (
                "我听见你不太想沿用刚才的方向。我们先校准一下："
                "你更想要具体步骤、情绪梳理，还是只需要我陪你把话说完？"
            )
            return DraftResponse(text=text, asked_question=True)
        if context.strategy.primary_strategy == StrategyType.COLLABORATIVE_PROBLEM_SOLVING:
            text = "听起来你想要更具体一点的下一步。我们可以先选一个很小、压力最低的行动来试试。"
            return DraftResponse(
                text=text,
                asked_question=False,
                contains_action_suggestion=True,
            )
        if context.strategy.primary_strategy == StrategyType.SAFETY_CHECK:
            return DraftResponse(
                text="我想先确认你的安全状况：你现在有立即伤害自己或他人的风险吗？",
                asked_question=True,
            )
        text = "我听到你现在有些不容易。我们可以先把最困扰你的部分慢慢说清楚。"
        return DraftResponse(text=text, asked_question=False)


class ResponseAgent:
    """Model-backed response drafter that returns a typed DraftResponse."""

    def __init__(
        self,
        llm_client: StructuredLLMClientProtocol,
        *,
        model_name: str | None = None,
        prompt_template: str | None = None,
    ) -> None:
        """Create a response agent with an injectable structured model client."""

        self._llm_client = llm_client
        self._model_name = model_name
        self._prompt_template = prompt_template or _read_prompt(
            _PROMPT_PATH,
            fallback=(
                "Draft one brief, supportive psychological-support response as "
                "DraftResponse JSON. Follow the selected strategy and do not diagnose."
            ),
        )

    async def run(self, payload: ResponseContext) -> DraftResponse:
        """Alias for generate so the class satisfies Agent protocol."""

        return await self.generate(payload)

    async def generate(self, context: ResponseContext) -> DraftResponse:
        """Draft a natural-language response from the prepared context."""

        try:
            draft = await self._llm_client.generate_structured(
                self._build_prompt(context),
                DraftResponse,
                model_name=self._model_name,
                metadata={"agent": "response_agent"},
            )
        except Exception:
            return _fallback_draft(context)
        return _normalize_metadata(draft, context)

    def _build_prompt(self, context: ResponseContext) -> str:
        """Render the complete typed response context for structured drafting."""

        recent_messages_json = json.dumps(
            [message.model_dump(mode="json") for message in context.recent_messages],
            ensure_ascii=False,
        )
        context_sections_json = json.dumps(
            [section.model_dump(mode="json") for section in context.sections],
            ensure_ascii=False,
        )
        return (
            f"{self._prompt_template}\n\n"
            "## Runtime Input\n"
            "current_user_message:\n"
            f"{context.current_user_message}\n\n"
            "memory_query_intent:\n"
            f"{context.memory_query_intent.value}\n\n"
            "system_policy:\n"
            f"{context.system_policy}\n\n"
            "risk_json:\n"
            f"{context.risk.model_dump_json()}\n\n"
            "session_state_json:\n"
            f"{context.session_state.model_dump_json()}\n\n"
            "strategy_json:\n"
            f"{context.strategy.model_dump_json()}\n\n"
            "rolling_summary_json:\n"
            f"{_json_or_null(context.rolling_summary)}\n\n"
            "memories_json:\n"
            f"{context.memories.model_dump_json()}\n\n"
            "reviewed_knowledge_json:\n"
            f"{context.knowledge.model_dump_json()}\n\n"
            "knowledge_reference_rule:\n"
            "If the draft relies on reviewed knowledge, include only supplied chunk_id values in referenced_knowledge_ids.\n\n"
            "recent_messages_json:\n"
            f"{recent_messages_json}\n\n"
            "context_sections_json:\n"
            f"{context_sections_json}\n"
        )


def _read_prompt(path: Path, *, fallback: str) -> str:
    """Read an optional prompt file, falling back when it is absent or empty."""

    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return fallback


def _json_or_null(value: object) -> str:
    """Return a model JSON payload for Pydantic objects or null."""

    if value is None:
        return "null"
    model_dump_json = getattr(value, "model_dump_json", None)
    if callable(model_dump_json):
        return str(model_dump_json())
    return json.dumps(value, ensure_ascii=False)


def _fallback_draft(context: ResponseContext) -> DraftResponse:
    """Return a deterministic low-risk draft when response generation fails."""

    if context.memory_query_intent != MemoryQueryIntent.NONE:
        return _normalize_metadata(build_memory_query_draft(context), context)
    strategy = context.strategy.primary_strategy
    if strategy == StrategyType.CLARIFICATION:
        text = (
            "我想先确认一下方向：你更希望我先帮你梳理感受，"
            "还是先一起找一个具体的下一步？"
        )
        return _normalize_metadata(DraftResponse(text=text), context)
    if strategy in {StrategyType.COLLABORATIVE_PROBLEM_SOLVING, StrategyType.ACTION_PLANNING}:
        text = "我们可以先把目标缩小到一个很小的下一步，选择一个今天压力最低、也最容易尝试的动作。"
        return _normalize_metadata(DraftResponse(text=text), context)
    if strategy == StrategyType.SAFETY_CHECK:
        text = "我想先确认你的安全状况：你现在有立即伤害自己或他人的风险吗？"
        return _normalize_metadata(DraftResponse(text=text), context)
    text = "我听到这件事对你并不轻松。我们可以先从最困扰你的那一部分开始说起。"
    return _normalize_metadata(DraftResponse(text=text), context)


def _normalize_metadata(draft: DraftResponse, context: ResponseContext) -> DraftResponse:
    """Trust the text, but normalize metadata to local context and safe heuristics."""

    text = draft.text.strip()
    if not text:
        return _fallback_draft(context)
    return DraftResponse(
        text=text,
        asked_question=draft.asked_question or _looks_like_question(text),
        contains_action_suggestion=(
            context.strategy.primary_strategy
            in {StrategyType.ACTION_PLANNING, StrategyType.COLLABORATIVE_PROBLEM_SOLVING}
            or (
                context.strategy.primary_strategy != StrategyType.CLARIFICATION
                and (draft.contains_action_suggestion or _looks_like_action_suggestion(text))
            )
        ),
        referenced_memory_ids=_filter_known_memory_ids(draft.referenced_memory_ids, context),
        referenced_knowledge_ids=_filter_known_knowledge_ids(
            draft.referenced_knowledge_ids,
            context,
        ),
    )


def _looks_like_question(text: str) -> bool:
    """Return whether draft text appears to ask the user a question."""

    return any(marker in text for marker in _QUESTION_MARKERS)


def _looks_like_action_suggestion(text: str) -> bool:
    """Return whether draft text appears to contain a concrete action suggestion."""

    lower_text = text.lower()
    return any(marker in lower_text for marker in _ACTION_MARKERS)


def _filter_known_memory_ids(memory_ids: list[str], context: ResponseContext) -> list[str]:
    """Keep only retrieved memory IDs that downstream guard can verify."""

    active_memories = context.memories.active_memories or (
        context.memories.semantic_memories + context.memories.episodic_memories
    )
    retrieved_memories = (
        active_memories + context.memories.pending_confirmation_memories
    )
    known_ids = {
        str(memory.id)
        for memory in retrieved_memories
    }
    filtered: list[str] = []
    for memory_id in memory_ids:
        if memory_id in known_ids and memory_id not in filtered:
            filtered.append(memory_id)
    return filtered


def _filter_known_knowledge_ids(
    knowledge_ids: list[str], context: ResponseContext
) -> list[str]:
    """Keep only knowledge chunk IDs supplied to this turn."""

    known_ids = {evidence.chunk_id for evidence in context.knowledge.evidences}
    return list(dict.fromkeys(item for item in knowledge_ids if item in known_ids))


__all__ = ["FakeResponseAgent", "ResponseAgent"]
