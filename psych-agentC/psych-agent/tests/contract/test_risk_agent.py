"""Contract tests for fake and model-backed risk agents."""

from __future__ import annotations

from typing import Any, cast

import pytest

from agents.risk_agent import FakeRiskAgent, RiskAgent
from llm.client import LLMClient
from llm.exceptions import LLMStructuredOutputError, LLMTimeoutError
from schemas.common import MessageId, SessionId
from schemas.messages import Message, MessageRole
from schemas.risk import RiskInput, RiskLevel, RiskResult, RiskRoute


class StubLLMClient:
    """Small async stub for RiskAgent contract tests."""

    def __init__(self, result: RiskResult | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[RiskResult],
        *,
        model_name: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> RiskResult:
        self.calls.append(
            {
                "prompt": prompt,
                "output_model": output_model,
                "model_name": model_name,
                "metadata": metadata,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _message(content: str, sequence_number: int = 1) -> Message:
    return Message(
        id=MessageId(f"msg_{sequence_number}"),
        session_id=SessionId("session_risk"),
        role=MessageRole.USER,
        content=content,
        sequence_number=sequence_number,
    )


def _input(content: str, recent_messages: list[Message] | None = None) -> RiskInput:
    return RiskInput(
        current_message=_message(content),
        recent_messages=recent_messages or [],
        current_risk_level=RiskLevel.LOW,
    )


def _normal_result() -> RiskResult:
    return RiskResult(
        risk_level=RiskLevel.LOW,
        categories=[],
        needs_clarification=False,
        route=RiskRoute.NORMAL,
        reason_codes=[],
        confidence=0.8,
    )


def _agent(llm: StubLLMClient) -> RiskAgent:
    return RiskAgent(cast(LLMClient, llm), prompt_template="risk prompt")


@pytest.mark.asyncio
async def test_model_backed_risk_agent_returns_risk_result() -> None:
    llm = StubLLMClient(_normal_result())
    agent = _agent(llm)

    result = await agent.analyze(_input("今天工作压力有点大。"))

    assert isinstance(result, RiskResult)
    assert result.route == RiskRoute.NORMAL


@pytest.mark.asyncio
async def test_model_backed_risk_agent_calls_generate_structured() -> None:
    llm = StubLLMClient(_normal_result())
    agent = RiskAgent(cast(LLMClient, llm), model_name="risk-model", prompt_template="risk prompt")

    await agent.analyze(_input("普通压力消息"))

    assert len(llm.calls) == 1
    assert llm.calls[0]["output_model"] is RiskResult
    assert llm.calls[0]["model_name"] == "risk-model"
    assert llm.calls[0]["metadata"] == {"agent": "risk_agent"}


@pytest.mark.asyncio
async def test_normal_message_can_use_normal_route() -> None:
    llm = StubLLMClient(_normal_result())
    agent = _agent(llm)

    result = await agent.analyze(_input("我想聊聊最近的工作焦虑。"))

    assert result.route == RiskRoute.NORMAL
    assert result.risk_level == RiskLevel.LOW


@pytest.mark.asyncio
async def test_explicit_self_harm_plan_cannot_use_normal_route() -> None:
    llm = StubLLMClient(_normal_result())
    agent = _agent(llm)

    result = await agent.analyze(_input("我今晚计划用药结束生命。"))

    assert result.route != RiskRoute.NORMAL
    assert result.route == RiskRoute.CRISIS
    assert "self_harm" in result.categories
    assert "plan_or_means" in result.categories


@pytest.mark.asyncio
async def test_explicit_harm_others_intent_cannot_use_normal_route() -> None:
    llm = StubLLMClient(_normal_result())
    agent = _agent(llm)

    result = await agent.analyze(_input("我今天准备拿刀伤害他们。"))

    assert result.route != RiskRoute.NORMAL
    assert result.route == RiskRoute.CRISIS
    assert "harm_others" in result.categories


@pytest.mark.asyncio
async def test_timeout_uses_conservative_fallback() -> None:
    llm = StubLLMClient(LLMTimeoutError("timed out at provider"))
    agent = _agent(llm)

    result = await agent.analyze(_input("我不知道还能不能撑下去。"))

    assert result.route == RiskRoute.CLARIFICATION
    assert result.risk_level == RiskLevel.MEDIUM
    assert "llm_timeout" in result.reason_codes


@pytest.mark.asyncio
async def test_invalid_json_uses_fallback() -> None:
    llm = StubLLMClient(LLMStructuredOutputError("invalid json from model"))
    agent = _agent(llm)

    result = await agent.analyze(_input("普通消息"))

    assert result.route == RiskRoute.CLARIFICATION
    assert "llm_structured_output_error" in result.reason_codes


@pytest.mark.asyncio
async def test_invalid_enum_uses_fallback() -> None:
    llm = StubLLMClient(LLMStructuredOutputError("invalid enum value"))
    agent = _agent(llm)

    result = await agent.analyze(_input("我最近很累。"))

    assert result.route == RiskRoute.CLARIFICATION
    assert "llm_structured_output_error" in result.reason_codes


@pytest.mark.asyncio
async def test_model_backed_risk_agent_does_not_modify_input_object() -> None:
    payload = _input("今天工作压力有点大。", recent_messages=[_message("前文", 2)])
    before = payload.model_dump()
    llm = StubLLMClient(_normal_result())
    agent = _agent(llm)

    await agent.analyze(payload)

    assert payload.model_dump() == before


@pytest.mark.asyncio
async def test_fake_risk_agent_original_behavior_still_works() -> None:
    agent = FakeRiskAgent()

    normal = await agent.analyze(_input("今天工作压力有点大。"))
    crisis = await agent.analyze(_input("我想伤害自己。"))

    assert normal.route == RiskRoute.NORMAL
    assert crisis.route == RiskRoute.CRISIS
    assert crisis.risk_level == RiskLevel.HIGH
@pytest.mark.asyncio
async def test_missing_required_fields_use_conservative_fallback() -> None:
    llm = StubLLMClient(LLMStructuredOutputError("missing required field: route"))
    agent = _agent(llm)

    result = await agent.analyze(_input("ordinary stress message"))

    assert isinstance(result, RiskResult)
    assert result.route == RiskRoute.CLARIFICATION
    assert result.risk_level == RiskLevel.MEDIUM
    assert "llm_structured_output_error" in result.reason_codes
