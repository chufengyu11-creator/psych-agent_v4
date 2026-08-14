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


class RaisingStructuredClient:
    """Fake structured client that always raises."""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        _ = (prompt, output_model, model_name, metadata)
        raise RuntimeError("model unavailable")


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
async def test_risk_agent_model_failure_without_keywords_stays_normal() -> None:
    """Model failures should not turn ordinary distress into a safety route."""

    agent = RiskAgent(RaisingStructuredClient(), prompt_template="risk prompt")

    result = await agent.analyze(
        RiskInput(current_message=_message("我今天只是有点累，想聊一下工作。"))
    )

    assert result.risk_level == RiskLevel.LOW
    assert result.route == RiskRoute.NORMAL
    assert result.needs_clarification is False
    assert "keyword_only_fallback" in result.reason_codes


@pytest.mark.asyncio
async def test_risk_keyword_floor_ignores_assistant_safety_text() -> None:
    """Assistant safety wording in history should not re-trigger crisis routing."""

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
    current = _message("没有，我现在没有立即伤害自己的风险。")

    result = await agent.analyze(
        RiskInput(
            current_message=current,
            recent_messages=[
                Message(
                    id=MessageId("msg_assistant_safety"),
                    session_id=current.session_id,
                    role=MessageRole.ASSISTANT,
                    content="请先远离可能伤害自己的物品。",
                    sequence_number=1,
                ),
                current,
            ],
        )
    )

    assert result.risk_level == RiskLevel.LOW
    assert result.route == RiskRoute.NORMAL
    assert "safety_keyword_floor" not in result.reason_codes


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
async def test_state_tracker_preserves_direct_short_term_advice_request() -> None:
    """Direct requests for brief concrete help should become strategy preference."""

    client = StubStructuredClient(
        [
            {
                "explicit_user_request": None,
                "topic_updates": [],
                "goal_updates": [],
                "reported_emotions": [],
                "user_corrections": [],
                "strategy_preferences": [],
                "hypotheses": [],
            }
        ]
    )
    message = _message("能给些短期缓解自己的情绪的建议吗？我今天时间有限")
    agent = StateTracker(client, prompt_template="state prompt")

    delta = await agent.extract_delta(
        StateTrackerInput(
            current_message=message,
            previous_state=SessionState(session_id=message.session_id),
        )
    )

    assert delta.explicit_user_request == message.content
    assert [item.value for item in delta.strategy_preferences] == [
        "prefers concrete, low-pressure, short-term action suggestions",
        "prefers concise responses because time is limited",
    ]
    assert delta.goal_updates[0].goal == (
        "get short-term, immediately usable emotion relief support"
    )


@pytest.mark.asyncio
async def test_state_tracker_does_not_treat_upcoming_phd_year_as_time_limited() -> None:
    """Chinese upcoming-time wording should not become a support-style preference."""

    client = StubStructuredClient(
        [
            {
                "explicit_user_request": None,
                "topic_updates": [],
                "goal_updates": [],
                "reported_emotions": [],
                "user_corrections": [],
                "strategy_preferences": [],
                "hypotheses": [],
            }
        ]
    )
    message = _message("我现在博士四年级，马上博士五年级。")
    agent = StateTracker(client, prompt_template="state prompt")

    delta = await agent.extract_delta(
        StateTrackerInput(
            current_message=message,
            previous_state=SessionState(session_id=message.session_id),
        )
    )

    assert delta.strategy_preferences == []


@pytest.mark.asyncio
async def test_state_tracker_preserves_emotional_support_correction() -> None:
    """A rejection of rational planning should become a strategy preference."""

    client = StubStructuredClient(
        [
            {
                "explicit_user_request": None,
                "topic_updates": [],
                "goal_updates": [],
                "reported_emotions": [],
                "user_corrections": [],
                "strategy_preferences": [],
                "hypotheses": [],
            }
        ]
    )
    message = _message("不是，我不想列清单：这是理性的事情，我今天只是比较emotional。")
    agent = StateTracker(client, prompt_template="state prompt")

    delta = await agent.extract_delta(
        StateTrackerInput(
            current_message=message,
            previous_state=SessionState(session_id=message.session_id),
        )
    )

    assert [item.value for item in delta.strategy_preferences] == [
        "prefers emotional support over practical planning",
        "does not want rational analysis, checklists, or breathing exercises now",
    ]


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

    assert plan.primary_strategy == StrategyType.ACTION_PLANNING
    assert "exploration" in plan.reason


@pytest.mark.asyncio
async def test_strategy_planner_respects_concrete_time_limited_preference() -> None:
    """A model-selected exploration strategy should be overridden for direct advice requests."""

    client = StubStructuredClient(
        [
            {
                "conversation_phase": "exploration",
                "primary_strategy": "emotional_exploration",
                "objective": "ask another broad question",
                "reason": "model selected exploration",
                "avoid": [],
                "expected_signals": [],
                "switch_conditions": [],
            }
        ]
    )
    state = SessionState(
        session_id=SessionId("session_model"),
        session_goal="get short-term emotion relief support",
        user_preferences=[
            {
                "value": "prefers concise responses because time is limited",
                "source": {"message_id": "msg_model_1"},
            }
        ],
    )
    agent = StrategyPlanner(client, prompt_template="strategy prompt")

    plan = await agent.plan(
        StrategyPlannerInput(
            session_state=state,
            risk=RiskResult(
                risk_level=RiskLevel.LOW,
                route=RiskRoute.NORMAL,
                reason_codes=["fixture"],
                confidence=0.9,
            ),
        )
    )

    assert plan.primary_strategy == StrategyType.ACTION_PLANNING
    assert plan.conversation_phase.value == "intervention"
    assert "time limit" in " ".join(plan.avoid)


@pytest.mark.asyncio
async def test_strategy_planner_emotional_support_overrides_stale_action_preference() -> None:
    """A newer emotional-support preference should override stale practical preferences."""

    client = StubStructuredClient(
        [
            {
                "conversation_phase": "intervention",
                "primary_strategy": "action_planning",
                "objective": "give another step",
                "reason": "model selected action",
                "avoid": [],
                "expected_signals": [],
                "switch_conditions": [],
            }
        ]
    )
    state = SessionState(
        session_id=SessionId("session_model"),
        session_goal="情感支持",
        user_preferences=[
            {
                "value": "prefers concise responses because time is limited",
                "source": {"message_id": "msg_model_1"},
            },
            {
                "value": "需要情感支持，不要理性分析或列清单",
                "source": {"message_id": "msg_model_1"},
            },
        ],
    )
    agent = StrategyPlanner(client, prompt_template="strategy prompt")

    plan = await agent.plan(
        StrategyPlannerInput(
            session_state=state,
            risk=RiskResult(
                risk_level=RiskLevel.LOW,
                route=RiskRoute.NORMAL,
                reason_codes=["fixture"],
                confidence=0.9,
            ),
        )
    )

    assert plan.primary_strategy == StrategyType.EMOTIONAL_EXPLORATION
    assert plan.conversation_phase.value == "exploration"
    assert "checklists" in " ".join(plan.avoid)
