"""Contract tests for model-backed state extraction."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from agents.state_tracker import StateTracker
from llm.structured_output import ModelT
from schemas.common import MessageId, SessionId
from schemas.messages import Message, MessageRole
from schemas.state import SessionState, StateDelta, StateTrackerInput


class StubStructuredClient:
    def __init__(self, result: dict[str, object] | Exception) -> None:
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
        if isinstance(self.result, Exception):
            raise self.result
        return output_model.model_validate(self.result)


def _input(text: str = "I feel anxious and want concrete steps.") -> StateTrackerInput:
    message = Message(
        id=MessageId("msg_state_current"),
        session_id=SessionId("session_state"),
        role=MessageRole.USER,
        content=text,
        sequence_number=2,
    )
    return StateTrackerInput(
        current_message=message,
        previous_state=SessionState(session_id=message.session_id),
    )


@pytest.mark.asyncio
async def test_state_tracker_returns_delta_and_rebinds_source_ids() -> None:
    agent = StateTracker(
        StubStructuredClient(
            {
                "explicit_user_request": "concrete steps",
                "topic_updates": [
                    {
                        "operation": "add",
                        "topic": "work anxiety",
                        "source_message_id": "hallucinated_id",
                    }
                ],
                "goal_updates": [],
                "reported_emotions": [
                    {"label": "anxious", "source_message_id": "hallucinated_id"}
                ],
                "user_corrections": [],
                "strategy_preferences": [],
                "hypotheses": [],
            }
        ),
        prompt_template="state prompt",
    )
    payload = _input()

    delta = await agent.extract_delta(payload)

    assert isinstance(delta, StateDelta)
    assert delta.topic_updates[0].source_message_id == payload.current_message.id
    assert delta.reported_emotions[0].source_message_id == payload.current_message.id


@pytest.mark.asyncio
async def test_state_tracker_failure_returns_minimal_delta() -> None:
    payload = _input("Please help me decide what to do.")
    agent = StateTracker(StubStructuredClient(RuntimeError("invalid output")), prompt_template="state")

    delta = await agent.extract_delta(payload)

    assert delta.explicit_user_request == payload.current_message.content
    assert delta.topic_updates == []
    assert delta.hypotheses == []
