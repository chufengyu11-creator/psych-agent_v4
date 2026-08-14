"""Contract tests for response drafting and final output guarding."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from agents.output_guard import OutputGuard
from agents.response_agent import ResponseAgent
from llm.structured_output import ModelT
from schemas.context import ResponseContext
from schemas.llm import LLMRequest, LLMResponse
from schemas.risk import RiskLevel, RiskResult, RiskRoute
from schemas.safety import DraftResponse, GuardDecision, GuardInput
from schemas.state import ConversationPhase
from schemas.strategy import StrategyPlan, StrategyType
from services.context_builder import ContextBuilder
from tests.fixtures.memories import retrieved_work_stress_memories
from tests.fixtures.messages import work_stress_dialogue
from tests.fixtures.states import work_stress_state
from tests.fixtures.summaries import work_stress_rolling_summary


class StubTextClient:
    def __init__(self, text: str | Exception) -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if isinstance(self.text, Exception):
            raise self.text
        return LLMResponse(content=self.text, model_name=request.model_name)


class StubStructuredClient:
    def __init__(self, result: dict[str, object] | Exception) -> None:
        self.result = result
        self.calls = 0

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        _ = (prompt, model_name, metadata)
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return output_model.model_validate(self.result)


def _risk(route: RiskRoute = RiskRoute.NORMAL) -> RiskResult:
    return RiskResult(
        risk_level=RiskLevel.LOW if route == RiskRoute.NORMAL else RiskLevel.HIGH,
        route=route,
        reason_codes=["fixture"],
        confidence=0.9,
    )


def _strategy(strategy: StrategyType) -> StrategyPlan:
    return StrategyPlan(
        conversation_phase=ConversationPhase.INTERVENTION,
        primary_strategy=strategy,
        objective="choose one manageable next step",
        reason="fixture",
        avoid=["diagnosis"],
        expected_signals=["user can respond"],
        switch_conditions=["risk changes"],
    )


async def _context(strategy: StrategyType = StrategyType.CLARIFICATION) -> ResponseContext:
    return await ContextBuilder().build(
        session_state=work_stress_state(),
        rolling_summary=work_stress_rolling_summary(),
        recent_messages=work_stress_dialogue(),
        memories=retrieved_work_stress_memories(),
        strategy=_strategy(strategy),
        risk=_risk(),
    )


@pytest.mark.asyncio
async def test_response_agent_returns_typed_draft_without_mutating_context() -> None:
    context = await _context(StrategyType.CLARIFICATION)
    before = context.model_dump()
    client = StubTextClient("你更希望先梳理感受，还是先找一个下一步？")

    draft = await ResponseAgent(client, prompt_template="response prompt").generate(context)

    assert isinstance(draft, DraftResponse)
    assert draft.asked_question is True
    assert draft.contains_action_suggestion is False
    assert context.model_dump() == before


@pytest.mark.asyncio
async def test_response_agent_failure_uses_strategy_aligned_fallback() -> None:
    context = await _context(StrategyType.ACTION_PLANNING)

    draft = await ResponseAgent(
        StubTextClient(RuntimeError("provider down")),
        prompt_template="response prompt",
    ).generate(context)

    assert draft.contains_action_suggestion is True
    assert "下一步" in draft.text


@pytest.mark.asyncio
async def test_output_guard_blocks_diagnosis_before_model_call() -> None:
    context = await _context()
    model = StubStructuredClient({"decision": "allow", "violations": []})
    guard = OutputGuard(model, prompt_template="guard prompt")

    result = await guard.review(
        GuardInput(
            draft=DraftResponse(text="我可以诊断你是抑郁症。"),
            context=context,
            risk=_risk(),
        )
    )

    assert result.decision == GuardDecision.BLOCK
    assert "unsupported_diagnosis" in result.violations
    assert model.calls == 0


@pytest.mark.asyncio
async def test_output_guard_routes_ordinary_crisis_draft_to_safety() -> None:
    context = await _context()
    guard = OutputGuard(
        StubStructuredClient({"decision": "allow", "violations": []}),
        prompt_template="guard prompt",
    )

    result = await guard.review(
        GuardInput(
            draft=DraftResponse(text="我们继续聊工作安排。"),
            context=context,
            risk=_risk(RiskRoute.CRISIS),
        )
    )

    assert result.decision == GuardDecision.ROUTE_TO_SAFETY


@pytest.mark.asyncio
async def test_output_guard_failure_fails_closed() -> None:
    context = await _context()
    guard = OutputGuard(StubStructuredClient(RuntimeError("invalid")), prompt_template="guard")

    result = await guard.review(
        GuardInput(
            draft=DraftResponse(text="我听到这件事很不容易。"),
            context=context,
            risk=_risk(),
        )
    )

    assert result.decision == GuardDecision.BLOCK
    assert "output_guard_failure" in result.violations
