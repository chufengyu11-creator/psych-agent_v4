"""Contract tests for model-backed risk analysis."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from agents.risk_agent import RiskAgent
from llm.structured_output import ModelT
from schemas.common import MessageId, SessionId
from schemas.messages import Message, MessageRole
from schemas.risk import RiskInput, RiskLevel, RiskResult, RiskRoute


class StubStructuredClient:
    def __init__(self, result: dict[str, object] | Exception) -> None:
        self.result = result

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        _ = (prompt, model_name, metadata)
        if isinstance(self.result, Exception):
            raise self.result
        return output_model.model_validate(self.result)


def _message(text: str) -> Message:
    return Message(
        id=MessageId("msg_risk_1"),
        session_id=SessionId("session_risk"),
        role=MessageRole.USER,
        content=text,
        sequence_number=1,
    )


@pytest.mark.asyncio
async def test_risk_agent_returns_typed_normal_result() -> None:
    agent = RiskAgent(
        StubStructuredClient(
            {
                "risk_level": "low",
                "categories": [],
                "needs_clarification": False,
                "route": "normal_dialogue",
                "reason_codes": ["no_risk_signal"],
                "confidence": 0.9,
            }
        ),
        prompt_template="risk prompt",
    )

    result = await agent.analyze(RiskInput(current_message=_message("I feel stressed.")))

    assert isinstance(result, RiskResult)
    assert result.route == RiskRoute.NORMAL


@pytest.mark.asyncio
async def test_risk_agent_never_routes_explicit_self_harm_to_normal() -> None:
    agent = RiskAgent(
        StubStructuredClient(
            {
                "risk_level": "low",
                "categories": [],
                "needs_clarification": False,
                "route": "normal_dialogue",
                "reason_codes": [],
                "confidence": 0.9,
            }
        ),
        prompt_template="risk prompt",
    )

    result = await agent.analyze(RiskInput(current_message=_message("I want to kill myself.")))

    assert result.risk_level == RiskLevel.HIGH
    assert result.route == RiskRoute.CRISIS
    assert "self_harm" in result.categories


@pytest.mark.asyncio
async def test_risk_agent_failure_uses_conservative_clarification() -> None:
    agent = RiskAgent(StubStructuredClient(RuntimeError("provider down")), prompt_template="risk")

    result = await agent.analyze(RiskInput(current_message=_message("I am overwhelmed.")))

    assert result.risk_level == RiskLevel.MEDIUM
    assert result.route == RiskRoute.CLARIFICATION
    assert result.needs_clarification is True
