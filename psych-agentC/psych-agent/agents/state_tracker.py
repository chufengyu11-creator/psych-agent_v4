"""State tracker implementations."""

from __future__ import annotations

from pathlib import Path

from llm.client import LLMClient
from schemas.state import (
    EmotionReport,
    GoalUpdate,
    StateDelta,
    StateOperation,
    StateTrackerInput,
    StrategyPreference,
    TopicUpdate,
)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "state_tracker.md"


class FakeStateTracker:
    """Small deterministic extractor for the first integration pipeline."""

    async def run(self, payload: StateTrackerInput) -> StateDelta:
        """Alias for extract_delta so the class satisfies Agent protocol."""

        return await self.extract_delta(payload)

    async def extract_delta(self, payload: StateTrackerInput) -> StateDelta:
        """Extract simple topic, goal, emotion, and preference deltas."""

        message_id = payload.current_message.id
        text = payload.current_message.content
        lower_text = text.lower()
        emotions: list[EmotionReport] = []
        if "焦虑" in text or "anxious" in lower_text:
            emotions.append(EmotionReport(label="焦虑", source_message_id=message_id))
        wants_action = (
            "建议" in text
            or "怎么做" in text
            or "next steps" in lower_text
            or "concrete" in lower_text
        )
        preferences: list[StrategyPreference] = []
        if wants_action:
            preferences.append(
                StrategyPreference(
                    operation=StateOperation.ADD,
                    value="希望获得具体、低压力的行动建议",
                    source_message_id=message_id,
                )
            )
        return StateDelta(
            explicit_user_request=text,
            topic_updates=[
                TopicUpdate(
                    operation=StateOperation.ADD,
                    topic="当前用户消息",
                    source_message_id=message_id,
                )
            ],
            goal_updates=[
                GoalUpdate(
                    operation=StateOperation.UPDATE,
                    goal="理解用户当前困扰并提供支持",
                    source_message_id=message_id,
                )
            ],
            reported_emotions=emotions,
            strategy_preferences=preferences,
        )


class StateTracker:
    """Model-backed extractor that emits only incremental StateDelta objects."""

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        model_name: str | None = None,
        prompt_template: str | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._model_name = model_name
        self._prompt_template = prompt_template or _PROMPT_PATH.read_text(encoding="utf-8")

    async def run(self, payload: StateTrackerInput) -> StateDelta:
        """Alias for extract_delta so the class satisfies Agent protocol."""

        return await self.extract_delta(payload)

    async def extract_delta(self, payload: StateTrackerInput) -> StateDelta:
        """Extract a StateDelta from the current turn without updating state."""

        try:
            result = await self._llm_client.generate_structured(
                self._build_prompt(payload),
                StateDelta,
                model_name=self._model_name,
                metadata={"agent": "state_tracker"},
            )
        except Exception:
            return _safe_empty_delta(payload)

        if not isinstance(result, StateDelta):
            return _safe_empty_delta(payload)
        return result

    def _build_prompt(self, payload: StateTrackerInput) -> str:
        recent_messages = "\n".join(
            f"- {message.role.value} #{message.sequence_number}: {message.content}"
            for message in payload.recent_messages
        )
        if not recent_messages:
            recent_messages = "- none"

        previous_state = payload.previous_state.model_dump_json()
        feedback = payload.feedback.model_dump_json() if payload.feedback is not None else "null"

        return (
            f"{self._prompt_template}\n\n"
            "## Runtime Input\n"
            "current_message:\n"
            f"id: {payload.current_message.id}\n"
            f"content: {payload.current_message.content}\n\n"
            "recent_messages:\n"
            f"{recent_messages}\n\n"
            "previous_state_json:\n"
            f"{previous_state}\n\n"
            "feedback_json:\n"
            f"{feedback}\n"
        )


def _safe_empty_delta(payload: StateTrackerInput) -> StateDelta:
    return StateDelta(explicit_user_request=payload.current_message.content)
