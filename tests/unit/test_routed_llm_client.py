"""Unit tests for model-name to endpoint routing."""

import json

import httpx
import pytest

from llm.exceptions import LLMConfigurationError
from llm.local_client import OpenAICompatibleHTTPClient
from llm.routed_client import RoutedLLMClient
from schemas.llm import LLMMessage, LLMMessageRole, LLMRequest


def _request(model_name: str) -> LLMRequest:
    return LLMRequest(
        messages=[LLMMessage(role=LLMMessageRole.USER, content="route this request")],
        model_name=model_name,
    )


def _client(
    base_url: str,
    model_name: str,
    calls: list[tuple[str, str]],
) -> OpenAICompatibleHTTPClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": model_name}]})
        payload = json.loads(request.content)
        calls.append((request.url.host or "", payload["model"]))
        return httpx.Response(
            200,
            json={
                "model": model_name,
                "choices": [
                    {
                        "message": {"role": "assistant", "content": model_name},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    return OpenAICompatibleHTTPClient(
        base_url,
        api_key="unit-test-key",
        model_name=model_name,
        transport=httpx.MockTransport(handler),
    )


async def test_router_sends_each_model_to_its_registered_endpoint() -> None:
    calls: list[tuple[str, str]] = []
    small = _client("https://small.test/v1", "local-small", calls)
    large = _client("https://large.test/v1", "local-large", calls)
    router = RoutedLLMClient(
        {"local-small": small, "local-large": large},
        default_model_name="local-small",
    )
    try:
        small_response = await router.generate(_request("local-small"))
        large_response = await router.generate(_request("local-large"))

        assert small_response.content == "local-small"
        assert large_response.content == "local-large"
        assert calls == [
            ("small.test", "local-small"),
            ("large.test", "local-large"),
        ]
        assert router.request_count == 2
        assert router.successful_http_response_count == 2
    finally:
        await router.aclose()

    assert router.is_closed


async def test_router_discovers_models_across_all_endpoints() -> None:
    calls: list[tuple[str, str]] = []
    router = RoutedLLMClient(
        {
            "local-small": _client(
                "https://small.test/v1", "local-small", calls
            ),
            "local-large": _client(
                "https://large.test/v1", "local-large", calls
            ),
        },
        default_model_name="local-small",
    )
    try:
        assert await router.list_models(retry=False) == [
            "local-small",
            "local-large",
        ]
    finally:
        await router.aclose()


async def test_router_rejects_unregistered_model_without_fallback() -> None:
    calls: list[tuple[str, str]] = []
    router = RoutedLLMClient(
        {"local-small": _client("https://small.test/v1", "local-small", calls)},
        default_model_name="local-small",
    )
    try:
        with pytest.raises(LLMConfigurationError, match="no LLM endpoint route"):
            await router.generate(_request("unknown-model"))
        assert calls == []
    finally:
        await router.aclose()


async def test_router_checks_model_on_its_assigned_endpoint() -> None:
    calls: list[tuple[str, str]] = []
    small_endpoint = _client(
        "https://small.test/v1",
        "different-small-model",
        calls,
    )
    large_endpoint = _client(
        "https://large.test/v1",
        "local-small",
        calls,
    )
    router = RoutedLLMClient(
        {"local-small": small_endpoint, "local-large": large_endpoint},
        default_model_name="local-small",
    )
    try:
        missing = await router.missing_model_names(
            ("local-small", "local-large"),
            retry=False,
        )
        assert missing == ["local-small", "local-large"]
    finally:
        await router.aclose()
