"""Unit tests for ContextBuilder."""

from copy import deepcopy

from schemas.risk import RiskLevel, RiskResult, RiskRoute
from schemas.state import ConversationPhase
from schemas.strategy import StrategyPlan, StrategyType
from services.context_builder import DEFAULT_SYSTEM_POLICY, ContextBuilder
from services.token_budget import DEFAULT_BUDGETS, TokenBudgetManager
from tests.fixtures.memories import retrieved_work_stress_memories
from tests.fixtures.messages import work_stress_dialogue
from tests.fixtures.states import work_stress_state
from tests.fixtures.summaries import work_stress_rolling_summary


def _risk_result() -> RiskResult:
    """Return a low-risk fixture for context-builder tests."""

    return RiskResult(
        risk_level=RiskLevel.LOW,
        route=RiskRoute.NORMAL,
        reason_codes=["fixture_low_risk"],
        confidence=0.91,
    )


def _strategy_plan() -> StrategyPlan:
    """Return a strategy fixture for context-builder tests."""

    return StrategyPlan(
        conversation_phase=ConversationPhase.INTERVENTION,
        primary_strategy=StrategyType.COLLABORATIVE_PROBLEM_SOLVING,
        objective="帮助用户选择一个低压力沟通步骤",
        reason="用户明确表示想要具体下一步",
        avoid=["一次性给太多建议"],
        expected_signals=["用户能选择一个可尝试的小步骤"],
        switch_conditions=["用户拒绝行动建议", "出现安全风险"],
    )


async def test_context_builder_builds_without_rolling_summary() -> None:
    """ContextBuilder should work when no rolling summary exists yet."""

    state = work_stress_state()
    messages = work_stress_dialogue()
    memories = retrieved_work_stress_memories()
    strategy = _strategy_plan()
    risk = _risk_result()

    context = await ContextBuilder().build(
        session_state=state,
        rolling_summary=None,
        recent_messages=messages,
        memories=memories,
        strategy=strategy,
        risk=risk,
    )

    assert context.system_policy == DEFAULT_SYSTEM_POLICY
    assert context.rolling_summary is None
    assert context.recent_messages == messages
    assert context.memories == memories
    assert [section.name for section in context.sections] == [
        "system_policy",
        "risk_state",
        "session_state",
        "strategy_plan",
        "recent_messages",
        "long_term_memory",
    ]


async def test_context_builder_includes_rolling_summary_in_stable_order() -> None:
    """Rolling summary should be inserted before recent raw messages."""

    context = await ContextBuilder().build(
        session_state=work_stress_state(),
        rolling_summary=work_stress_rolling_summary(),
        recent_messages=work_stress_dialogue(),
        memories=retrieved_work_stress_memories(),
        strategy=_strategy_plan(),
        risk=_risk_result(),
    )

    assert [section.name for section in context.sections] == [
        "system_policy",
        "risk_state",
        "session_state",
        "strategy_plan",
        "rolling_summary",
        "recent_messages",
        "long_term_memory",
    ]
    summary_section = next(
        section for section in context.sections if section.name == "rolling_summary"
    )
    assert "直属领导" in summary_section.content


async def test_context_builder_adds_budget_to_each_section() -> None:
    """Every context section should carry a configured token budget."""

    context = await ContextBuilder().build(
        session_state=work_stress_state(),
        rolling_summary=work_stress_rolling_summary(),
        recent_messages=work_stress_dialogue(),
        memories=retrieved_work_stress_memories(),
        strategy=_strategy_plan(),
        risk=_risk_result(),
    )

    assert all(section.token_budget > 0 for section in context.sections)
    assert {
        section.name: section.token_budget for section in context.sections
    } == {
        "system_policy": DEFAULT_BUDGETS["system_policy"],
        "risk_state": DEFAULT_BUDGETS["risk_state"],
        "session_state": DEFAULT_BUDGETS["session_state"],
        "strategy_plan": DEFAULT_BUDGETS["strategy_plan"],
        "rolling_summary": DEFAULT_BUDGETS["rolling_summary"],
        "recent_messages": DEFAULT_BUDGETS["recent_messages"],
        "long_term_memory": DEFAULT_BUDGETS["long_term_memory"],
    }


async def test_context_builder_uses_custom_token_budget() -> None:
    """Custom token budgets should flow into generated sections."""

    builder = ContextBuilder(TokenBudgetManager({"long_term_memory": 123}))

    context = await builder.build(
        session_state=work_stress_state(),
        rolling_summary=None,
        recent_messages=work_stress_dialogue(),
        memories=retrieved_work_stress_memories(),
        strategy=_strategy_plan(),
        risk=_risk_result(),
    )

    memory_section = next(
        section for section in context.sections if section.name == "long_term_memory"
    )
    assert memory_section.token_budget == 123


async def test_context_builder_section_content_contains_expected_inputs() -> None:
    """Risk, messages, memory, state, and strategy should be visible in sections."""

    context = await ContextBuilder().build(
        session_state=work_stress_state(),
        rolling_summary=work_stress_rolling_summary(),
        recent_messages=work_stress_dialogue(),
        memories=retrieved_work_stress_memories(),
        strategy=_strategy_plan(),
        risk=_risk_result(),
    )
    sections = {section.name: section.content for section in context.sections}

    assert "normal_dialogue" in sections["risk_state"]
    assert "和直属领导沟通紧张" in sections["session_state"]
    assert "collaborative_problem_solving" in sections["strategy_plan"]
    assert "我不想继续分析情绪" in sections["recent_messages"]
    assert "每次只给一个小步骤" in sections["long_term_memory"]


async def test_context_builder_does_not_mutate_inputs() -> None:
    """Building context should not mutate state, messages, memories, or strategy."""

    state = work_stress_state()
    summary = work_stress_rolling_summary()
    messages = work_stress_dialogue()
    memories = retrieved_work_stress_memories()
    strategy = _strategy_plan()
    risk = _risk_result()
    before = {
        "state": state.model_dump(),
        "summary": summary.model_dump(),
        "messages": [message.model_dump() for message in messages],
        "memories": memories.model_dump(),
        "strategy": strategy.model_dump(),
        "risk": risk.model_dump(),
    }
    before_copy = deepcopy(before)

    await ContextBuilder().build(
        session_state=state,
        rolling_summary=summary,
        recent_messages=messages,
        memories=memories,
        strategy=strategy,
        risk=risk,
    )

    after = {
        "state": state.model_dump(),
        "summary": summary.model_dump(),
        "messages": [message.model_dump() for message in messages],
        "memories": memories.model_dump(),
        "strategy": strategy.model_dump(),
        "risk": risk.model_dump(),
    }
    assert after == before_copy
