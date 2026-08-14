"""Tests for the provider-agnostic OpenAI-compatible HTTP client."""

from collections.abc import Callable

import httpx
import pytest
from anyio import EndOfStream
from pydantic import SecretStr

from app.config import LLMThinkingMode, Settings
from llm.exceptions import (
    LLMAuthenticationError,
    LLMConnectionError,
    LLMHTTPError,
    LLMOutputTruncatedError,
    LLMProtocolError,
    LLMTimeoutError,
)
from llm.local_client import LocalHTTPClient, OpenAICompatibleHTTPClient
from llm.retry_policy import RetryPolicy
from schemas.llm import LLMMessage, LLMMessageRole, LLMRequest

TEST_TOKEN = "unit-test-token"


def test_local_http_client_name_remains_a_compatible_alias() -> None:
    """Existing imports keep working while the formal class has a clearer name."""

    assert LocalHTTPClient is OpenAICompatibleHTTPClient


def _completion(
    content: object = "OK",
    *,
    finish_reason: object = "stop",
    reasoning_content: str | None = None,
    usage: object = None,
) -> dict[str, object]:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    payload: dict[str, object] = {
        "model": "served-model",
        "choices": [{"message": message, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def _request(*, response_format: str | None = None) -> LLMRequest:
    return LLMRequest(
        model_name="served-model",
        messages=[LLMMessage(role=LLMMessageRole.USER, content="Connectivity test")],
        response_format=response_format,
    )


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    base_url: str = "https://provider.example",
    thinking_mode: LLMThinkingMode = "omit",
    retry_policy: RetryPolicy | None = None,
) -> OpenAICompatibleHTTPClient:
    return OpenAICompatibleHTTPClient(
        base_url,
        api_key=SecretStr(TEST_TOKEN),
        model_name="served-model",
        thinking_mode=thinking_mode,
        retry_policy=retry_policy or RetryPolicy(max_attempts=1),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "expected_url"),
    [
        ("https://api.example", "https://api.example/chat/completions"),
        ("https://api.example/", "https://api.example/chat/completions"),
        ("http://127.0.0.1:8001/v1", "http://127.0.0.1:8001/v1/chat/completions"),
        ("http://127.0.0.1:8001/v1/", "http://127.0.0.1:8001/v1/chat/completions"),
    ],
)
async def test_chat_url_normalization(base_url: str, expected_url: str) -> None:
    """API-root joining must avoid doubled slashes and duplicated /v1 prefixes."""

    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json=_completion())

    await _client(handler, base_url=base_url).generate(_request())

    assert seen_urls == [expected_url]


@pytest.mark.asyncio
async def test_authorization_content_type_and_non_streaming_payload() -> None:
    """Requests must use Bearer auth, JSON content type, and stream=false."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
        assert request.headers["Content-Type"] == "application/json"
        payload = _request_json(request)
        assert payload["stream"] is False
        assert payload["model"] == "served-model"
        assert payload["max_tokens"] == 800
        return httpx.Response(200, json=_completion())

    await _client(handler).generate(_request())


@pytest.mark.asyncio
async def test_models_endpoint_returns_model_ids() -> None:
    """GET /models should validate and return served model identifiers."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://provider.example/models"
        assert request.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "served-model"}]},
        )

    assert await _client(handler).list_models() == ["served-model"]


@pytest.mark.asyncio
async def test_json_output_request_contains_response_format() -> None:
    """Structured requests must translate the abstract format into an API object."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _request_json(request)
        assert payload["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json=_completion('{"status":"ok"}'))

    await _client(handler).generate(_request(response_format="json_object"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("thinking_mode", "expected"),
    [
        ("omit", None),
        ("disabled", {"type": "disabled"}),
        ("enabled", {"type": "enabled"}),
    ],
)
async def test_thinking_mode_is_optional(
    thinking_mode: LLMThinkingMode,
    expected: dict[str, str] | None,
) -> None:
    """Provider extensions must be absent unless explicitly configured."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _request_json(request)
        if expected is None:
            assert "thinking" not in payload
        else:
            assert payload["thinking"] == expected
        return httpx.Response(200, json=_completion())

    await _client(handler, thinking_mode=thinking_mode).generate(_request())


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 429, 500])
async def test_http_statuses_become_domain_errors(status_code: int) -> None:
    """Provider statuses must not leak raw httpx exceptions to agents."""

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(status_code, json={"error": {"message": "unsafe detail"}})

    with pytest.raises(LLMHTTPError) as exc_info:
        await _client(handler).generate(_request())

    assert exc_info.value.status_code == status_code
    assert "unsafe detail" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_api_key_is_redacted_from_repr_and_error() -> None:
    """Diagnostics must never reveal the configured Bearer token."""

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(401, json={"error": {"message": TEST_TOKEN}})

    client = _client(handler)
    assert TEST_TOKEN not in repr(client)
    with pytest.raises(LLMAuthenticationError) as exc_info:
        await client.generate(_request())
    assert TEST_TOKEN not in str(exc_info.value)

    settings = Settings(llm_api_key=TEST_TOKEN, _env_file=None)
    assert TEST_TOKEN not in repr(settings)


