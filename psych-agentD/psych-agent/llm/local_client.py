"""Provider-agnostic HTTP client for OpenAI-compatible model services."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

import httpx
from anyio import EndOfStream
from pydantic import SecretStr

from app.config import LLMThinkingMode
from llm.exceptions import (
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMConfigurationError,
    LLMConnectionError,
    LLMError,
    LLMHTTPError,
    LLMNotFoundError,
    LLMOutputTruncatedError,
    LLMPermissionError,
    LLMProtocolError,
    LLMRateLimitError,
    LLMRetryExhaustedError,
    LLMServerError,
    LLMTimeoutError,
)
from llm.retry_policy import RetryPolicy
from schemas.llm import LLMRequest, LLMResponse, LLMUsage

if TYPE_CHECKING:
    from app.config import Settings


class OpenAICompatibleHTTPClient:
    """OpenAI-compatible client usable with hosted APIs or local vLLM servers."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: SecretStr | str,
        model_name: str = "local-psych-support",
        chat_path: str = "chat/completions",
        models_path: str = "models",
        timeout_seconds: float = 60.0,
        thinking_mode: LLMThinkingMode = "omit",
        retry_policy: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Configure one OpenAI-compatible API root without exposing its secret."""

        self._base_url = _normalize_base_url(base_url)
        self._api_key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)
        self._model_name = _require_value(model_name, "model name")
        self._chat_path = _normalize_endpoint_path(self._base_url, chat_path)
        self._models_path = _normalize_endpoint_path(self._base_url, models_path)
        if timeout_seconds <= 0:
            msg = "LLM timeout must be greater than zero"
            raise LLMConfigurationError(msg)
        if thinking_mode not in {"omit", "enabled", "disabled"}:
            msg = "LLM thinking mode must be omit, enabled, or disabled"
            raise LLMConfigurationError(msg)
        _require_api_key(self._api_key)
        self._timeout_seconds = timeout_seconds
        self._thinking_mode = thinking_mode
        self._retry_policy = retry_policy or RetryPolicy()
        self._transport = transport
        self._request_count = 0
        self._successful_http_response_count = 0
        self._client = httpx.AsyncClient(
            base_url=f"{self._base_url}/",
            headers=self._headers(),
            timeout=self._timeout_seconds,
            transport=self._transport,
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        model_name: str | None = None,
        timeout_seconds: float | None = None,
        retry_policy: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> OpenAICompatibleHTTPClient:
        """Build the formal HTTP client from shared application settings."""

        return cls(
            settings.llm_base_url,
            api_key=settings.llm_api_key,
            model_name=model_name or settings.structured_model_name,
            timeout_seconds=(
                settings.llm_timeout_seconds
                if timeout_seconds is None
                else timeout_seconds
            ),
            thinking_mode=settings.llm_thinking_mode,
            retry_policy=retry_policy,
            transport=transport,
        )

    @property
    def base_url(self) -> str:
        """Return the normalized, non-secret API root."""

        return self._base_url

    @property
    def model_name(self) -> str:
        """Return the fallback served model name."""

        return self._model_name

    @property
    def request_count(self) -> int:
        """Return the number of HTTP attempts, including retries."""

        return self._request_count

    @property
    def successful_http_response_count(self) -> int:
        """Return how many attempts received a successful HTTP status."""

        return self._successful_http_response_count

    @property
    def is_closed(self) -> bool:
        """Return whether the application-owned HTTP pool is closed."""

        return self._client.is_closed

    async def aclose(self) -> None:
        """Close the shared HTTP connection pool; repeated calls are safe."""

        if not self._client.is_closed:
            await self._client.aclose()

    def __repr__(self) -> str:
        """Return diagnostics with the API key permanently redacted."""

        return (
            f"{type(self).__name__}(base_url={self._base_url!r}, "
            f"model_name={self._model_name!r}, timeout_seconds={self._timeout_seconds!r}, "
            f"thinking_mode={self._thinking_mode!r}, api_key=**********)"
        )

    async def list_models(self, *, retry: bool = True) -> list[str]:
        """Return model IDs from the OpenAI-compatible GET /models endpoint."""

        response_data = (
            await self._request_json("GET", self._models_path)
            if retry
            else await self._request_json_once(
                "GET",
                self._models_path,
                payload=None,
            )
        )
        models = response_data.get("data")
        if not isinstance(models, list):
            msg = "models response field 'data' must be a list"
            raise LLMProtocolError(msg)

        model_ids: list[str] = []
        for item in models:
            if not isinstance(item, dict):
                msg = "models response entries must be JSON objects"
                raise LLMProtocolError(msg)
            model_data = cast(dict[str, object], item)
            model_id = model_data.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                msg = "models response entries must contain a non-empty id"
                raise LLMProtocolError(msg)
            model_ids.append(model_id)
        return model_ids

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Send a non-streaming chat completion and return final content only."""

        payload: dict[str, object] = {
            "model": request.model_name or self._model_name,
            "messages": [message.model_dump(mode="json") for message in request.messages],
            "stream": False,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.response_format is not None:
            payload["response_format"] = {"type": request.response_format}
        if self._thinking_mode != "omit":
            payload["thinking"] = {"type": self._thinking_mode}

        response_data = await self._request_json(
            "POST",
            self._chat_path,
            payload=payload,
        )
        return self._parse_chat_completion(response_data, request)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Perform one request with bounded retries and sanitized exceptions."""

        for attempt in range(1, self._retry_policy.max_attempts + 1):
            try:
                return await self._request_json_once(method, path, payload=payload)
            except LLMError as exc:
                should_retry = self._retry_policy.should_retry_exception(exc)
                if not should_retry:
                    raise
                if attempt >= self._retry_policy.max_attempts:
                    if attempt == 1:
                        raise
                    raise LLMRetryExhaustedError(attempt, exc) from exc
                await self._retry_policy.sleep_before_retry(attempt)

        msg = "LLM retry loop ended unexpectedly"
        raise LLMProtocolError(msg)

    async def _request_json_once(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None,
    ) -> dict[str, object]:
        """Perform one provider request and parse its top-level JSON object."""

        self._request_count += 1
        if self._client.is_closed:
            msg = "LLM HTTP client is closed"
            raise LLMConfigurationError(msg)
        try:
            if payload is None:
                response = await self._client.request(method, path)
            else:
                response = await self._client.request(method, path, json=payload)
        except httpx.TimeoutException as exc:
            msg = "LLM provider request timed out"
            raise LLMTimeoutError(msg) from exc
        except (httpx.TransportError, EndOfStream) as exc:
            msg = "could not connect to the LLM provider"
            raise LLMConnectionError(msg) from exc

        if response.is_error:
            raise _http_error(response.status_code)
        self._successful_http_response_count += 1

        try:
            data: object = response.json()
        except ValueError as exc:
            msg = "LLM provider response was not valid JSON"
            raise LLMProtocolError(msg) from exc
        if not isinstance(data, dict):
            msg = "LLM provider response must be a JSON object"
            raise LLMProtocolError(msg)
        return cast(dict[str, object], data)

    def _headers(self) -> dict[str, str]:
        """Build request headers without exposing them through public diagnostics."""

        return {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _parse_chat_completion(
        self,
        data: dict[str, object],
        request: LLMRequest,
    ) -> LLMResponse:
        """Validate the standard choices/message/content response shape."""

        choices = data.get("choices")
        if not isinstance(choices, list):
            msg = "chat completion field 'choices' must be a list"
            raise LLMProtocolError(msg)
        if not choices:
            msg = "chat completion choices must not be empty"
            raise LLMProtocolError(msg)

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            msg = "chat completion choice must be a JSON object"
            raise LLMProtocolError(msg)
        choice_data = cast(dict[str, object], first_choice)

        finish_reason = choice_data.get("finish_reason")
        if finish_reason == "length":
            msg = "chat completion was truncated because finish_reason=length"
            raise LLMOutputTruncatedError(msg)
        if finish_reason is not None and not isinstance(finish_reason, str):
            msg = "chat completion finish_reason must be a string or null"
            raise LLMProtocolError(msg)

        message = choice_data.get("message")
        if not isinstance(message, dict):
            msg = "chat completion choice did not include a message object"
            raise LLMProtocolError(msg)
        message_data = cast(dict[str, object], message)
        content = message_data.get("content")
        if content is None:
            msg = "chat completion message content was null"
            raise LLMProtocolError(msg)
        if not isinstance(content, str):
            msg = "chat completion message content must be a string"
            raise LLMProtocolError(msg)
        if not content.strip():
            msg = "chat completion message content was empty"
            raise LLMProtocolError(msg)

        model = data.get("model")
        model_name = model if isinstance(model, str) and model.strip() else request.model_name
        return LLMResponse(
            content=content,
            model_name=model_name,
            finish_reason=finish_reason,
            usage=_parse_usage(data.get("usage")),
        )


def _normalize_base_url(base_url: str) -> str:
    """Normalize a provider API root while preserving an existing /v1 path."""

    value = _require_value(base_url, "LLM base URL").rstrip("/")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        msg = "LLM base URL is invalid"
        raise LLMConfigurationError(msg) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        msg = "LLM base URL must be an absolute HTTP or HTTPS URL"
        raise LLMConfigurationError(msg)
    if parsed.username is not None or parsed.password is not None:
        msg = "LLM base URL must not include embedded credentials"
        raise LLMConfigurationError(msg)
    if parsed.query or parsed.fragment:
        msg = "LLM base URL must not include a query string or fragment"
        raise LLMConfigurationError(msg)
    return value


def _normalize_endpoint_path(base_url: str, endpoint_path: str) -> str:
    """Make an endpoint relative and avoid duplicating a configured /v1 prefix."""

    path = _require_value(endpoint_path, "LLM endpoint path").strip("/")
    base_path = urlsplit(base_url).path.rstrip("/")
    if base_path.endswith("/v1") and path.startswith("v1/"):
        path = path.removeprefix("v1/")
    return path


def _require_value(value: str, label: str) -> str:
    """Reject blank or placeholder configuration values."""

    normalized = value.strip()
    if not normalized or normalized.casefold() == "replace-me":
        msg = f"{label} is not configured"
        raise LLMConfigurationError(msg)
    return normalized


def _require_api_key(api_key: SecretStr) -> None:
    """Reject blank or placeholder API keys without including them in errors."""

    value = api_key.get_secret_value().strip()
    if not value or value.casefold() == "replace-me":
        msg = "LLM API key is not configured"
        raise LLMConfigurationError(msg)


def _http_error(status_code: int) -> LLMHTTPError:
    """Map provider status codes to safe, provider-independent exceptions."""

    if status_code == 400:
        return LLMBadRequestError(status_code, "invalid or unsupported request")
    if status_code == 401:
        return LLMAuthenticationError(status_code, "authentication failed")
    if status_code == 403:
        return LLMPermissionError(status_code, "permission denied")
    if status_code == 404:
        return LLMNotFoundError(status_code, "endpoint or model not found")
    if status_code == 429:
        return LLMRateLimitError(
            status_code,
            "rate limit or quota exceeded",
            retryable=True,
        )
    if status_code >= 500:
        return LLMServerError(
            status_code,
            "provider service error",
            retryable=status_code in {502, 503, 504},
        )
    return LLMHTTPError(status_code, "unexpected provider response")


def _parse_usage(value: object) -> LLMUsage:
    """Read optional usage counters without making them protocol requirements."""

    if not isinstance(value, dict):
        return LLMUsage()
    usage = cast(dict[str, object], value)
    return LLMUsage(
        prompt_tokens=_non_negative_int(usage.get("prompt_tokens")),
        completion_tokens=_non_negative_int(usage.get("completion_tokens")),
        total_tokens=_non_negative_int(usage.get("total_tokens")),
    )


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


# Backward-compatible name for existing local deployment imports.
LocalHTTPClient = OpenAICompatibleHTTPClient

__all__ = ["LocalHTTPClient", "OpenAICompatibleHTTPClient"]
