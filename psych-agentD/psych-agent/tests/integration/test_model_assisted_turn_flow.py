"""Integration tests for the model-assisted in-memory turn pipeline."""

import pytest

from runtime.factory import build_model_assisted_in_memory_orchestrator
from schemas.common import SessionId, UserId
from schemas.llm import LLMRequest, LLMResponse
from schemas.messages import ChatTurnResult, MessageRole
from tests.fixtures.messages import work_stress_dialogue


class QueueLLMClient:
    """Base LLM client that returns queued structured JSON strings."""

    def __init__(self, responses: list[str]) -> None:
        """Store raw LLM responses for later calls."""

        self._responses = responses
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Return the next queued response as a normal LLMResponse."""

        self.requests.append(request)
        return LLMResponse(
            content=self._responses.pop(0),
            model_name=request.model_name,
            finish_reason="stop",
        )


@pytest.mark.asyncio
async def test_model_assisted_pipeline_runs_one_turn_with_structured_subagents() -> None:
    """Risk, state, and strategy can run through the structured LLM adapter."""

    llm_client = QueueLLMClient(
        [
            """
            {
              "risk_level": "low",
              "categories": [],
              "needs_clarification": false,
              "route": "normal_dialogue",
              "reason_codes": ["model_low"],
              "confidence": 0.91
            }
            """,
            """
            {
              "explicit_user_request": "I feel stuck at work and want concrete help.",
              "topic_updates": [],
              "goal_updates": [],
              "reported_emotions": [],
              "user_corrections": [],
              "strategy_preferences": [],
              "hypotheses": []
            }
            """,
            """
            {
              "conversation_phase": "exploration",
              "primary_strategy": "reflective_listening",
              "objective": "Help the user clarify the current work stressor.",
              "reason": "The user has not shared enough context yet.",
              "avoid": ["diagnosis", "medication advice"],
              "expected_signals": ["user shares more context"],
              "switch_conditions": ["user asks for concrete next steps", "risk escalates"]
            }
            """,
        ]
    )
    orchestrator = build_model_assisted_in_memory_orchestrator(llm_client)

    result = await orchestrator.handle_turn(
        user_id=UserId("user_model"),
        session_id=SessionId("session_model"),
        text="I feel stuck at work and want concrete help.",
    )

    assert result.status == "ok"
    assert result.state_version == 1
    assert len(llm_client.requests) == 3
    assert all(request.response_format == "json_object" for request in llm_client.requests)
    assert result.response


def _risk_json() -> str:
    """Return a low-risk structured model response."""

    return """
    {
      "risk_level": "low",
      "categories": [],
      "needs_clarification": false,
      "route": "normal_dialogue",
      "reason_codes": ["fixture_low"],
      "confidence": 0.91
    }
    """


def _feedback_json(
    label: str,
    progress: str,
    fit: str,
    message_id: str,
    quote: str,
) -> str:
    """Return a grounded FeedbackEvaluator private draft response."""

    return f"""
    {{
      "explicit_feedback": "{label}",
      "objective_progress": "{progress}",
      "strategy_fit": "{fit}",
      "confidence": 0.9,
      "evidence_message_id": "{message_id}",
      "evidence_quote": "{quote}"
    }}
    """


def _state_delta_json(request: str, preference: str | None = None) -> str:
    """Return a StateDelta JSON response for one fixture user turn."""

    preference_block = ""
    if preference is not None:
        preference_block = f"""
        {{
          "operation": "add",
          "value": "{preference}",
          "source_message_id": "msg_1"
        }}
        """
    return f"""
    {{
      "explicit_user_request": "{request}",
      "topic_updates": [
        {{
          "operation": "add",
          "topic": "work communication stress",
          "source_message_id": "msg_1"
        }}
      ],
      "goal_updates": [],
      "reported_emotions": [],
      "user_corrections": [],
      "strategy_preferences": [{preference_block}],
      "hypotheses": []
    }}
    """


def _strategy_json(strategy: str) -> str:
    """Return a StrategyPlan JSON response for one fixture user turn."""

    phase = "intervention" if strategy == "collaborative_problem_solving" else "exploration"
    return f"""
    {{
      "conversation_phase": "{phase}",
      "primary_strategy": "{strategy}",
      "objective": "keep the support focused and concrete",
      "reason": "fixture strategy for corpus smoke test",
      "avoid": ["diagnosis", "medication advice"],
      "expected_signals": ["user can continue the conversation"],
      "switch_conditions": ["risk escalates", "strategy does not fit"]
    }}
    """


@pytest.mark.asyncio
async def test_model_assisted_pipeline_runs_work_stress_fixture_corpus() -> None:
    """The previous multi-turn fixture corpus should run through the model path."""

    user_messages = [
        message for message in work_stress_dialogue() if message.role == MessageRole.USER
    ]
    llm_client = QueueLLMClient(
        [
            _risk_json(),
            _state_delta_json("work communication feels tense"),
            _strategy_json("reflective_listening"),
            _feedback_json(
                "negative",
                "not_achieved",
                "poor",
                "msg_3",
                "我不想继续分析情绪",
            ),
            _risk_json(),
            _state_delta_json("user wants concrete next steps", "prefers concrete steps"),
            _strategy_json("collaborative_problem_solving"),
            _feedback_json(
                "mixed",
                "partial",
                "mixed",
                "msg_5",
                "可以，而且我希望建议不要一次太多",
            ),
            _risk_json(),
            _state_delta_json("user prefers one small step", "prefers one small step"),
            _strategy_json("collaborative_problem_solving"),
        ]
    )
    orchestrator = build_model_assisted_in_memory_orchestrator(llm_client)

    results: list[ChatTurnResult] = []
    for message in user_messages:
        results.append(
            await orchestrator.handle_turn(
                user_id=UserId("user_fixture_corpus"),
                session_id=SessionId("session_fixture_corpus"),
                text=message.content,
            )
        )

    assert len(results) == 3
    assert [result.state_version for result in results] == [1, 2, 3]
    assert all(result.status == "ok" for result in results)
    assert len(llm_client.requests) == 11
    assert all(request.response_format == "json_object" for request in llm_client.requests)
    assert results[-1].response
