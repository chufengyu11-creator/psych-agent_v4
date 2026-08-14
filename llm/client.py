"""Client protocols for language-model backends."""

from typing import Protocol

from schemas.llm import LLMRequest, LLMResponse


class LLMClient(Protocol):
    """Minimal async interface shared by fake, local, and remote model clients."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate one model response from a normalized request."""


class ManagedLLMClient(LLMClient, Protocol):
    """Process-owned LLM client contract used by runtime and health checks."""

    @property
    def model_name(self) -> str:
        """Return the default model name used when a caller omits one."""

    @property
    def request_count(self) -> int:
        """Return all HTTP attempts made by this managed client."""

    @property
    def successful_http_response_count(self) -> int:
        """Return successful HTTP responses made by this managed client."""

    @property
    def is_closed(self) -> bool:
        """Return whether all owned HTTP resources are closed."""

    async def list_models(self, *, retry: bool = True) -> list[str]:
        """Return model IDs discoverable through owned endpoints."""

    async def missing_model_names(
        self,
        model_names: tuple[str, ...],
        *,
        retry: bool = True,
    ) -> list[str]:
        """Return required model names unavailable on their assigned endpoints."""

    async def aclose(self) -> None:
        """Close all owned HTTP resources."""


__all__ = ["LLMClient", "ManagedLLMClient"]
