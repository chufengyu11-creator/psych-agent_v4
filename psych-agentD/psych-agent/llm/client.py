"""Client protocols for language-model backends."""

from typing import Protocol

from schemas.llm import LLMRequest, LLMResponse


class LLMClient(Protocol):
    """Minimal async interface shared by fake, local, and remote model clients."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate one model response from a normalized request."""