"""Shared asynchronous LLM client."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TypeVar

import httpx

from llm.exceptions import (
    LLMHTTPError,
    LLMRetryExhaustedError,
    LLMStructuredOutputError,
    LLMTimeoutError,
)
from llm.model_registry import ModelConfig, ModelRegistry
from llm.retry_policy import RetryPolicy
from llm.structured_output import ModelT, parse_structured_output

ReturnT = TypeVar("ReturnT")


class LLMClient:
    """Thin HTTP client that returns raw text or typed structured output."""

    def __init__(
        self,
        registry: ModelRegistry,
        *,
        retry_policy: RetryPolicy | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._registry = registry
        self._retry_policy = retry_policy or RetryPolicy()
        self._http_client = http_client
        self._owns_http_client = http_client is None

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally owned HTTP client."""

        if self._http_client is not None and self._owns_http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def generate_text(
        self,
        prompt: str,
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        """Call a model endpoint and return assistant text."""

        config = self._registry.get(model_name)
        return await self._run_with_retries(
            lambda: self._post_prompt(config=config, prompt=prompt, metadata=metadata)
        )

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Call a model endpoint and parse the response into a Pydantic model."""

        config = self._registry.get(model_name)

        async def attempt() -> ModelT:
            text = await self._post_prompt(config=config, prompt=prompt, metadata=metadata)
            return parse_structured_output(text, output_model)

        return await self._run_with_retries(attempt)

    async def _run_with_retries(self, operation: Callable[[], Awaitable[ReturnT]]) -> ReturnT:
        last_error: Exception | None = None
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            try:
                return await operation()
            except LLMHTTPError as exc:
                last_error = exc
                if not self._retry_policy.should_retry_status(exc.status_code):
                    raise
            except httpx.TimeoutException as exc:
                last_error = LLMTimeoutError(str(exc))
            except Exception as exc:
                last_error = exc
                if not self._retry_policy.should_retry_exception(exc):
                    if not isinstance(exc, LLMStructuredOutputError):
                        raise

            if attempt < self._retry_policy.max_attempts:
                await self._retry_policy.sleep_before_retry(attempt)

        if isinstance(last_error, LLMTimeoutError):
            raise last_error
        if last_error is None:
            msg = "LLM request failed without an error"
            raise RuntimeError(msg)
        raise LLMRetryExhaustedError(self._retry_policy.max_attempts, last_error)

    async def _post_prompt(
        self,
        *,
        config: ModelConfig,
        prompt: str,
        metadata: Mapping[str, object] | None,
    ) -> str:
        client = self._get_http_client(config)
        response = await client.post(
            config.endpoint,
            json={"model": config.name, "prompt": prompt, "metadata": dict(metadata or {})},
            headers=config.resolved_headers(),
        )
        if response.status_code >= 400:
            raise LLMHTTPError(response.status_code, response.text)
        return _extract_text(response.json())

    def _get_http_client(self, config: ModelConfig) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=config.timeout_seconds)
        return self._http_client


def _extract_text(payload: object) -> str:
    """Extract assistant text from common provider response shapes."""

    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        msg = "LLM response must be a JSON object or string"
        raise ValueError(msg)

    for key in ("output_text", "text", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            text = first.get("text")
            if isinstance(text, str):
                return text
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content

    msg = "LLM response did not contain assistant text"
    raise ValueError(msg)
