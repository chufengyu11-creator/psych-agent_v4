"""Tests for the shared LLM client layer."""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

from llm.client import LLMClient
from llm.exceptions import LLMRetryExhaustedError, LLMTimeoutError
from llm.model_registry import ModelConfig, ModelRegistry
from llm.retry_policy import RetryPolicy


class ParsedReply(BaseModel):
    answer: str
    confidence: float


def _registry() -> ModelRegistry:
    return ModelRegistry(
        [ModelConfig(name="test-model", endpoint="https://llm.test/generate")],
        default_model="test-model",
    )


@pytest.mark.asyncio
async def test_generate_text_uses_mock_http_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://llm.test/generate"
        return httpx.Response(200, json={"text": "hello from model"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = LLMClient(_registry(), http_client=http_client)

    result = await client.generate_text("Say hello")

    assert result == "hello from model"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_generate_structured_parses_pydantic_object() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": '{"answer": "ok", "confidence": 0.9}'})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = LLMClient(_registry(), http_client=http_client)

    result = await client.generate_structured("Return JSON", ParsedReply)

    assert result == ParsedReply(answer="ok", confidence=0.9)
    await http_client.aclose()


@pytest.mark.asyncio
async def test_invalid_json_is_retried_before_parsing_success() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"text": "not json"})
        return httpx.Response(200, json={"text": '{"answer": "retried", "confidence": 0.7}'})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = LLMClient(
        _registry(),
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
        http_client=http_client,
    )

    result = await client.generate_structured("Return JSON", ParsedReply)

    assert calls == 2
    assert result.answer == "retried"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_timeout_raises_typed_error_without_crashing_process() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider timed out", request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = LLMClient(
        _registry(),
        retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
        http_client=http_client,
    )

    with pytest.raises(LLMTimeoutError):
        await client.generate_text("Say hello")

    await http_client.aclose()


@pytest.mark.asyncio
async def test_invalid_json_exhausts_retry_policy() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "still not json"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = LLMClient(
        _registry(),
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
        http_client=http_client,
    )

    with pytest.raises(LLMRetryExhaustedError):
        await client.generate_structured("Return JSON", ParsedReply)

    await http_client.aclose()
