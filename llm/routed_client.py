"""Route normalized LLM requests to model-specific HTTP clients."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from llm.exceptions import LLMConfigurationError
from llm.local_client import OpenAICompatibleHTTPClient
from schemas.llm import LLMRequest, LLMResponse


class RoutedLLMClient:
    """Application-owned router for multiple OpenAI-compatible model endpoints."""

    def __init__(
        self,
        clients: Mapping[str, OpenAICompatibleHTTPClient],
        *,
        default_model_name: str,
    ) -> None:
        """Bind each served model name to the HTTP client that hosts it."""

        normalized = {
            model_name.strip(): client
            for model_name, client in clients.items()
            if model_name.strip()
        }
        if not normalized:
            raise LLMConfigurationError("at least one LLM model route is required")
        default_name = default_model_name.strip()
        if not default_name:
            raise LLMConfigurationError("default routed model name is not configured")
        if default_name not in normalized:
            msg = f"default routed model is not registered: {default_name}"
            raise LLMConfigurationError(msg)
        self._clients = normalized
        self._default_model_name = default_name

    @property
    def model_name(self) -> str:
        """Return the default routed model name."""

        return self._default_model_name

    @property
    def model_names(self) -> tuple[str, ...]:
        """Return all model names that can be selected by Agent requests."""

        return tuple(self._clients)

    @property
    def request_count(self) -> int:
        """Return HTTP attempts across unique endpoint clients."""

        return sum(client.request_count for client in self._unique_clients())

    @property
    def successful_http_response_count(self) -> int:
        """Return successful HTTP responses across unique endpoint clients."""

        return sum(
            client.successful_http_response_count
            for client in self._unique_clients()
        )

    @property
    def is_closed(self) -> bool:
        """Return whether every owned endpoint client is closed."""

        return all(client.is_closed for client in self._unique_clients())

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Send one request to the endpoint registered for its model name."""

        model_name = request.model_name or self._default_model_name
        client = self._clients.get(model_name)
        if client is None:
            msg = f"no LLM endpoint route is configured for model: {model_name}"
            raise LLMConfigurationError(msg)
        return await client.generate(request)

    async def list_models(self, *, retry: bool = True) -> list[str]:
        """Return the union of model IDs reported by every unique endpoint."""

        discovered = await asyncio.gather(
            *(client.list_models(retry=retry) for client in self._unique_clients())
        )
        return list(dict.fromkeys(model for group in discovered for model in group))

    async def missing_model_names(
        self,
        model_names: tuple[str, ...],
        *,
        retry: bool = True,
    ) -> list[str]:
        """Check every required model against its specifically assigned endpoint."""

        required = tuple(dict.fromkeys(model_names))
        routed_clients = {
            model_name: self._clients.get(model_name)
            for model_name in required
        }
        unique_clients = tuple(
            dict.fromkeys(
                client for client in routed_clients.values() if client is not None
            )
        )
        discovered_groups = await asyncio.gather(
            *(client.list_models(retry=retry) for client in unique_clients)
        )
        discovered_by_client = {
            client: set(discovered)
            for client, discovered in zip(
                unique_clients,
                discovered_groups,
                strict=True,
            )
        }
        return [
            model_name
            for model_name, client in routed_clients.items()
            if client is None or model_name not in discovered_by_client[client]
        ]

    async def aclose(self) -> None:
        """Close every unique endpoint pool; repeated calls are safe."""

        await asyncio.gather(*(client.aclose() for client in self._unique_clients()))

    def __repr__(self) -> str:
        """Return routing diagnostics without endpoint URLs or credentials."""

        return (
            f"{type(self).__name__}(models={self.model_names!r}, "
            f"endpoint_count={len(self._unique_clients())}, "
            f"closed={self.is_closed})"
        )

    def _unique_clients(self) -> tuple[OpenAICompatibleHTTPClient, ...]:
        """Deduplicate shared clients while preserving registration order."""

        return tuple(dict.fromkeys(self._clients.values()))


__all__ = ["RoutedLLMClient"]
