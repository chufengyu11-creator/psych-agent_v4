"""Contract tests for fake and model-backed strategy planners."""

from __future__ import annotations

from typing import Any, cast

import pytest

from agents.strategy_planner import FakeStrategyPlanner, StrategyPlanner
from llm.client import LLMClient
from llm.exceptions import LLMStructuredOutputError, LLMTimeoutError
from schemas.common import SessionId
from schemas.feedback import FeedbackLabel, FeedbackResult, ObjectiveProgress, StrategyFit
from schemas.memory import RetrievedMemories
from schemas.risk import RiskLevel, RiskResult, RiskRoute
from schemas.state import ConversationPhase, SessionState
from schemas.strategy import StrategyPlan, StrategyPlannerInput, StrategyType
from tests.fixtures.memories import retrieved_work_stress_memories
from tests.fixtures.states import work_stress_state


class StubLLMClient:
    """Small async stub for StrategyPlanner contract tests."""

    def __init__(self, result: StrategyPlan | Exception | object) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[StrategyPlan],
        *,
        model_name: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> StrategyPlan:
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
        return cast(StrategyPlan, self.result)


def _risk(route: RiskRoute = RiskRoute.NORMAL) -> RiskResult:
    return RiskResult(
        risk_level=RiskLevel.LOW if route == RiskRoute.NORMAL else RiskLevel.HIGH,
        categories=[] if route == RiskRoute.NORMAL else ["self_harm"],
        needs_clarification=route != RiskRoute.NORMAL,
        route=route,
        reason_codes=[],
        confidence=0.8,
    )


def _poor_feedback() -> FeedbackResult:
    return FeedbackResult(
        observed_response="不要再分析情绪，我需要具体办法",
        explicit_feedback=FeedbackLabel.NEGATIVE,
        objective_progress=ObjectiveProgress.NOT_ACHIEVED,
        strategy_fit=StrategyFit.POOR,
        recommended_adjustment="改为具体行动建议",
        confidence=0.9,
    )


def _input(
    *,
    state: SessionState | None = None,
    risk: RiskResult | None = None,
    feedback: FeedbackResult | None = None,
    memories: RetrievedMemories | None = None,
) -> StrategyPlannerInput:
    return StrategyPlannerInput(
        session_state=state or SessionState(session_id=SessionId("session_strategy")),
        risk=risk or _risk(),
        feedback=feedback,
        memories=memories or RetrievedMemories(),
    )


def _reflective_plan() -> StrategyPlan:
    return StrategyPlan(
        conversation_phase=ConversationPhase.EXPLORATION,
        primary_strategy=StrategyType.REFLECTIVE_LISTENING,
        objective="承接用户表达",
        reason="模型认为可以继续倾听",
        avoid=["诊断"],
        expected_signals=["用户继续补充"],
        switch_conditions=["用户要求建议"],
    )


def _collaborative_plan() -> StrategyPlan:
    return StrategyPlan(
        conversation_phase=ConversationPhase.INTERVENTION,
        primary_strategy=StrategyType.COLLABORATIVE_PROBLEM_SOLVING,
        objective="和用户一起找到具体下一步",
        reason="用户需要具体办法",
        avoid=["继续重复上一轮策略"],
        expected_signals=["用户选择一个小步骤"],
        switch_conditions=["出现安全风险"],
    )


def _agent(llm: StubLLMClient) -> StrategyPlanner:
    return StrategyPlanner(cast(LLMClient, llm), prompt_template="strategy planner prompt")


@pytest.mark.asyncio
async def test_model_backed_strategy_planner_returns_strategy_plan() -> None:
    llm = StubLLMClient(_collaborative_plan())
    agent = _agent(llm)

    result = await agent.plan(_input())

    assert isinstance(result, StrategyPlan)
    assert result.primary_strategy == StrategyType.COLLABORATIVE_PROBLEM_SOLVING


@pytest.mark.asyncio
async def test_model_backed_strategy_planner_calls_generate_structured() -> None:
    llm = StubLLMClient(_collaborative_plan())
    agent = StrategyPlanner(
        cast(LLMClient, llm),
        model_name="strategy-model",
        prompt_template="strategy planner prompt",
    )

    await agent.plan(_input(memories=retrieved_work_stress_memories()))

    assert len(llm.calls) == 1
    assert llm.calls[0]["output_model"] is StrategyPlan
    assert llm.calls[0]["model_name"] == "strategy-model"
    assert llm.calls[0]["metadata"] == {"agent": "strategy_planner"}
    assert "retrieved_memories_json" in llm.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_poor_negative_feedback_switches_away_from_reflective_listening() -> None:
    llm = StubLLMClient(_reflective_plan())
    agent = _agent(llm)

    result = await agent.plan(_input(feedback=_poor_feedback()))

    assert result.primary_strategy == StrategyType.COLLABORATIVE_PROBLEM_SOLVING
    assert "继续重复上一轮策略" in result.avoid


@pytest.mark.asyncio
async def test_poor_negative_feedback_can_accept_concrete_model_strategy() -> None:
    llm = StubLLMClient(_collaborative_plan())
    agent = _agent(llm)

    result = await agent.plan(_input(feedback=_poor_feedback()))

    assert result.primary_strategy == StrategyType.COLLABORATIVE_PROBLEM_SOLVING
    assert result.reason == "用户需要具体办法"


@pytest.mark.asyncio
async def test_risk_route_fallback_uses_safety_check() -> None:
    llm = StubLLMClient(LLMTimeoutError("provider timeout"))
    agent = _agent(llm)

    result = await agent.plan(_input(risk=_risk(RiskRoute.CRISIS)))

    assert result.primary_strategy == StrategyType.SAFETY_CHECK
    assert result.conversation_phase == ConversationPhase.SAFETY_CHECK


@pytest.mark.asyncio
async def test_invalid_strategy_enum_uses_fallback_plan() -> None:
    llm = StubLLMClient(LLMStructuredOutputError("invalid primary_strategy deep_therapy"))
    agent = _agent(llm)

    result = await agent.plan(_input(feedback=_poor_feedback()))

    assert result.primary_strategy == StrategyType.COLLABORATIVE_PROBLEM_SOLVING
    assert result.primary_strategy.value != "deep_therapy"


@pytest.mark.asyncio
async def test_memory_or_preference_fallback_uses_collaborative_problem_solving() -> None:
    llm = StubLLMClient(LLMStructuredOutputError("invalid json"))
    agent = _agent(llm)

    result = await agent.plan(
        _input(
            state=work_stress_state(),
            memories=retrieved_work_stress_memories(),
        )
    )

    assert result.primary_strategy == StrategyType.COLLABORATIVE_PROBLEM_SOLVING
    assert "偏好" in result.reason


@pytest.mark.asyncio
async def test_model_backed_strategy_planner_does_not_modify_input_object() -> None:
    payload = _input(
        state=work_stress_state(),
        feedback=_poor_feedback(),
        memories=retrieved_work_stress_memories(),
    )
    before = payload.model_dump()
    llm = StubLLMClient(_collaborative_plan())
    agent = _agent(llm)

    await agent.plan(payload)

    assert payload.model_dump() == before


@pytest.mark.asyncio
async def test_fake_strategy_planner_original_feedback_behavior_still_works() -> None:
    agent = FakeStrategyPlanner()

    result = await agent.plan(_input(feedback=_poor_feedback()))

    assert result.primary_strategy == StrategyType.CLARIFICATION
    assert "继续重复上一轮策略" in result.avoid
