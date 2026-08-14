"""Run the model-assisted in-memory pipeline on the work-stress fixture corpus."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


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


def _model_outputs() -> list[str]:
    """Return queued Risk/State/Strategy outputs for three user turns."""

    return [
        _risk_json(),
        _state_delta_json("work communication feels tense"),
        _strategy_json("reflective_listening"),
        _risk_json(),
        _state_delta_json("user wants concrete next steps", "prefers concrete steps"),
        _strategy_json("collaborative_problem_solving"),
        _risk_json(),
        _state_delta_json("user prefers one small step", "prefers one small step"),
        _strategy_json("collaborative_problem_solving"),
    ]


async def main() -> None:
    """Run the fixture corpus through the model-assisted in-memory pipeline."""

    from runtime.factory import build_model_assisted_in_memory_orchestrator
    from schemas.common import SessionId, UserId
    from schemas.llm import LLMRequest, LLMResponse
    from schemas.messages import MessageRole
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

    user_messages = [
        message for message in work_stress_dialogue() if message.role == MessageRole.USER
    ]
    llm_client = QueueLLMClient(_model_outputs())
    orchestrator = build_model_assisted_in_memory_orchestrator(llm_client)

    for index, message in enumerate(user_messages, start=1):
        result = await orchestrator.handle_turn(
            user_id=UserId("user_fixture_corpus"),
            session_id=SessionId("session_fixture_corpus"),
            text=message.content,
        )
        print(f"\nTURN {index}")
        print(f"USER: {message.content}")
        print(f"ASSISTANT: {result.response}")
        print(f"STATE_VERSION: {result.state_version}")

    print(f"\nSTRUCTURED_LLM_CALLS: {len(llm_client.requests)}")


if __name__ == "__main__":
    asyncio.run(main())