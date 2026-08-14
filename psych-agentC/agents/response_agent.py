"""Response-agent implementations."""

from pathlib import Path

from llm.client import LLMClientProtocol
from llm.fake_client import FakeLLMClient
from llm.prompt_renderer import PromptRenderer
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


class ResponseAgent:
    """Generate a draft response from prepared context without mutating state."""

    def __init__(
        self,
        llm_client: LLMClientProtocol | None = None,
        prompt_renderer: PromptRenderer | None = None,
        *,
        model_name: str = "local-psych-support",
        prompt_template: str | None = None,
    ) -> None:
        """Create a response agent with replaceable model and renderer dependencies."""

        self._llm_client = llm_client or FakeLLMClient()
        template = prompt_template or _read_prompt(
            _PROMPT_PATH,
            fallback=(
                "Draft one brief, supportive psychological-support response. "
                "Follow the selected strategy and do not diagnose or give medication advice."
            ),
        )
        self._prompt_renderer = prompt_renderer or PromptRenderer(
            model_name=model_name,
            instruction_template=template,
        )

    async def run(self, payload: ResponseContext) -> DraftResponse:
        """Alias for generate so the class satisfies the generic Agent protocol."""

        return await self.generate(payload)

    async def generate(self, context: ResponseContext) -> DraftResponse:
        """Render context, call the model, and return a typed safe draft fallback on failure."""

        try:
            request = self._prompt_renderer.render(context)
            response = await self._llm_client.generate(request)
            text = response.content.strip()
            if not text:
                return _fallback_draft(context)
            return _normalize_draft(DraftResponse(text=text), context)
        except Exception:
            return _fallback_draft(context)


class FakeResponseAgent(ResponseAgent):
    """ResponseAgent wired to the deterministic fake model by default."""


def _read_prompt(path: Path, *, fallback: str) -> str:
    """Read an optional prompt file, using a safe fallback when absent or empty."""

    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return fallback


def _fallback_draft(context: ResponseContext) -> DraftResponse:
    """Return a deterministic low-risk draft when response generation fails."""

    strategy = context.strategy.primary_strategy
    if strategy == StrategyType.CLARIFICATION:
        return _normalize_draft(
            DraftResponse(
                text=(
                    "我想先确认一下方向：你更希望我先帮你梳理感受，"
                    "还是先一起找一个具体的下一步？"
                )
            ),
            context,
        )
    if strategy in {
        StrategyType.COLLABORATIVE_PROBLEM_SOLVING,
        StrategyType.ACTION_PLANNING,
    }:
        return _normalize_draft(
            DraftResponse(
                text=(
                    "我们可以先把目标缩小到一个很小的下一步，"
                    "选择一个今天压力最低、也最容易尝试的动作。"
                )
            ),
            context,
        )
    if strategy == StrategyType.SAFETY_CHECK:
        return _normalize_draft(
            DraftResponse(text="我想先确认你的安全状况：你现在有立即伤害自己或他人的风险吗？"),
            context,
        )
    return _normalize_draft(
        DraftResponse(text="我听到这件事对你并不轻松。我们可以先从最困扰你的部分慢慢说起。"),
        context,
    )


def _normalize_draft(draft: DraftResponse, context: ResponseContext) -> DraftResponse:
    """Normalize metadata without trusting model-generated flags or unknown memory IDs."""

    text = draft.text.strip()
    return DraftResponse(
        text=text,
        asked_question=_looks_like_question(text)
        or context.strategy.primary_strategy == StrategyType.CLARIFICATION,
        contains_action_suggestion=(
            context.strategy.primary_strategy
            in {StrategyType.ACTION_PLANNING, StrategyType.COLLABORATIVE_PROBLEM_SOLVING}
            or (
                context.strategy.primary_strategy != StrategyType.CLARIFICATION
                and _looks_like_action_suggestion(text)
            )
        ),
        referenced_memory_ids=_known_memory_ids(context),
    )


def _looks_like_question(text: str) -> bool:
    """Return whether draft text appears to ask the user a question."""

    return any(marker in text for marker in _QUESTION_MARKERS)


def _looks_like_action_suggestion(text: str) -> bool:
    """Return whether draft text appears to contain a concrete action suggestion."""

    lower_text = text.lower()
    return any(marker in lower_text for marker in _ACTION_MARKERS)


def _known_memory_ids(context: ResponseContext) -> list[str]:
    """Return only memory IDs supplied by ContextBuilder for downstream auditing."""

    memories = [
        *context.memories.semantic_memories,
        *context.memories.episodic_memories,
    ]
    return list(dict.fromkeys(str(memory.id) for memory in memories))
