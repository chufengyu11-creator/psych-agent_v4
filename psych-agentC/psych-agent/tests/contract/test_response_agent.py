"""Contract tests for fake and model-backed response agents."""

from __future__ import annotations

import inspect
from typing import Any, cast

import pytest

import agents.response_agent as response_agent_module
from agents.response_agent import FakeResponseAgent, ResponseAgent
from llm.client import LLMClient
from llm.exceptions import LLMStructuredOutputError, LLMTimeoutError
from schemas.context import ContextSection, ResponseContext
from schemas.risk import RiskLevel, RiskResult, RiskRoute
from schemas.safety import DraftResponse
from schemas.state import ConversationPhase
from schemas.strategy import StrategyPlan, StrategyType
from tests.fixtures.memories import retrieved_work_stress_memories
from tests.fixtures.messages import work_stress_dialogue
from tests.fixtures.states import work_stress_state
from tests.fixtures.summaries import work_stress_rolling_summary


class StubLLMClient:
    """Small async stub for ResponseAgent contract tests."""

    def __init__(self, result: DraftResponse | Exception | object) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[DraftResponse],
        *,
        model_name: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> DraftResponse:
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
        return cast(DraftResponse, self.result)


def _risk() -> RiskResult:
    return RiskResult(
        risk_level=RiskLevel.LOW,
        categories=[],
        needs_clarification=False,
        route=RiskRoute.NORMAL,
        reason_codes=[],
        confidence=0.8,
    )


def _strategy(strategy: StrategyType = StrategyType.COLLABORATIVE_PROBLEM_SOLVING) -> StrategyPlan:
    return StrategyPlan(
        conversation_phase=ConversationPhase.INTERVENTION,
        primary_strategy=strategy,
        objective="和用户一起找到一个可承受的小步骤",
        reason="用户希望得到具体办法",
        avoid=["一次性给太多建议", "诊断", "药物建议"],
        expected_signals=["用户能选择一个小步骤"],
        switch_conditions=["出现安全风险", "用户拒绝行动建议"],
    )


def _context(
    strategy: StrategyType = StrategyType.COLLABORATIVE_PROBLEM_SOLVING,
) -> ResponseContext:
    return ResponseContext(
        system_policy="Provide psychological support. Do not diagnose.",
        risk=_risk(),
        session_state=work_stress_state(),
        strategy=_strategy(strategy),
        recent_messages=work_stress_dialogue(),
        rolling_summary=work_stress_rolling_summary(),
        memories=retrieved_work_stress_memories(),
        sections=[
            ContextSection(name="system_policy", content="Do not diagnose.", token_budget=256),
            ContextSection(
                name="strategy_plan",
                content=_strategy(strategy).model_dump_json(),
                token_budget=512,
            ),
        ],
    )


def _agent(llm: StubLLMClient) -> ResponseAgent:
    return ResponseAgent(cast(LLMClient, llm), prompt_template="response agent prompt")


@pytest.mark.asyncio
async def test_model_backed_response_agent_returns_draft_response() -> None:
    llm = StubLLMClient(
        DraftResponse(
            text="我们可以先选一个压力最低的小步骤来试试。",
            contains_action_suggestion=True,
        )
    )
    agent = _agent(llm)

    result = await agent.generate(_context())

    assert isinstance(result, DraftResponse)
    assert result.text


@pytest.mark.asyncio
async def test_model_backed_response_agent_calls_generate_structured() -> None:
    llm = StubLLMClient(DraftResponse(text="我听到了你的需要。"))
    agent = ResponseAgent(
        cast(LLMClient, llm),
        model_name="response-model",
        prompt_template="response agent prompt",
    )

    await agent.generate(_context())

    assert len(llm.calls) == 1
    assert llm.calls[0]["output_model"] is DraftResponse
    assert llm.calls[0]["model_name"] == "response-model"
    assert llm.calls[0]["metadata"] == {"agent": "response_agent"}
    assert "strategy_json" in llm.calls[0]["prompt"]
    assert "recent_messages_json" in llm.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_question_text_sets_asked_question_true() -> None:
    llm = StubLLMClient(
        DraftResponse(
            text="你更希望先梳理情绪，还是先制定行动计划？",
            asked_question=False,
        )
    )
    agent = _agent(llm)

    result = await agent.generate(_context(StrategyType.CLARIFICATION))

    assert result.asked_question is True


@pytest.mark.asyncio
async def test_action_suggestion_text_sets_action_metadata_true() -> None:
    llm = StubLLMClient(
        DraftResponse(
            text="我们可以先写下一句你想对领导说的开场白。",
            contains_action_suggestion=False,
        )
    )
    agent = _agent(llm)

    result = await agent.generate(_context())

    assert result.contains_action_suggestion is True


@pytest.mark.asyncio
async def test_referenced_memory_ids_are_filtered_to_context_memories() -> None:
    llm = StubLLMClient(
        DraftResponse(
            text="我会记得你之前想改善和直属领导的沟通。",
            referenced_memory_ids=["mem_001", "mem_missing", "mem_001"],
        )
    )
    agent = _agent(llm)

    result = await agent.generate(_context())

    assert result.referenced_memory_ids == ["mem_001"]


@pytest.mark.asyncio
async def test_timeout_returns_safe_fallback_draft() -> None:
    llm = StubLLMClient(LLMTimeoutError("provider timeout"))
    agent = _agent(llm)

    result = await agent.generate(_context(StrategyType.CLARIFICATION))

    assert isinstance(result, DraftResponse)
    assert result.asked_question is True
    assert result.referenced_memory_ids == []


@pytest.mark.asyncio
async def test_invalid_structured_output_returns_fallback_draft() -> None:
    llm = StubLLMClient(LLMStructuredOutputError("invalid json"))
    agent = _agent(llm)

    result = await agent.generate(_context(StrategyType.ACTION_PLANNING))

    assert isinstance(result, DraftResponse)
    assert result.contains_action_suggestion is True


@pytest.mark.asyncio
async def test_non_draft_model_result_returns_fallback_draft() -> None:
    llm = StubLLMClient({"text": "not a DraftResponse instance"})
    agent = _agent(llm)

    result = await agent.generate(_context(StrategyType.REFLECTIVE_LISTENING))

    assert isinstance(result, DraftResponse)
    assert result.text


@pytest.mark.asyncio
async def test_model_backed_response_agent_does_not_modify_context() -> None:
    context = _context()
    before = context.model_dump()
    llm = StubLLMClient(DraftResponse(text="我们可以先选一个很小的下一步。"))
    agent = _agent(llm)

    await agent.generate(context)

    assert context.model_dump() == before


@pytest.mark.asyncio
async def test_fake_response_agent_original_behavior_still_works() -> None:
    agent = FakeResponseAgent()

    clarification = await agent.generate(_context(StrategyType.CLARIFICATION))
    action = await agent.generate(_context(StrategyType.COLLABORATIVE_PROBLEM_SOLVING))

    assert clarification.asked_question is True
    assert action.contains_action_suggestion is True
def test_response_agent_module_does_not_write_database_or_state() -> None:
    source = inspect.getsource(response_agent_module)

    assert "Repository" not in source
    assert "repository" not in source
    assert ".save(" not in source
    assert "state_repository" not in source
    assert "memory_repository" not in source
