"""Response agent implementations."""

from __future__ import annotations

from pathlib import Path

from llm.client import LLMClient
from schemas.context import ResponseContext
from schemas.safety import DraftResponse
from schemas.strategy import StrategyType

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
        text = "我听到你现在有些不容易。我们可以先把最困扰你的部分慢慢说清楚。"
        return DraftResponse(text=text, asked_question=False)


class ResponseAgent:
    """Model-backed response drafter that returns a typed DraftResponse."""

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        model_name: str | None = None,
        prompt_template: str | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._model_name = model_name
        self._prompt_template = prompt_template or _PROMPT_PATH.read_text(encoding="utf-8")

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

        if not isinstance(draft, DraftResponse):
            return _fallback_draft(context)
        return _normalize_metadata(draft, context)

    def _build_prompt(self, context: ResponseContext) -> str:
        rolling_summary_json = (
            context.rolling_summary.model_dump_json() if context.rolling_summary else "null"
        )
        return (
            f"{self._prompt_template}\n\n"
            "## Runtime Input\n"
            "system_policy:\n"
            f"{context.system_policy}\n\n"
            "risk_json:\n"
            f"{context.risk.model_dump_json()}\n\n"
            "session_state_json:\n"
            f"{context.session_state.model_dump_json()}\n\n"
            "strategy_json:\n"
            f"{context.strategy.model_dump_json()}\n\n"
            "rolling_summary_json:\n"
            f"{rolling_summary_json}\n\n"
            "memories_json:\n"
            f"{context.memories.model_dump_json()}\n\n"
            "recent_messages_json:\n"
            f"{[message.model_dump(mode='json') for message in context.recent_messages]}\n\n"
            "context_sections_json:\n"
            f"{[section.model_dump(mode='json') for section in context.sections]}\n"
        )


def _fallback_draft(context: ResponseContext) -> DraftResponse:
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
    text = "我听到这件事对你并不轻松。我们可以先从最困扰你的那一部分开始说起。"
    return _normalize_metadata(DraftResponse(text=text), context)


def _normalize_metadata(draft: DraftResponse, context: ResponseContext) -> DraftResponse:
    return DraftResponse(
        text=draft.text,
        asked_question=draft.asked_question or _looks_like_question(draft.text),
        contains_action_suggestion=(
            draft.contains_action_suggestion or _looks_like_action_suggestion(draft.text)
        ),
        referenced_memory_ids=_filter_known_memory_ids(draft.referenced_memory_ids, context),
    )


def _looks_like_question(text: str) -> bool:
    return any(marker in text for marker in _QUESTION_MARKERS)


def _looks_like_action_suggestion(text: str) -> bool:
    lower_text = text.lower()
    return any(marker in lower_text for marker in _ACTION_MARKERS)


def _filter_known_memory_ids(memory_ids: list[str], context: ResponseContext) -> list[str]:
    known_ids = {
        str(memory.id)
        for memory in context.memories.semantic_memories + context.memories.episodic_memories
    }
    filtered: list[str] = []
    for memory_id in memory_ids:
        if memory_id in known_ids and memory_id not in filtered:
            filtered.append(memory_id)
    return filtered
