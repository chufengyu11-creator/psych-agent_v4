"""Tests for model-backed risk, state, and strategy agents."""

from collections.abc import Mapping

import pytest

from agents.risk_agent import RiskAgent
from agents.state_tracker import StateTracker
from agents.strategy_planner import StrategyPlanner
from llm.structured_output import ModelT
from schemas.common import MessageId, SessionId
from schemas.feedback import FeedbackLabel, FeedbackResult, ObjectiveProgress, StrategyFit
from schemas.messages import Message, MessageRole
from schemas.risk import RiskInput, RiskLevel, RiskResult, RiskRoute
from schemas.state import SessionState, StateTrackerInput
from schemas.strategy import StrategyPlannerInput, StrategyType


class StubStructuredClient:
    """Fake structured client that returns queued dictionaries as Pydantic models."""

    def __init__(self, responses: list[dict[str, object]]) -> None:
        """Store structured responses for later calls."""

        self._responses = responses
        self.calls: list[tuple[str, object, Mapping[str, object] | None]] = []

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Return the next queued response as the requested model."""

        _ = model_name
        self.calls.append((prompt, output_model, metadata))
        return output_model.model_validate(self._responses.pop(0))


def _message(text: str) -> Message:
    """Create a user message fixture."""

    return Message(
        id=MessageId("msg_model_1"),
        session_id=SessionId("session_model"),
        role=MessageRole.USER,
        content=text,
        sequence_number=1,
    )


@pytest.mark.asyncio
async def test_model_backed_risk_agent_enforces_keyword_floor() -> None:
    """RiskAgent should not allow model output to miss obvious crisis keywords."""

    client = StubStructuredClient(
        [
            {
                "risk_level": "low",
                "categories": [],
                "needs_clarification": False,
                "route": "normal_dialogue",
                "reason_codes": ["model_low"],
                "confidence": 0.9,
            }
        ]
    )
    agent = RiskAgent(client, prompt_template="risk prompt")

    result = await agent.analyze(RiskInput(current_message=_message("I want to hurt myself.")))

    assert result.risk_level == RiskLevel.HIGH
    assert result.route == RiskRoute.CRISIS
    assert "self_harm" in result.categories
    assert "safety_keyword_floor" in result.reason_codes


@pytest.mark.asyncio
async def test_model_backed_state_tracker_returns_state_delta() -> None:
    """StateTracker should consume typed input and return a StateDelta."""

    client = StubStructuredClient(
        [
            {
                "explicit_user_request": "I want concrete next steps.",
                "topic_updates": [
                    {
                        "operation": "add",
                        "topic": "work communication stress",
                        "source_message_id": "msg_model_1",
                    }
                ],
                "goal_updates": [],
                "reported_emotions": [],
                "user_corrections": [],
                "strategy_preferences": [
                    {
                        "operation": "add",
                        "value": "prefers concrete suggestions",
                        "source_message_id": "msg_model_1",
                    }
                ],
                "hypotheses": [],
            }
        ]
    )
    message = _message("I want concrete next steps.")
    agent = StateTracker(client, prompt_template="state prompt")

    delta = await agent.extract_delta(
        StateTrackerInput(
            current_message=message,
            previous_state=SessionState(session_id=message.session_id),
        )
    )

    assert delta.topic_updates[0].topic == "work communication stress"
    assert delta.strategy_preferences[0].value == "prefers concrete suggestions"
    assert client.calls[0][2] == {"agent": "state_tracker"}


@pytest.mark.asyncio
async def test_model_backed_strategy_planner_avoids_bad_feedback_repeat() -> None:
    """StrategyPlanner should override reflective listening after poor feedback."""

    client = StubStructuredClient(
        [
            {
                "conversation_phase": "exploration",
                "primary_strategy": "reflective_listening",
                "objective": "keep reflecting",
                "reason": "model selected reflection",
                "avoid": [],
                "expected_signals": [],
                "switch_conditions": [],
            }
        ]
    )
    feedback = FeedbackResult(
        explicit_feedback=FeedbackLabel.NEGATIVE,
        strategy_fit=StrategyFit.POOR,
        observed_response="user rejected the prior response",
        objective_progress=ObjectiveProgress.NOT_ACHIEVED,
        recommended_adjustment="switch to concrete advice",
        confidence=0.8,
    )
    agent = StrategyPlanner(client, prompt_template="strategy prompt")

    plan = await agent.plan(
        StrategyPlannerInput(
            session_state=SessionState(session_id=SessionId("session_model")),
            risk=RiskResult(
                risk_level=RiskLevel.LOW,
                route=RiskRoute.NORMAL,
                reason_codes=["fixture"],
                confidence=0.9,
            ),
            feedback=feedback,
        )
    )

    assert plan.primary_strategy == StrategyType.COLLABORATIVE_PROBLEM_SOLVING
    assert "reflective_listening" in plan.reason