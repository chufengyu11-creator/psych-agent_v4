"""Unit tests for the OpenAI-compatible shared LLM client."""

from __future__ import annotations

import json

import httpx
import pytest

from llm.client import LLMClient
from llm.exceptions import LLMHTTPError
from llm.model_registry import ModelConfig, ModelRegistry
from llm.retry_policy import RetryPolicy
from schemas.llm import LLMMessage, LLMMessageRole, LLMRequest
from schemas.risk import RiskLevel, RiskResult, RiskRoute


def _registry() -> ModelRegistry:
    return ModelRegistry(
        models=[
            ModelConfig(
                name="main-model",
                provider_model_name="provider-main",
                endpoint="https://llm.test/v1/chat/completions",
                timeout_seconds=2.0,
            ),
            ModelConfig(
                name="safety-model",
                provider_model_name="provider-safety",
                endpoint="https://llm.test/v1/chat/completions",
                timeout_seconds=2.0,
            ),
        ],
        default_model="main-model",
        agent_models={"risk_agent": "safety-model"},
    )


@pytest.mark.asyncio
async def test_generate_uses_openai_chat_completions_shape() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "model": "provider-main",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LLMClient(_registry(), http_client=http_client)
        response = await client.generate(
            LLMRequest(
                model_name="main-model",
                messages=[LLMMessage(role=LLMMessageRole.USER, content="hi")],
            )
        )

    assert captured["model"] == "provider-main"
    assert captured["messages"] == [{"role": "user", "content": "hi"}]
    assert response.content == "hello"
    assert response.finish_reason == "stop"
    assert response.usage.total_tokens == 12


@pytest.mark.asyncio
async def test_generate_structured_routes_by_agent_and_retries_invalid_json() -> None:
    calls: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        calls.append(payload)
        content = "not-json" if len(calls) == 1 else json.dumps(
            {
                "risk_level": "low",
                "categories": [],
                "needs_clarification": False,
                "route": "normal_dialogue",
                "reason_codes": ["fixture"],
                "confidence": 0.9,
            }
        )
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "choices": [{"message": {"role": "assistant", "content": content}}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LLMClient(
            _registry(),
            retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
            http_client=http_client,
        )
        result = await client.generate_structured(
            "classify risk",
            RiskResult,
            metadata={"agent": "risk_agent"},
        )

    assert len(calls) == 2
    assert calls[0]["model"] == "provider-safety"
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert result.risk_level == RiskLevel.LOW
    assert result.route == RiskRoute.NORMAL


@pytest.mark.asyncio
async def test_generate_retries_timeout_then_succeeds() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("slow provider", request=request)
        return httpx.Response(
            200,
            json={
                "model": "provider-main",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LLMClient(
            _registry(),
            retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
            http_client=http_client,
        )
        text = await client.generate_text("hello", model_name="main-model")

    assert attempts == 2
    assert text == "ok"


@pytest.mark.asyncio
async def test_generate_does_not_retry_nonretryable_http_error() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, text="bad request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LLMClient(
            _registry(),
            retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0),
            http_client=http_client,
        )
        with pytest.raises(LLMHTTPError):
            await client.generate_text("hello", model_name="main-model")

    assert attempts == 1
