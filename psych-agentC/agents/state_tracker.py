"""State tracker implementations."""

from pathlib import Path

from llm.structured_client import StructuredLLMClientProtocol
from schemas.state import (
    EmotionReport,
    GoalUpdate,
    Hypothesis,
    StateDelta,
    StateOperation,
    StateTrackerInput,
    StrategyPreference,
    TopicUpdate,
    UserCorrection,
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
        if "\u7126\u8651" in text or "anxious" in lower_text:
            emotions.append(EmotionReport(label="\u7126\u8651", source_message_id=message_id))
        wants_action = (
            "\u5efa\u8bae" in text
            or "\u600e\u4e48\u505a" in text
            or "next steps" in lower_text
            or "concrete" in lower_text
        )
        preferences: list[StrategyPreference] = []
        if wants_action:
            preferences.append(
                StrategyPreference(
                    operation=StateOperation.ADD,
                    value=(
                        "\u5e0c\u671b\u83b7\u5f97\u5177\u4f53\u3001\u4f4e\u538b\u529b"
                        "\u7684\u884c\u52a8\u5efa\u8bae"
                    ),
                    source_message_id=message_id,
                )
            )
        return StateDelta(
            explicit_user_request=text,
            topic_updates=[
                TopicUpdate(
                    operation=StateOperation.ADD,
                    topic="\u5f53\u524d\u7528\u6237\u6d88\u606f",
                    source_message_id=message_id,
                )
            ],
            goal_updates=[
                GoalUpdate(
                    operation=StateOperation.UPDATE,
                    goal=(
                        "\u7406\u89e3\u7528\u6237\u5f53\u524d\u56f0\u6270"
                        "\u5e76\u63d0\u4f9b\u652f\u6301"
                    ),
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
        llm_client: StructuredLLMClientProtocol,
        *,
        model_name: str | None = None,
        prompt_template: str | None = None,
    ) -> None:
        """Create a state tracker with an injectable structured LLM client."""

        self._llm_client = llm_client
        self._model_name = model_name
        self._prompt_template = prompt_template or _read_prompt(
            _PROMPT_PATH,
            fallback=(
                "You extract only user-stated session-state changes. "
                "Return a StateDelta JSON object. Do not update SessionState directly."
            ),
        )

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
        return _normalize_source_ids(payload, result)

    def _build_prompt(self, payload: StateTrackerInput) -> str:
        """Render the state-extraction prompt from the typed payload."""

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
            "Important rules:\n"
            "- Use message IDs from the input for all source_message_id fields.\n"
            "- Put guesses in hypotheses, not in active facts.\n"
            "- Return empty lists when there is no update for a field.\n\n"
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


def _read_prompt(path: Path, *, fallback: str) -> str:
    """Read an optional prompt file, falling back when it is absent or empty."""

    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return fallback


def _safe_empty_delta(payload: StateTrackerInput) -> StateDelta:
    """Preserve the user request when extraction fails."""

    return StateDelta(explicit_user_request=payload.current_message.content)

def _normalize_source_ids(payload: StateTrackerInput, delta: StateDelta) -> StateDelta:
    """Bind extracted facts to the current user message instead of hallucinated IDs."""

    message_id = payload.current_message.id
    return StateDelta(
        explicit_user_request=delta.explicit_user_request,
        topic_updates=[
            TopicUpdate(
                operation=item.operation,
                topic=item.topic,
                source_message_id=message_id,
            )
            for item in delta.topic_updates
        ],
        goal_updates=[
            GoalUpdate(
                operation=item.operation,
                goal=item.goal,
                source_message_id=message_id,
            )
            for item in delta.goal_updates
        ],
        reported_emotions=[
            EmotionReport(label=item.label, source_message_id=message_id)
            for item in delta.reported_emotions
        ],
        user_corrections=[
            UserCorrection(
                correction=item.correction,
                replaces=item.replaces,
                source_message_id=message_id,
            )
            for item in delta.user_corrections
        ],
        strategy_preferences=[
            StrategyPreference(
                operation=item.operation,
                value=item.value,
                source_message_id=message_id,
            )
            for item in delta.strategy_preferences
        ],
        hypotheses=[
            Hypothesis(
                value=item.value,
                source_message_id=message_id,
                confidence=item.confidence,
            )
            for item in delta.hypotheses
        ],
    )
