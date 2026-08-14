"""Tests for safe endpoint checks and the real structured-Agent smoke harness."""

import json

import httpx
import pytest

from app.config import Settings
from llm.retry_policy import RetryPolicy
from scripts.check_llm_endpoint import (
    CheckOptions,
    EndpointExitCode,
    render_result,
    run_endpoint_checks,
)
from scripts.check_llm_endpoint import (
    parse_options as parse_check_options,
)
from scripts.smoke_real_structured_agents import (
    SmokeOptions,
    render_summary,
    run_structured_agent_smoke,
)
from scripts.smoke_real_structured_agents import (
    parse_options as parse_smoke_options,
)

TEST_TOKEN = "script-test-token"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "llm_base_url": "https://provider.example",
        "llm_api_key": TEST_TOKEN,
        "main_model_name": "served-model",
        "structured_model_name": "served-model",
        "safety_model_name": "served-model",
        "llm_thinking_mode": "omit",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def _retry_once() -> RetryPolicy:
    return RetryPolicy(max_attempts=1, base_delay_seconds=0)


def _completion(content: str) -> dict[str, object]:
    return {
        "model": "served-model",
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


@pytest.mark.asyncio
async def test_endpoint_check_succeeds_without_exposing_secret() -> None:
    """All four stages should succeed against one deterministic MockTransport."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "served-model"}]})
        body = _request_json(request)
        if body.get("response_format") == {"type": "json_object"}:
            return httpx.Response(200, json=_completion('{"status":"ok"}'))
        return httpx.Response(200, json=_completion("OK"))

    result, exit_code = await run_endpoint_checks(
        _settings(),
        CheckOptions(),
        transport=httpx.MockTransport(handler),
        retry_policy=_retry_once(),
    )

    assert exit_code == EndpointExitCode.SUCCESS
    assert result.models_endpoint_status == "ok"
    assert result.chat_endpoint_status == "ok"
    assert result.json_output_status == "ok"
    assert result.content_non_empty is True
    assert result.json_parsed is True
    assert result.api_call_count == 3
    assert result.success is True
    assert TEST_TOKEN not in render_result(result, json_output=True)


@pytest.mark.asyncio
async def test_models_endpoint_unsupported_is_distinct_from_network_failure() -> None:
    """A GET /models 404 may be tolerated while chat and JSON checks continue."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, json={"error": {}})
        body = _request_json(request)
        content = (
            '{"status":"ok"}'
            if body.get("response_format") == {"type": "json_object"}
            else "OK"
        )
        return httpx.Response(200, json=_completion(content))

    result, exit_code = await run_endpoint_checks(
        _settings(),
        CheckOptions(),
        transport=httpx.MockTransport(handler),
        retry_policy=_retry_once(),
    )

    assert exit_code == EndpointExitCode.SUCCESS
    assert result.models_endpoint_status == "unsupported"
    assert result.success is True


@pytest.mark.asyncio
async def test_endpoint_check_reports_missing_config_and_model() -> None:
    """Configuration and served-model mismatches use stable distinct exit codes."""

    config_result, config_code = await run_endpoint_checks(
        _settings(llm_api_key="replace-me"),
        CheckOptions(),
        retry_policy=_retry_once(),
    )
    assert config_code == EndpointExitCode.CONFIGURATION
    assert config_result.error_type == "LLMConfigurationError"

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(200, json={"data": [{"id": "different-model"}]})

    model_result, model_code = await run_endpoint_checks(
        _settings(),
        CheckOptions(),
        transport=httpx.MockTransport(handler),
        retry_policy=_retry_once(),
    )
    assert model_code == EndpointExitCode.MODEL_NOT_FOUND
    assert model_result.models_endpoint_status == "model_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_kind", "expected_code"),
    [
        ("connection", EndpointExitCode.CONNECTION_OR_TIMEOUT),
        ("auth", EndpointExitCode.AUTH_OR_PERMISSION),
        ("protocol", EndpointExitCode.PROTOCOL),
        ("json", EndpointExitCode.JSON_VALIDATION),
    ],
)
async def test_endpoint_check_failure_exit_codes(
    handler_kind: str,
    expected_code: EndpointExitCode,
) -> None:
    """Operational, auth, protocol, and JSON failures remain distinguishable."""

    def handler(request: httpx.Request) -> httpx.Response:
        if handler_kind == "connection":
            raise httpx.ConnectError("unreachable", request=request)
        if handler_kind == "auth":
            return httpx.Response(401, json={"error": {}})
        if handler_kind == "protocol":
            return httpx.Response(200, json={"choices": []})
        return httpx.Response(200, json=_completion('{"status":"wrong"}'))

    options = CheckOptions(skip_model_list=True, structured_only=handler_kind == "json")
    _, exit_code = await run_endpoint_checks(
        _settings(),
        options,
        transport=httpx.MockTransport(handler),
        retry_policy=_retry_once(),
    )

    assert exit_code == expected_code


