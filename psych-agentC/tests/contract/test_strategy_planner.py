"""Contract tests for model-backed strategy planning."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from agents.strategy_planner import StrategyPlanner
from llm.structured_output import ModelT
from schemas.common import SessionId
from schemas.feedback import FeedbackLabel, FeedbackResult, ObjectiveProgress, StrategyFit
from schemas.risk import RiskLevel, RiskResult, RiskRoute
from schemas.state import SessionState
from schemas.strategy import StrategyPlannerInput, StrategyType


class StubStructuredClient:
    def __init__(self, result: dict[str, object]) -> None:
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
        return output_model.model_validate(self.result)


def _plan_result(strategy: str = "reflective_listening") -> dict[str, object]:
    return {
        "conversation_phase": "exploration",
        "primary_strategy": strategy,
        "objective": "continue support",
        "reason": "model choice",
        "avoid": [],
        "expected_signals": [],
        "switch_conditions": [],
    }


def _risk(route: RiskRoute = RiskRoute.NORMAL) -> RiskResult:
    return RiskResult(
        risk_level=RiskLevel.LOW if route == RiskRoute.NORMAL else RiskLevel.HIGH,
        route=route,
        reason_codes=["fixture"],
        confidence=0.9,
    )


@pytest.mark.asyncio
async def test_negative_poor_fit_feedback_switches_rejected_strategy() -> None:
    feedback = FeedbackResult(
        explicit_feedback=FeedbackLabel.NEGATIVE,
        strategy_fit=StrategyFit.POOR,
        observed_response="user rejected reflection",
        objective_progress=ObjectiveProgress.NOT_ACHIEVED,
        recommended_adjustment="be concrete",
        confidence=0.9,
    )
    agent = StrategyPlanner(StubStructuredClient(_plan_result()), prompt_template="strategy")

    plan = await agent.plan(
        StrategyPlannerInput(
            session_state=SessionState(session_id=SessionId("session_strategy")),
            risk=_risk(),
            feedback=feedback,
        )
    )

    assert plan.primary_strategy == StrategyType.COLLABORATIVE_PROBLEM_SOLVING
    assert "repeat previous strategy" in plan.avoid


@pytest.mark.asyncio
async def test_non_normal_risk_overrides_ordinary_model_strategy() -> None:
    agent = StrategyPlanner(StubStructuredClient(_plan_result()), prompt_template="strategy")

    plan = await agent.plan(
        StrategyPlannerInput(
            session_state=SessionState(session_id=SessionId("session_strategy")),
            risk=_risk(RiskRoute.CRISIS),
        )
    )

    assert plan.primary_strategy == StrategyType.SAFETY_CHECK
    assert "crisis_protocol" in plan.reason