@pytest.mark.asyncio
async def test_connection_error_is_sanitized() -> None:
    """Transport connection failures must become LLMConnectionError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LLMConnectionError, match="could not connect"):
        await _client(handler).generate(_request())


@pytest.mark.asyncio
async def test_tls_end_of_stream_is_sanitized() -> None:
    """A TLS stream ending during a request must not escape the client boundary."""

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        raise EndOfStream

    with pytest.raises(LLMConnectionError, match="could not connect"):
        await _client(handler).generate(_request())


@pytest.mark.asyncio
async def test_timeout_is_sanitized() -> None:
    """Read/connect timeouts must become LLMTimeoutError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(LLMTimeoutError, match="timed out"):
        await _client(handler).generate(_request())


@pytest.mark.asyncio
async def test_non_json_response_is_protocol_error() -> None:
    """A successful non-JSON body must be rejected."""

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(200, text="not json")

    with pytest.raises(LLMProtocolError, match="not valid JSON"):
        await _client(handler).generate(_request())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"choices": []}, "must not be empty"),
        ({"choices": [{}]}, "message object"),
        (_completion(None), "content was null"),
        (_completion("   "), "content was empty"),
    ],
)
async def test_invalid_completion_shapes_are_protocol_errors(
    payload: dict[str, object],
    message: str,
) -> None:
    """Missing choices/message/content fields must fail with precise diagnostics."""

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(200, json=payload)

    with pytest.raises(LLMProtocolError, match=message):
        await _client(handler).generate(_request())


@pytest.mark.asyncio
async def test_finish_reason_length_is_truncation_error() -> None:
    """Truncated output must not be accepted as a complete model response."""

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(200, json=_completion("partial", finish_reason="length"))

    with pytest.raises(LLMOutputTruncatedError, match="truncated"):
        await _client(handler).generate(_request())


@pytest.mark.asyncio
async def test_reasoning_content_is_not_exposed_and_usage_is_optional() -> None:
    """Only final content is normalized; hidden reasoning is discarded."""

    hidden = "hidden-reasoning-sentinel"

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(200, json=_completion("OK", reasoning_content=hidden))

    response = await _client(handler).generate(_request())

    assert response.content == "OK"
    assert hidden not in repr(response)
    assert response.usage.prompt_tokens is None
    assert response.usage.completion_tokens is None
    assert response.usage.total_tokens is None


@pytest.mark.asyncio
async def test_usage_is_read_when_present() -> None:
    """Optional token counters should be normalized when returned."""

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(
            200,
            json=_completion(
                usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
            ),
        )

    response = await _client(handler).generate(_request())

    assert response.usage.prompt_tokens == 2
    assert response.usage.completion_tokens == 1
    assert response.usage.total_tokens == 3


@pytest.mark.asyncio
async def test_retry_policy_retries_429_without_real_sleep() -> None:
    """A transient status should retry once using the injected sleeper."""

    attempts = 0
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        _ = request
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, json={"error": {}})
        return httpx.Response(200, json=_completion())

    retry_policy = RetryPolicy(
        max_attempts=2,
        base_delay_seconds=0.25,
        sleeper=sleeper,
    )
    client = _client(handler, retry_policy=retry_policy)

    response = await client.generate(_request())

    assert response.content == "OK"
    assert client.request_count == 2
    assert delays == [0.25]


@pytest.mark.asyncio
async def test_retry_policy_does_not_retry_500() -> None:
    """Plain HTTP 500 is mapped but excluded from the bounded retry set."""

    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        _ = request
        attempts += 1
        return httpx.Response(500, json={"error": {}})

    with pytest.raises(LLMHTTPError):
        await _client(
            handler,
            retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0),
        ).generate(_request())

    assert attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [502, 503, 504])
async def test_retry_policy_retries_selected_gateway_errors(status_code: int) -> None:
    """Only the explicitly allowed transient 5xx statuses should retry."""

    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        _ = request
        attempts += 1
        if attempts == 1:
            return httpx.Response(status_code, json={"error": {}})
        return httpx.Response(200, json=_completion())

    response = await _client(
        handler,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
    ).generate(_request())

    assert response.content == "OK"
    assert attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 500])
async def test_retry_policy_does_not_retry_permanent_http_errors(status_code: int) -> None:
    """Configuration, auth, not-found, and plain 500 responses fail immediately."""

    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        _ = request
        attempts += 1
        return httpx.Response(status_code, json={"error": {}})

    with pytest.raises(LLMHTTPError):
        await _client(
            handler,
            retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0),
        ).generate(_request())

    assert attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["connection", "timeout"])
async def test_retry_policy_retries_transient_transport_failures(
    failure_kind: str,
) -> None:
    """One transient connection or read timeout should be retried."""

    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1 and failure_kind == "connection":
            raise httpx.ConnectError("unreachable", request=request)
        if attempts == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json=_completion())

    response = await _client(
        handler,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
    ).generate(_request())

    assert response.content == "OK"
    assert attempts == 2


def _request_json(request: httpx.Request) -> dict[str, object]:
    import json

    value: object = json.loads(request.content)
    assert isinstance(value, dict)
    return value
