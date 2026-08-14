"""Prompt rendering from ResponseContext to LLMRequest."""

from schemas.context import ResponseContext
from schemas.llm import LLMMessage, LLMMessageRole, LLMRequest


class PromptRenderer:
    """Turns typed response context into chat messages for an LLMClient."""

    def __init__(
        self,
        model_name: str = "local-psych-support",
        temperature: float = 0.3,
        max_tokens: int = 800,
    ) -> None:
        """Configure model request defaults used for rendered prompts."""

        self._model_name = model_name
        self._temperature = temperature
        self._max_tokens = max_tokens

    def render(self, context: ResponseContext) -> LLMRequest:
        """Render the complete response context into a model request."""

        return LLMRequest(
            model_name=self._model_name,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            messages=[
                LLMMessage(
                    role=LLMMessageRole.SYSTEM,
                    content=context.system_policy,
                ),
                LLMMessage(
                    role=LLMMessageRole.USER,
                    content=self._render_user_prompt(context),
                ),
            ],
        )

    def _render_user_prompt(self, context: ResponseContext) -> str:
        """Render context sections as a stable, inspectable prompt."""

        section_text = "\n\n".join(
            self._render_section(section.name, section.token_budget, section.content)
            for section in context.sections
        )
        return (
            "Generate one psychological-support reply from the structured context below.\n"
            "Requirements: empathic, concrete, no diagnosis, no medication advice; "
            "if risk escalates, prioritize safety checking.\n\n"
            "# Context Sections\n"
            f"{section_text}\n\n"
            "# Response Requirements\n"
            "- Reply directly to the user's current turn.\n"
            "- Use natural language and avoid mechanically repeating field names.\n"
            "- If strategy_plan asks for clarification, ask one short question.\n"
            "- If strategy_plan asks for action, offer one low-pressure next step."
        )

    def _render_section(self, name: str, token_budget: int, content: str) -> str:
        """Render one named context section."""

        return f"## {name} (token_budget={token_budget})\n{content}"