"""Contract tests for fake and model-backed output guards."""

from __future__ import annotations

from typing import Any, cast

import pytest

from agents.output_guard import FakeOutputGuard, OutputGuard
from llm.client import LLMClient
from llm.exceptions import LLMStructuredOutputError, LLMTimeoutError
from schemas.context import ContextSection, ResponseContext
from schemas.risk import RiskLevel, RiskResult, RiskRoute
from schemas.safety import DraftResponse, GuardDecision, GuardInput, GuardResult
from schemas.state import ConversationPhase
from schemas.strategy import StrategyPlan, StrategyType
from tests.fixtures.memories import retrieved_work_stress_memories
from tests.fixtures.messages import work_stress_dialogue
from tests.fixtures.states import work_stress_state


class StubLLMClient:
    """Small async stub for OutputGuard contract tests."""

    def __init__(self, result: GuardResult | Exception | object) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[GuardResult],
        *,
        model_name: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> GuardResult:
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
        return cast(GuardResult, self.result)


def _risk(route: RiskRoute = RiskRoute.NORMAL) -> RiskResult:
    return RiskResult(
        risk_level=RiskLevel.LOW if route == RiskRoute.NORMAL else RiskLevel.HIGH,
        categories=[] if route == RiskRoute.NORMAL else ["self_harm"],
        needs_clarification=route != RiskRoute.NORMAL,
        route=route,
        reason_codes=[],
        confidence=0.8,
    )


def _context(route: RiskRoute = RiskRoute.NORMAL) -> ResponseContext:
    strategy = StrategyPlan(
        conversation_phase=ConversationPhase.INTERVENTION,
        primary_strategy=StrategyType.COLLABORATIVE_PROBLEM_SOLVING,
        objective="和用户一起找到一个小步骤",
        reason="用户希望得到具体办法",
        avoid=["诊断", "药物建议"],
        expected_signals=["用户选择一个小步骤"],
        switch_conditions=["出现安全风险"],
    )
    return ResponseContext(
        system_policy="Provide support. Do not diagnose or provide medication advice.",
        risk=_risk(route),
        session_state=work_stress_state(),
        strategy=strategy,
        recent_messages=work_stress_dialogue(),
        memories=retrieved_work_stress_memories(),
        sections=[
            ContextSection(
                name="strategy_plan",
                content=strategy.model_dump_json(),
                token_budget=512,
            )
        ],
    )


def _input(text: str, *, route: RiskRoute = RiskRoute.NORMAL) -> GuardInput:
    return GuardInput(
        draft=DraftResponse(text=text),
        context=_context(route),
        risk=_risk(route),
    )


def _agent(llm: StubLLMClient) -> OutputGuard:
    return OutputGuard(cast(LLMClient, llm), prompt_template="output guard prompt")


@pytest.mark.asyncio
async def test_safe_draft_calls_model_and_allows() -> None:
    llm = StubLLMClient(GuardResult(decision=GuardDecision.ALLOW))
    agent = OutputGuard(
        cast(LLMClient, llm),
        model_name="guard-model",
        prompt_template="output guard prompt",
    )

    result = await agent.review(_input("我听到这件事很不容易，我们可以慢慢看下一步。"))

    assert result.decision == GuardDecision.ALLOW
    assert len(llm.calls) == 1
    assert llm.calls[0]["output_model"] is GuardResult
    assert llm.calls[0]["model_name"] == "guard-model"
    assert llm.calls[0]["metadata"] == {"agent": "output_guard"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "violation"),
    [
        ("你就是抑郁症。", "unsupported_diagnosis"),
        ("你可以把药量加到两倍。", "medication_advice"),
        ("我保证这样做一定能治好你。", "guaranteed_outcome"),
        ("只有我真正理解你，不需要再和其他人说。", "ai_dependency"),
        ("你应该远离你的家人，不要告诉朋友。", "isolate_from_support"),
        ("你可以去报复那个领导。", "unsafe_action_suggestion"),
        ("你的潜意识就是想失败。", "overcertain_psychological_interpretation"),
        ("这是系统 prompt 和内部状态。", "prompt_or_internal_state_leak"),
    ],
)
async def test_rule_layer_blocks_required_violation_types(text: str, violation: str) -> None:
    llm = StubLLMClient(GuardResult(decision=GuardDecision.ALLOW))
    agent = _agent(llm)

    result = await agent.review(_input(text))

    assert result.decision == GuardDecision.BLOCK
    assert violation in result.violations
    assert llm.calls == []


