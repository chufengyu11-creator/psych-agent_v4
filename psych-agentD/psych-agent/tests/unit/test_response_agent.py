"""Unit tests for response generation through the LLM client boundary."""

from agents.response_agent import FakeResponseAgent, ResponseAgent
from llm.fake_client import FakeLLMClient
from llm.prompt_renderer import PromptRenderer
from schemas.context import ResponseContext
from schemas.risk import RiskLevel, RiskResult, RiskRoute
from schemas.state import ConversationPhase
from schemas.strategy import StrategyPlan, StrategyType
from services.context_builder import ContextBuilder
from tests.fixtures.memories import retrieved_work_stress_memories
from tests.fixtures.messages import work_stress_dialogue
from tests.fixtures.states import work_stress_state
from tests.fixtures.summaries import work_stress_rolling_summary


def _risk_result() -> RiskResult:
    """Return a low-risk fixture for response-agent tests."""

    return RiskResult(
        risk_level=RiskLevel.LOW,
        route=RiskRoute.NORMAL,
        reason_codes=["fixture_low_risk"],
        confidence=0.91,
    )


def _strategy_plan(strategy_type: StrategyType) -> StrategyPlan:
    """Return a strategy fixture for response-agent tests."""

    return StrategyPlan(
        conversation_phase=ConversationPhase.INTERVENTION,
        primary_strategy=strategy_type,
        objective="test objective",
        reason="test reason",
        avoid=["avoid overloading the user"],
        expected_signals=["user can choose one next step"],
        switch_conditions=["risk escalates"],
    )


async def _response_context(strategy_type: StrategyType) -> ResponseContext:
    """Build a realistic response context from shared fake data."""

    return await ContextBuilder().build(
        session_state=work_stress_state(),
        rolling_summary=work_stress_rolling_summary(),
        recent_messages=work_stress_dialogue(),
        memories=retrieved_work_stress_memories(),
        strategy=_strategy_plan(strategy_type),
        risk=_risk_result(),
    )


async def test_prompt_renderer_preserves_context_section_order() -> None:
    """Rendered prompts should keep ContextBuilder section priority order."""

    context = await _response_context(StrategyType.COLLABORATIVE_PROBLEM_SOLVING)
    request = PromptRenderer(model_name="fixture-model").render(context)

    assert request.model_name == "fixture-model"
    assert request.messages[0].role == "system"
    user_prompt = request.messages[1].content
    positions = [
        user_prompt.index(f"## {section.name} ")
        for section in context.sections
    ]

    assert positions == sorted(positions)
    assert "token_budget=" in user_prompt
    assert "collaborative_problem_solving" in user_prompt


async def test_response_agent_calls_llm_client_and_returns_draft() -> None:
    """ResponseAgent should consume ResponseContext through the LLMClient."""

    response_text = "\u6211\u542c\u5230\u4f60\u60f3\u5148\u627e\u4e00\u4e2a\u5c0f\u6b65\u9aa4\uff1f"
    fake_client = FakeLLMClient(response_text=response_text)
    agent = ResponseAgent(llm_client=fake_client)
    context = await _response_context(StrategyType.COLLABORATIVE_PROBLEM_SOLVING)

    draft = await agent.generate(context)

    assert draft.text == response_text
    assert draft.asked_question is True
    assert draft.contains_action_suggestion is True
    assert draft.referenced_memory_ids
    assert len(fake_client.requests) == 1
    assert "long_term_memory" in fake_client.requests[0].messages[1].content


async def test_fake_response_agent_keeps_clarification_behavior() -> None:
    """Default fake model should still support the existing clarification path."""

    context = await _response_context(StrategyType.CLARIFICATION)

    draft = await FakeResponseAgent().generate(context)

    assert "\u6821\u51c6" in draft.text
    assert draft.asked_question is True
    assert draft.contains_action_suggestion is False


async def test_fake_response_agent_keeps_action_behavior() -> None:
    """Default fake model should still support the action-planning path."""

    context = await _response_context(StrategyType.COLLABORATIVE_PROBLEM_SOLVING)

    draft = await FakeResponseAgent().generate(context)

    assert "\u5f88\u5c0f" in draft.text
    assert draft.asked_question is False
    assert draft.contains_action_suggestion is True