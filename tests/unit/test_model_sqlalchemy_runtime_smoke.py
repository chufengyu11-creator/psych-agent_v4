"""Offline tests for the manual model-assisted SQLAlchemy runtime smoke."""

import json
from collections.abc import Callable

import httpx
from anyio import Path

from app.config import Settings
from llm.retry_policy import RetryPolicy
from scripts.smoke_model_sqlalchemy_runtime import (
    RuntimeSmokeExitCode,
    RuntimeSmokeOptions,
    parse_options,
    render_summary,
    run_runtime_smoke,
)

TEST_KEY = "runtime-smoke-unit-key"
TEST_MODEL = "runtime-smoke-model"


def _settings(*, api_key: str = TEST_KEY) -> Settings:
    return Settings(
        app_env="testing",
        app_runtime_mode="in_memory",
        database_url="sqlite+aiosqlite:///:memory:",
        llm_base_url="https://runtime-smoke.test/v1",
        llm_api_key=api_key,
        structured_model_name=TEST_MODEL,
        main_model_name=TEST_MODEL,
        safety_model_name=TEST_MODEL,
        _env_file=None,
    )


def _response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": TEST_MODEL,
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        },
    )


def _risk_json() -> str:
    return json.dumps(
        {
            "risk_level": "low",
            "categories": [],
            "needs_clarification": False,
            "route": "normal_dialogue",
            "reason_codes": ["artificial_low_risk"],
            "confidence": 0.9,
        }
    )


def _state_json() -> str:
    return json.dumps(
        {
            "explicit_user_request": "Complete one artificial planning step.",
            "topic_updates": [],
            "goal_updates": [],
            "reported_emotions": [],
            "user_corrections": [],
            "strategy_preferences": [],
            "hypotheses": [],
        }
    )


def _strategy_json() -> str:
    return json.dumps(
        {
            "conversation_phase": "exploration",
            "primary_strategy": "reflective_listening",
            "objective": "clarify one artificial planning step",
            "reason": "the artificial input is low risk",
            "avoid": ["diagnosis", "medication advice"],
            "expected_signals": ["the test turn completes"],
            "switch_conditions": ["risk changes"],
        }
    )


def _response_json() -> str:
    return json.dumps(
        {
            "text": "I hear you want one small and manageable next step.",
            "asked_question": False,
            "contains_action_suggestion": True,
            "referenced_memory_ids": [],
        }
    )


def _guard_json() -> str:
    return json.dumps(
        {
            "decision": "allow",
            "violations": [],
            "rewritten_response": None,
        }
    )


def _feedback_json() -> str:
    return json.dumps(
        {
            "explicit_feedback": "absent",
            "objective_progress": "unknown",
            "strategy_fit": "unknown",
            "confidence": 0.7,
            "evidence_message_id": None,
            "evidence_quote": None,
        }
    )


def _handler(
    *,
    invalid_state: bool = False,
) -> tuple[dict[str, int], Callable[[httpx.Request], httpx.Response]]:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        payload = json.loads(request.content)
        prompt = payload["messages"][0]["content"]
        if "- agent: risk_agent" in prompt:
            return _response(_risk_json())
        if "- agent: state_tracker" in prompt:
            return _response("not-json" if invalid_state else _state_json())
        if "- agent: strategy_planner" in prompt:
            return _response(_strategy_json())
        if "- agent: response_agent" in prompt:
            return _response(_response_json())
        if "- agent: output_guard" in prompt:
            return _response(_guard_json())
        if "- agent: feedback_evaluator" in prompt:
            return _response(_feedback_json())
        return httpx.Response(400)

    return calls, handler


def test_runtime_smoke_argument_contract() -> None:
    """ASCII, JSON, and keep-db options should remain stable."""

    assert parse_options(["--ascii"]) == RuntimeSmokeOptions(ascii_output=True)
    assert parse_options(["--json", "--keep-db"]) == RuntimeSmokeOptions(
        json_output=True,
        keep_db=True,
    )


async def test_missing_key_exits_before_any_network_call() -> None:
    """Placeholder credentials should fail safely with zero provider requests."""

    calls, handler = _handler()
    summary, exit_code = await run_runtime_smoke(
        _settings(api_key="replace-me"),
        RuntimeSmokeOptions(),
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(max_attempts=1),
    )

    assert exit_code is RuntimeSmokeExitCode.CONFIGURATION
    assert not summary.success
    assert summary.api_attempts == 0
    assert calls["count"] == 0
    output = render_summary(summary, json_output=True)
    assert TEST_KEY not in output
    assert "Authorization" not in output


async def test_mocked_runtime_smoke_runs_two_turns_and_can_keep_only_its_db() -> None:
    """The offline smoke should exercise the full formal structured model chain."""

    calls, handler = _handler()
    summary, exit_code = await run_runtime_smoke(
        _settings(),
        RuntimeSmokeOptions(json_output=True, keep_db=True),
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(max_attempts=1),
    )
    try:
        assert exit_code is RuntimeSmokeExitCode.SUCCESS
        assert summary.success
        assert summary.turns_completed == 2
        assert summary.messages_rows == 4
        assert summary.state_versions_rows == 2
        assert summary.intervention_events_rows == 2
        assert summary.source_message_ids_match
        assert summary.api_attempts == 12
        assert summary.structured_calls == 12
        assert summary.fallback_count == 0
        assert calls["count"] == 12
        assert summary.database_kept
        assert summary.database_path is not None
        assert await Path(summary.database_path).is_file()
        payload = json.loads(render_summary(summary, json_output=True))
        assert payload["success"] is True
        assert TEST_KEY not in json.dumps(payload)
        assert "reasoning_content" not in json.dumps(payload)
    finally:
        if summary.database_path is not None:
            await Path(summary.database_path).unlink(missing_ok=True)


async def test_schema_failure_is_counted_as_agent_fallback_not_runtime_fallback() -> None:
    """A model parse failure may yield an Agent fallback but never a fake Runtime."""

    calls, handler = _handler(invalid_state=True)
    summary, exit_code = await run_runtime_smoke(
        _settings(),
        RuntimeSmokeOptions(),
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(max_attempts=1),
    )

    state_stats = next(item for item in summary.agents if item.agent == "state_tracker")
    assert exit_code is RuntimeSmokeExitCode.ACCEPTANCE
    assert not summary.success
    assert state_stats.api_success == 2
    assert state_stats.schema_success == 0
    assert state_stats.fallback_used == 2
    assert state_stats.effective_result_available == 2
    assert summary.fallback_count == 2
    assert calls["count"] == 12