@pytest.mark.asyncio
async def test_crisis_route_with_ordinary_chat_routes_to_safety() -> None:
    llm = StubLLMClient(GuardResult(decision=GuardDecision.ALLOW))
    agent = _agent(llm)

    result = await agent.review(_input("我们先继续聊聊工作安排。", route=RiskRoute.CRISIS))

    assert result.decision == GuardDecision.ROUTE_TO_SAFETY
    assert "ordinary_dialogue_during_safety_route" in result.violations
    assert llm.calls == []


@pytest.mark.asyncio
async def test_unknown_referenced_memory_id_is_blocked() -> None:
    llm = StubLLMClient(GuardResult(decision=GuardDecision.ALLOW))
    agent = _agent(llm)
    payload = _input("我记得你之前说过这件事。")
    payload.draft.referenced_memory_ids.append("mem_missing")

    result = await agent.review(payload)

    assert result.decision == GuardDecision.BLOCK
    assert "inappropriate_memory_use" in result.violations


@pytest.mark.asyncio
async def test_model_rewrite_result_is_preserved() -> None:
    llm = StubLLMClient(
        GuardResult(
            decision=GuardDecision.REWRITE,
            violations=["overcertain_psychological_interpretation"],
            rewritten_response="这可能和压力有关，但我们可以先看最贴近你的部分。",
        )
    )
    agent = _agent(llm)

    result = await agent.review(_input("这听起来很重，我们可以一起看。"))

    assert result.decision == GuardDecision.REWRITE
    assert result.rewritten_response == "这可能和压力有关，但我们可以先看最贴近你的部分。"


@pytest.mark.asyncio
async def test_rewrite_without_text_becomes_block() -> None:
    llm = StubLLMClient(
        GuardResult(
            decision=GuardDecision.REWRITE,
            violations=["overcertain_psychological_interpretation"],
        )
    )
    agent = _agent(llm)

    result = await agent.review(_input("这听起来很重，我们可以一起看。"))

    assert result.decision == GuardDecision.BLOCK
    assert "missing_rewrite_text" in result.violations


@pytest.mark.asyncio
async def test_guard_timeout_defaults_to_block() -> None:
    llm = StubLLMClient(LLMTimeoutError("provider timeout"))
    agent = _agent(llm)

    result = await agent.review(_input("这是一条普通支持回复。"))

    assert result.decision == GuardDecision.BLOCK
    assert "output_guard_failure" in result.violations


@pytest.mark.asyncio
async def test_invalid_model_output_defaults_to_block() -> None:
    llm = StubLLMClient(LLMStructuredOutputError("invalid enum"))
    agent = _agent(llm)

    result = await agent.review(_input("这是一条普通支持回复。"))

    assert result.decision == GuardDecision.BLOCK
    assert "output_guard_failure" in result.violations


@pytest.mark.asyncio
async def test_non_guard_result_defaults_to_block() -> None:
    llm = StubLLMClient({"decision": "allow"})
    agent = _agent(llm)

    result = await agent.review(_input("这是一条普通支持回复。"))

    assert result.decision == GuardDecision.BLOCK
    assert "output_guard_failure" in result.violations


@pytest.mark.asyncio
async def test_model_backed_output_guard_does_not_modify_input_object() -> None:
    payload = _input("我听到这件事不容易，我们可以慢慢看。")
    before = payload.model_dump()
    llm = StubLLMClient(GuardResult(decision=GuardDecision.ALLOW))
    agent = _agent(llm)

    await agent.review(payload)

    assert payload.model_dump() == before


@pytest.mark.asyncio
async def test_fake_output_guard_original_behavior_still_works() -> None:
    agent = FakeOutputGuard()

    blocked = await agent.review(_input("我可以诊断你是某种问题。"))
    allowed = await agent.review(_input("我听到你现在很不容易。"))

    assert blocked.decision == GuardDecision.BLOCK
    assert allowed.decision == GuardDecision.ALLOW
