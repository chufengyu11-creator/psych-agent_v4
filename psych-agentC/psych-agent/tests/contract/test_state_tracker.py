"""Contract tests for fake and model-backed state trackers."""

from __future__ import annotations

import inspect
from typing import Any, cast

import pytest

import agents.state_tracker as state_tracker_module
from agents.state_tracker import FakeStateTracker, StateTracker
from llm.client import LLMClient
from llm.exceptions import LLMStructuredOutputError, LLMTimeoutError
from schemas.common import MessageId, SessionId
from schemas.messages import Message, MessageRole
from schemas.state import (
    EmotionReport,
    GoalUpdate,
    Hypothesis,
    SessionState,
    StateDelta,
    StateOperation,
    StateTrackerInput,
    StrategyPreference,
    TopicUpdate,
    UserCorrection,
)


class StubLLMClient:
    """Small async stub for StateTracker contract tests."""

    def __init__(self, result: StateDelta | Exception | object) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[StateDelta],
        *,
        model_name: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> StateDelta:
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
        return cast(StateDelta, self.result)


def _message(
    content: str,
    sequence_number: int = 1,
    role: MessageRole = MessageRole.USER,
) -> Message:
    return Message(
        id=MessageId(f"msg_{sequence_number}"),
        session_id=SessionId("session_state_tracker"),
        role=role,
        content=content,
        sequence_number=sequence_number,
    )


def _input(content: str, recent_messages: list[Message] | None = None) -> StateTrackerInput:
    return StateTrackerInput(
        current_message=_message(content),
        previous_state=SessionState(session_id=SessionId("session_state_tracker")),
        recent_messages=recent_messages or [],
        feedback=None,
    )


def _rich_delta() -> StateDelta:
    message_id = MessageId("msg_1")
    return StateDelta(
        explicit_user_request="我不是焦虑，是生气；请少分析，多给下一步。",
        topic_updates=[
            TopicUpdate(
                operation=StateOperation.UPDATE,
                topic="与领导沟通冲突",
                source_message_id=message_id,
            )
        ],
        goal_updates=[
            GoalUpdate(
                operation=StateOperation.UPDATE,
                goal="获得下一步具体沟通行动",
                source_message_id=message_id,
            )
        ],
        reported_emotions=[EmotionReport(label="生气", source_message_id=message_id)],
        user_corrections=[
            UserCorrection(
                correction="用户纠正为生气而不是焦虑",
                replaces="焦虑",
                source_message_id=message_id,
            )
        ],
        strategy_preferences=[
            StrategyPreference(
                operation=StateOperation.ADD,
                value="少分析，多给具体下一步",
                source_message_id=message_id,
            )
        ],
        hypotheses=[
            Hypothesis(
                value="用户可能希望降低情绪分析比例",
                source_message_id=message_id,
                confidence=0.62,
            )
        ],
    )


def _agent(llm: StubLLMClient) -> StateTracker:
    return StateTracker(cast(LLMClient, llm), prompt_template="state tracker prompt")


@pytest.mark.asyncio
async def test_model_backed_state_tracker_returns_state_delta() -> None:
    llm = StubLLMClient(_rich_delta())
    agent = _agent(llm)

    result = await agent.extract_delta(_input("我不是焦虑，是生气；请少分析，多给下一步。"))

    assert isinstance(result, StateDelta)
    assert result.explicit_user_request is not None


@pytest.mark.asyncio
async def test_model_backed_state_tracker_calls_generate_structured() -> None:
    llm = StubLLMClient(_rich_delta())
    agent = StateTracker(
        cast(LLMClient, llm),
        model_name="state-model",
        prompt_template="state tracker prompt",
    )

    await agent.extract_delta(_input("我想知道下一步怎么做。"))

    assert len(llm.calls) == 1
    assert llm.calls[0]["output_model"] is StateDelta
    assert llm.calls[0]["model_name"] == "state-model"
    assert llm.calls[0]["metadata"] == {"agent": "state_tracker"}


@pytest.mark.asyncio
async def test_model_backed_state_tracker_can_extract_all_delta_fields() -> None:
    llm = StubLLMClient(_rich_delta())
    agent = _agent(llm)

    result = await agent.extract_delta(_input("我不是焦虑，是生气；请少分析，多给下一步。"))

    assert result.topic_updates[0].topic == "与领导沟通冲突"
    assert result.goal_updates[0].goal == "获得下一步具体沟通行动"
    assert result.reported_emotions[0].label == "生气"
    assert result.user_corrections[0].replaces == "焦虑"
    assert result.strategy_preferences[0].value == "少分析，多给具体下一步"
    assert result.hypotheses[0].confidence == 0.62


@pytest.mark.asyncio
async def test_model_backed_state_tracker_prompt_includes_context() -> None:
    llm = StubLLMClient(_rich_delta())
    agent = _agent(llm)

    await agent.extract_delta(
        _input(
            "我想知道下一步怎么做。",
            recent_messages=[_message("前面提到和领导开会紧张。", 2, MessageRole.ASSISTANT)],
        )
    )

    prompt = llm.calls[0]["prompt"]
    assert "previous_state_json" in prompt
    assert "recent_messages" in prompt
    assert "前面提到和领导开会紧张" in prompt


@pytest.mark.asyncio
async def test_timeout_returns_safe_empty_delta() -> None:
    llm = StubLLMClient(LLMTimeoutError("provider timeout"))
    agent = _agent(llm)

    result = await agent.extract_delta(_input("我现在只想说这句话。"))

    assert result == StateDelta(explicit_user_request="我现在只想说这句话。")


@pytest.mark.asyncio
async def test_invalid_structured_output_returns_safe_empty_delta() -> None:
    llm = StubLLMClient(LLMStructuredOutputError("invalid json"))
    agent = _agent(llm)

    result = await agent.extract_delta(_input("请帮我整理一下。"))

    assert result == StateDelta(explicit_user_request="请帮我整理一下。")


@pytest.mark.asyncio
async def test_non_delta_model_result_returns_safe_empty_delta() -> None:
    llm = StubLLMClient({"session_id": "session_state_tracker"})
    agent = _agent(llm)

    result = await agent.extract_delta(_input("不要更新完整状态。"))

    assert result == StateDelta(explicit_user_request="不要更新完整状态。")


@pytest.mark.asyncio
async def test_model_backed_state_tracker_does_not_modify_input_object() -> None:
    payload = _input("我不是焦虑，是生气；请少分析，多给下一步。")
    before = payload.model_dump()
    llm = StubLLMClient(_rich_delta())
    agent = _agent(llm)

    await agent.extract_delta(payload)

    assert payload.model_dump() == before


@pytest.mark.asyncio
async def test_fake_state_tracker_original_behavior_still_works() -> None:
    agent = FakeStateTracker()

    result = await agent.extract_delta(_input("我很焦虑，想知道 next steps。"))

    assert result.explicit_user_request == "我很焦虑，想知道 next steps。"
    assert result.reported_emotions[0].label == "焦虑"
    assert result.strategy_preferences[0].value == "希望获得具体、低压力的行动建议"
@pytest.mark.asyncio
async def test_reported_emotions_include_source_message_id() -> None:
    llm = StubLLMClient(_rich_delta())
    agent = _agent(llm)

    result = await agent.extract_delta(_input("not anxiety, anger"))

    assert result.reported_emotions
    assert result.reported_emotions[0].source_message_id == MessageId("msg_1")


def test_state_tracker_module_does_not_access_repositories() -> None:
    source = inspect.getsource(state_tracker_module)

    assert "Repository" not in source
    assert "repository" not in source
    assert ".save(" not in source
    assert "state_repository" not in source
