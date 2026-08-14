"""OpenAI-compatible asynchronous language-model client."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, TypeVar, cast

import httpx
from pydantic import BaseModel

from llm.exceptions import (
    LLMHTTPError,
    LLMRetryExhaustedError,
    LLMStructuredOutputError,
    LLMTimeoutError,
)
from llm.model_registry import ModelConfig, ModelRegistry
from llm.retry_policy import RetryPolicy
from llm.structured_output import ModelT, parse_structured_output
from schemas.llm import (
    LLMMessage,
    LLMMessageRole,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)

ReturnT = TypeVar("ReturnT")


class LLMClientProtocol(Protocol):
    """Minimal model-client contract consumed by response generation."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate one normalized model response."""


class LLMClient:
    """Async OpenAI-compatible client with routing, timeout, retry, and validation."""

    def __init__(
        self,
        registry: ModelRegistry,
        *,
        retry_policy: RetryPolicy | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create a client with injectable registry, retry policy, and HTTP transport."""

        self._registry = registry
        self._retry_policy = retry_policy or RetryPolicy()
        self._http_client = http_client

    async def __aenter__(self) -> LLMClient:
        """Return this client for use in an async context manager."""

        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Close an injected HTTP client when the caller explicitly owns this wrapper."""

        await self.aclose()

    async def aclose(self) -> None:
        """Close the injected HTTP client, if one was supplied."""

        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Send a normalized chat-completions request and return normalized output."""

        config = self._registry.get(request.model_name)
        return await self._run_with_retries(lambda: self._post_request(config, request))

    async def generate_text(
        self,
        prompt: str,
        *,
        model_name: str | None = None,
        agent_name: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 800,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        """Generate plain text while allowing model selection by agent name."""

        selected_model = self._registry.resolve_model_name(
            model_name=model_name,
            agent_name=agent_name or _metadata_agent(metadata),
        )
        request = LLMRequest(
            messages=[LLMMessage(role=LLMMessageRole.USER, content=prompt)],
            model_name=selected_model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        response = await self.generate(request)
        return response.content

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        agent_name: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1200,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Generate and validate one Pydantic structured-output object.

        Malformed JSON and schema-invalid output are retried under the same retry
        policy as transport failures. After retry exhaustion, callers receive a
        typed LLM exception and can apply their agent-specific safe fallback.
        """

        selected_model = self._registry.resolve_model_name(
            model_name=model_name,
            agent_name=agent_name or _metadata_agent(metadata),
        )
        config = self._registry.get(selected_model)
        request = LLMRequest(
            messages=[
                LLMMessage(
                    role=LLMMessageRole.USER,
                    content=_structured_prompt(prompt, output_model, metadata),
                )
            ],
            model_name=selected_model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format="json_object",
        )

        async def operation() -> ModelT:
            response = await self._post_request(config, request)
            return parse_structured_output(response.content, output_model)

        return await self._run_with_retries(operation)

    async def _run_with_retries(
        self,
        operation: Callable[[], Awaitable[ReturnT]],
    ) -> ReturnT:
        """Execute one model operation with bounded exponential-backoff retries."""

        last_error: Exception | None = None
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            try:
                return await operation()
            except LLMHTTPError as exc:
                last_error = exc
                if not self._retry_policy.should_retry_status(exc.status_code):
                    raise
            except LLMStructuredOutputError as exc:
                last_error = exc
            except LLMTimeoutError as exc:
                last_error = exc
            except httpx.TimeoutException as exc:
                last_error = LLMTimeoutError(str(exc))
            except httpx.TransportError as exc:
                last_error = exc
            except Exception:
                raise

            if attempt < self._retry_policy.max_attempts:
                await self._retry_policy.sleep_before_retry(attempt)

        if last_error is None:
            raise RuntimeError("LLM request failed without an error")
        raise LLMRetryExhaustedError(self._retry_policy.max_attempts, last_error)

    async def _post_request(
        self,
        config: ModelConfig,
        request: LLMRequest,
    ) -> LLMResponse:
        """Issue one OpenAI-compatible `/chat/completions` request."""

        payload: dict[str, object] = {
            "model": config.provider_model_name or request.model_name,
            "messages": [message.model_dump(mode="json") for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.response_format:
            payload["response_format"] = {"type": request.response_format}

        try:
            if self._http_client is not None:
                response = await self._http_client.post(
                    config.endpoint,
                    json=payload,
                    headers=config.resolved_headers(),
                    timeout=config.timeout_seconds,
                )
            else:
                async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
                    response = await client.post(
                        config.endpoint,
                        json=payload,
                        headers=config.resolved_headers(),
                    )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(str(exc)) from exc

        if response.status_code >= 400:
            raise LLMHTTPError(response.status_code, response.text)
        return _normalize_response(response.json(), fallback_model=request.model_name)


def _metadata_agent(metadata: Mapping[str, object] | None) -> str | None:
    """Read a stable agent name from optional request metadata."""

    if metadata is None:
        return None
    value = metadata.get("agent")
    return value if isinstance(value, str) and value else None


def _structured_prompt(
    prompt: str,
    output_model: type[BaseModel],
    metadata: Mapping[str, object] | None,
) -> str:
    """Append a compact schema-only output instruction to an agent prompt."""

    metadata_text = "\n".join(
        f"- {key}: {value}" for key, value in (metadata or {}).items()
    ) or "- none"
    return (
        f"{prompt.strip()}\n\n"
        "Return exactly one JSON object and no markdown or explanatory prose.\n"
        f"Output model: {output_model.__name__}\n"
        f"JSON schema: {output_model.model_json_schema()}\n"
        f"Metadata:\n{metadata_text}\n"
    )


def _normalize_response(payload: object, *, fallback_model: str) -> LLMResponse:
    """Normalize common OpenAI-compatible response shapes."""

    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object")
    data = cast(dict[str, object], payload)
    content = _extract_content(data)
    model = data.get("model")
    finish_reason = _extract_finish_reason(data)
    usage = _extract_usage(data.get("usage"))
    return LLMResponse(
        content=content,
        model_name=model if isinstance(model, str) and model else fallback_model,
        finish_reason=finish_reason,
        usage=usage,
    )


def _extract_content(data: dict[str, object]) -> str:
    """Extract assistant text from standard and common local response shapes."""

    for key in ("output_text", "response", "text", "content"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value

    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            choice = cast(dict[str, object], first)
            message = choice.get("message")
            if isinstance(message, dict):
                content = cast(dict[str, object], message).get("content")
                if isinstance(content, str) and content:
                    return content
            text = choice.get("text")
            if isinstance(text, str) and text:
                return text
    raise ValueError("LLM response did not contain assistant text")


def _extract_finish_reason(data: dict[str, object]) -> str | None:
    """Extract the first completion finish reason when present."""

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    value = cast(dict[str, object], first).get("finish_reason")
    return value if isinstance(value, str) and value else None


def _extract_usage(payload: object) -> LLMUsage:
    """Normalize optional token-usage accounting."""

    if not isinstance(payload, dict):
        return LLMUsage()
    data = cast(dict[str, object], payload)
    return LLMUsage(
        prompt_tokens=_optional_nonnegative_int(data.get("prompt_tokens")),
        completion_tokens=_optional_nonnegative_int(data.get("completion_tokens")),
        total_tokens=_optional_nonnegative_int(data.get("total_tokens")),
    )


def _optional_nonnegative_int(value: object) -> int | None:
    """Return a provider token count only when it is a valid nonnegative integer."""

    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