def test_endpoint_and_smoke_cli_options_are_stable() -> None:
    """Both manual tools should expose their documented safe parameters."""

    check_options = parse_check_options(
        ["--json", "--skip-model-list", "--timeout", "12", "--structured-only"]
    )
    smoke_options = parse_smoke_options(["--json", "--timeout", "15"])

    assert check_options == CheckOptions(
        json_output=True,
        skip_model_list=True,
        timeout_seconds=12,
        structured_only=True,
        models_only=False,
    )
    assert smoke_options == SmokeOptions(json_output=True, timeout_seconds=15)


@pytest.mark.asyncio
async def test_models_only_check_never_sends_a_completion() -> None:
    """Linux preflight should reuse discovery without a paid chat request."""

    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json={"data": [{"id": "served-model"}]})

    result, exit_code = await run_endpoint_checks(
        _settings(),
        CheckOptions(models_only=True),
        transport=httpx.MockTransport(handler),
        retry_policy=_retry_once(),
    )

    assert exit_code is EndpointExitCode.SUCCESS
    assert result.models_endpoint_status == "ok"
    assert result.chat_endpoint_status == "skipped"
    assert result.json_output_status == "skipped"
    assert result.api_call_count == 1
    assert methods == ["GET"]


@pytest.mark.asyncio
async def test_models_only_requires_a_supported_discovery_endpoint() -> None:
    """Preflight cannot accept a missing /models endpoint without model proof."""

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(404, json={"error": {}})

    result, exit_code = await run_endpoint_checks(
        _settings(),
        CheckOptions(models_only=True),
        transport=httpx.MockTransport(handler),
        retry_policy=_retry_once(),
    )

    assert exit_code is EndpointExitCode.PROTOCOL
    assert result.models_endpoint_status == "unsupported"
    assert result.success is False


@pytest.mark.asyncio
async def test_structured_agent_smoke_reports_three_real_schema_successes() -> None:
    """The smoke harness should use one formal HTTP/structured call per Agent."""

    responses = iter(
        [
            {
                "risk_level": "low",
                "categories": [],
                "needs_clarification": False,
                "route": "normal_dialogue",
                "reason_codes": ["synthetic_test"],
                "confidence": 0.9,
            },
            {
                "explicit_user_request": "Plan one short break after lunch.",
                "topic_updates": [],
                "goal_updates": [
                    {
                        "operation": "add",
                        "goal": "Plan one short break after lunch.",
                        "source_message_id": "msg_real_state_1",
                    }
                ],
                "reported_emotions": [],
                "user_corrections": [],
                "strategy_preferences": [],
                "hypotheses": [],
            },
            {
                "conversation_phase": "goal_alignment",
                "primary_strategy": "clarification",
                "objective": "Clarify the small planning goal.",
                "reason": "The synthetic request contains a concrete goal.",
                "avoid": ["diagnosis"],
                "expected_signals": ["goal is confirmed"],
                "switch_conditions": ["risk changes"],
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = _request_json(request)
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json=_completion(json.dumps(next(responses))))

    summary, exit_code = await run_structured_agent_smoke(
        _settings(),
        SmokeOptions(),
        transport=httpx.MockTransport(handler),
        retry_policy=_retry_once(),
    )

    assert exit_code == EndpointExitCode.SUCCESS
    assert [result.agent for result in summary.agents] == [
        "risk_agent",
        "state_tracker",
        "strategy_planner",
    ]
    assert all(result.api_success for result in summary.agents)
    assert all(result.schema_success for result in summary.agents)
    assert all(result.fallback_used is False for result in summary.agents)
    assert summary.agents[1].source_ids_valid is True
    assert summary.api_call_count == 3
    assert summary.success is True
    assert TEST_TOKEN not in render_summary(summary, json_output=True)


@pytest.mark.asyncio
async def test_structured_agent_smoke_distinguishes_fallback_from_model_success() -> None:
    """A fallback remains usable but must make the real smoke unsuccessful."""

    call_index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_index
        _ = request
        call_index += 1
        if call_index == 1:
            return httpx.Response(200, json={"choices": []})
        if call_index == 2:
            return httpx.Response(
                200,
                json=_completion(
                    json.dumps(
                        {
                            "explicit_user_request": "Plan one break.",
                            "topic_updates": [],
                            "goal_updates": [],
                            "reported_emotions": [],
                            "user_corrections": [],
                            "strategy_preferences": [],
                            "hypotheses": [],
                        }
                    )
                ),
            )
        return httpx.Response(
            200,
            json=_completion(
                json.dumps(
                    {
                        "conversation_phase": "exploration",
                        "primary_strategy": "reflective_listening",
                        "objective": "Clarify the synthetic request.",
                        "reason": "More detail may help.",
                        "avoid": [],
                        "expected_signals": [],
                        "switch_conditions": [],
                    }
                )
            ),
        )

    summary, exit_code = await run_structured_agent_smoke(
        _settings(),
        SmokeOptions(),
        transport=httpx.MockTransport(handler),
        retry_policy=_retry_once(),
    )

    risk = summary.agents[0]
    assert risk.api_success is True
    assert risk.schema_success is False
    assert risk.fallback_used is True
    assert risk.effective_result_available is True
    assert summary.success is False
    assert exit_code == EndpointExitCode.PROTOCOL
    assert summary.api_call_count == 3


def _request_json(request: httpx.Request) -> dict[str, object]:
    payload: object = json.loads(request.content)
    assert isinstance(payload, dict)
    return payload
