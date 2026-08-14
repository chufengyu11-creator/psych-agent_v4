"""Response agent implementations."""

from llm.client import LLMClient
from llm.fake_client import FakeLLMClient
from llm.prompt_renderer import PromptRenderer
from schemas.context import ResponseContext
from schemas.safety import DraftResponse
from schemas.strategy import StrategyType


class ResponseAgent:
    """Generates draft assistant replies from typed response context."""

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        prompt_renderer: PromptRenderer | None = None,
    ) -> None:
        """Create a response agent with swappable prompt and model dependencies."""

        self._llm_client = llm_client or FakeLLMClient()
        self._prompt_renderer = prompt_renderer or PromptRenderer()

    async def run(self, payload: ResponseContext) -> DraftResponse:
        """Alias for generate so the class satisfies Agent protocol."""

        return await self.generate(payload)

    async def generate(self, context: ResponseContext) -> DraftResponse:
        """Render context, call the LLM client, and normalize draft metadata."""

        request = self._prompt_renderer.render(context)
        response = await self._llm_client.generate(request)
        return DraftResponse(
            text=response.content,
            asked_question=self._asked_question(response.content, context),
            contains_action_suggestion=self._contains_action_suggestion(context),
            referenced_memory_ids=self._referenced_memory_ids(context),
        )

    def _asked_question(self, text: str, context: ResponseContext) -> bool:
        """Infer whether the draft asks the user a question."""

        return (
            "?" in text
            or "\uff1f" in text
            or context.strategy.primary_strategy == StrategyType.CLARIFICATION
        )

    def _contains_action_suggestion(self, context: ResponseContext) -> bool:
        """Infer whether the selected strategy expects an action suggestion."""

        return context.strategy.primary_strategy in {
            StrategyType.ACTION_PLANNING,
            StrategyType.COLLABORATIVE_PROBLEM_SOLVING,
        }

    def _referenced_memory_ids(self, context: ResponseContext) -> list[str]:
        """Expose retrieved memory IDs so downstream guard/logging can inspect them."""

        memories = [
            *context.memories.semantic_memories,
            *context.memories.episodic_memories,
        ]
        return [str(memory.id) for memory in memories]


class FakeResponseAgent(ResponseAgent):
    """ResponseAgent wired to the deterministic fake LLM client by default."""