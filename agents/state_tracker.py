"""State tracker implementations."""

import re
from pathlib import Path

from llm.structured_client import StructuredLLMClientProtocol
from schemas.common import MessageId
from schemas.state import (
    EmotionReport,
    GoalUpdate,
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


class FastStateTracker:
    """Deterministically extract explicit state needed by the current turn."""

    async def run(self, payload: StateTrackerInput) -> StateDelta:
        """Alias for extract_delta so the class satisfies Agent protocols."""

        return await self.extract_delta(payload)

    async def extract_delta(self, payload: StateTrackerInput) -> StateDelta:
        """Extract only direct, high-confidence statements without model inference."""

        text = payload.current_message.content
        message_id = payload.current_message.id
        session_goal = _explicit_session_goal(text)
        corrections = _explicit_corrections(payload)
        goal_updates = (
            [
                GoalUpdate(
                    operation=StateOperation.UPDATE,
                    goal=session_goal,
                    source_message_id=message_id,
                )
            ]
            if session_goal is not None
            else []
        )
        delta = StateDelta(
            explicit_user_request=text,
            current_turn_goal=_current_turn_goal(text),
            goal_updates=goal_updates,
            reported_emotions=_explicit_emotions(
                text,
                message_id,
                excluded_values={
                    correction.replaces
                    for correction in corrections
                    if correction.replaces is not None
                },
            ),
            user_corrections=corrections,
        )
        return _enforce_direct_user_request_delta(payload, delta)


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
        """Extract a StateDelta with the legacy safe fallback behavior."""

        try:
            return await self._extract_model_delta(
                payload,
                agent_name="state_tracker",
            )
        except Exception:
            return _safe_empty_delta(payload)

    async def extract_deep_delta(self, payload: StateTrackerInput) -> StateDelta:
        """Run deep extraction and expose failures to the background supervisor."""

        return await self._extract_model_delta(
            payload,
            agent_name="deep_state_tracker",
        )

    async def _extract_model_delta(
        self,
        payload: StateTrackerInput,
        *,
        agent_name: str,
    ) -> StateDelta:
        """Generate one model-backed delta for the selected execution path."""

        result = await self._llm_client.generate_structured(
            self._build_prompt(payload),
            StateDelta,
            model_name=self._model_name,
            metadata={"agent": agent_name},
        )
        return _enforce_direct_user_request_delta(payload, result)

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


_CURRENT_TURN_GOAL_MARKERS = (
    "\u6211\u73b0\u5728\u60f3",
    "\u73b0\u5728\u5148",
    "\u8fd9\u6b21\u6211\u60f3",
    "\u8fd9\u4e00\u8f6e\u6211\u60f3",
    "\u773c\u4e0b\u6211\u60f3",
    "right now i want",
    "for now i want",
    "this time i want",
)
_SESSION_GOAL_MARKERS = (
    "\u6211\u771f\u6b63\u60f3\u89e3\u51b3\u7684\u662f",
    "\u6211\u7684\u76ee\u6807\u662f",
    "\u63a5\u4e0b\u6765\u6211\u60f3\u91cd\u70b9\u89e3\u51b3",
    "\u6211\u60f3\u628a\u76ee\u6807\u6539\u6210",
    "my goal is",
    "what i really want to solve is",
    "i want to change the goal to",
    "i want to focus on",
)
_EMOTION_TERMS = (
    ("\u7126\u8651", ("\u7126\u8651", "anxious", "anxiety")),
    ("\u751f\u6c14", ("\u751f\u6c14", "\u6124\u6012", "angry", "anger")),
    ("\u96be\u8fc7", ("\u96be\u8fc7", "\u4f24\u5fc3", "sad", "sadness")),
    ("\u5bb3\u6015", ("\u5bb3\u6015", "\u6050\u60e7", "afraid", "fear")),
    ("\u65e0\u52a9", ("\u65e0\u52a9", "helpless")),
    ("\u75b2\u60eb", ("\u75b2\u60eb", "\u7d2f", "tired", "exhausted")),
    ("\u538b\u529b", ("\u538b\u529b", "stressed", "stress", "overwhelmed")),
)
_CHINESE_CORRECTION = re.compile(
    r"(?:\u4e0d\u662f|\u5e76\u4e0d\u662f)"
    r"(?P<old>[^，。；;]{1,60})"
    r"[，,\s]*(?:\u800c\u662f|\u662f)"
    r"(?P<new>[^，。；;]{1,60})"
)
_ENGLISH_CORRECTION = re.compile(
    r"\bnot\s+(?P<old>[^,.;]{1,60})"
    r"[,;\s]+(?:but|it(?:'s| is))\s+(?P<new>[^,.;]{1,60})",
    flags=re.IGNORECASE,
)


def _current_turn_goal(text: str) -> str | None:
    """Return an explicit immediate goal without inferring hidden intent."""

    lowered = text.casefold()
    if any(marker in text for marker in _CURRENT_TURN_GOAL_MARKERS[:5]):
        return text.strip()
    if any(marker in lowered for marker in _CURRENT_TURN_GOAL_MARKERS[5:]):
        return text.strip()
    session_goal = _explicit_session_goal(text)
    return text.strip() if session_goal is not None else None


def _explicit_session_goal(text: str) -> str | None:
    """Return a session-goal update only when the user marks it explicitly."""

    lowered = text.casefold()
    if any(marker in text for marker in _SESSION_GOAL_MARKERS[:4]):
        return text.strip()
    if any(marker in lowered for marker in _SESSION_GOAL_MARKERS[4:]):
        return text.strip()
    return None


def _explicit_emotions(
    text: str,
    message_id: MessageId,
    *,
    excluded_values: set[str],
) -> list[EmotionReport]:
    """Extract direct emotions while respecting explicit negations."""

    lowered = text.casefold()
    normalized_exclusions = {
        value.casefold().strip()
        for value in excluded_values
    }
    reports: list[EmotionReport] = []
    for label, terms in _EMOTION_TERMS:
        normalized_label = label.casefold()
        if any(
            normalized_label in value or value in normalized_label
            for value in normalized_exclusions
        ):
            continue
        if any(term.casefold() in lowered for term in terms):
            reports.append(
                EmotionReport(
                    label=label,
                    source_message_id=message_id,
                )
            )
    return reports


def _explicit_corrections(payload: StateTrackerInput) -> list[UserCorrection]:
    """Extract explicit old-to-new corrections from the current message."""

    text = payload.current_message.content
    matches = [
        *_CHINESE_CORRECTION.finditer(text),
        *_ENGLISH_CORRECTION.finditer(text),
    ]
    corrections: list[UserCorrection] = []
    for match in matches:
        old_value = _clean_correction_fragment(match.group("old"))
        new_value = _clean_correction_fragment(match.group("new"))
        if not old_value or not new_value:
            continue
        corrections.append(
            UserCorrection(
                correction=new_value,
                replaces=_matching_previous_value(payload, old_value) or old_value,
                source_message_id=payload.current_message.id,
            )
        )
    return corrections


def _clean_correction_fragment(value: str) -> str:
    """Normalize one short correction fragment."""

    return value.strip(" \t\r\n,.;:，。；：\"'")


def _matching_previous_value(
    payload: StateTrackerInput,
    fragment: str,
) -> str | None:
    """Resolve a correction fragment to one active value from prior state."""

    state = payload.previous_state
    candidates = [
        state.session_goal,
        *(
            item.value
            for collection in (
                state.active_topics,
                state.reported_emotions,
                state.user_preferences,
            )
            for item in collection
            if item.active
        ),
    ]
    normalized_fragment = fragment.casefold()
    for candidate in candidates:
        if candidate is None:
            continue
        normalized_candidate = candidate.casefold()
        if (
            normalized_fragment in normalized_candidate
            or normalized_candidate in normalized_fragment
        ):
            return candidate
    return None


def _read_prompt(path: Path, *, fallback: str) -> str:
    """Read an optional prompt file, falling back when it is absent or empty."""

    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return fallback


def _safe_empty_delta(payload: StateTrackerInput) -> StateDelta:
    """Preserve the user request when extraction fails."""

    return _enforce_direct_user_request_delta(
        payload,
        StateDelta(explicit_user_request=payload.current_message.content),
    )


def _enforce_direct_user_request_delta(
    payload: StateTrackerInput,
    delta: StateDelta,
) -> StateDelta:
    """Preserve explicit support-style requests even when model extraction misses them."""

    text = payload.current_message.content
    message_id = payload.current_message.id
    next_delta = delta.model_copy(deep=True)
    if next_delta.explicit_user_request is None:
        next_delta.explicit_user_request = text

    if _wants_concrete_support(text):
        _append_preference(
            next_delta,
            StrategyPreference(
                operation=StateOperation.ADD,
                value="prefers concrete, low-pressure, short-term action suggestions",
                source_message_id=message_id,
            ),
        )
    if _wants_emotional_support(text):
        _append_preference(
            next_delta,
            StrategyPreference(
                operation=StateOperation.ADD,
                value="prefers emotional support over practical planning",
                source_message_id=message_id,
            ),
        )
    if _rejects_structured_action(text):
        _append_preference(
            next_delta,
            StrategyPreference(
                operation=StateOperation.ADD,
                value="does not want rational analysis, checklists, or breathing exercises now",
                source_message_id=message_id,
            ),
        )
    if _is_time_limited(text):
        _append_preference(
            next_delta,
            StrategyPreference(
                operation=StateOperation.ADD,
                value="prefers concise responses because time is limited",
                source_message_id=message_id,
            ),
        )
    if _wants_short_term_emotion_relief(text) and not next_delta.goal_updates:
        next_delta.goal_updates.append(
            GoalUpdate(
                operation=StateOperation.UPDATE,
                goal="get short-term, immediately usable emotion relief support",
                source_message_id=message_id,
            )
        )
    return next_delta


def _append_preference(delta: StateDelta, preference: StrategyPreference) -> None:
    """Append a preference once, preserving model-supplied preferences."""

    if not any(item.value == preference.value for item in delta.strategy_preferences):
        delta.strategy_preferences.append(preference)


def _wants_concrete_support(text: str) -> bool:
    """Return whether the user explicitly asks for concrete advice or steps."""

    lowered = text.casefold()
    return any(
        term in lowered
        for term in (
            "建议",
            "方法",
            "步骤",
            "怎么做",
            "缓解",
            "行动",
            "next step",
            "next steps",
            "concrete",
            "specific",
            "advice",
        )
    )


def _is_time_limited(text: str) -> bool:
    """Return whether the user states that the answer must be brief or immediate."""

    lowered = text.casefold()
    return any(
        term in lowered
        for term in (
            "时间有限",
            "没时间",
            "立刻",
            "短期",
            "5分钟",
            "五分钟",
            "quick",
            "right now",
            "limited time",
        )
    )


def _wants_emotional_support(text: str) -> bool:
    """Return whether the user asks to stay with emotion rather than planning."""

    lowered = text.casefold()
    return any(
        term in lowered
        for term in (
            "情感支持",
            "情绪支持",
            "只是比较emotional",
            "比较emotional",
            "只是 emotional",
            "只是emotional",
            "陪我",
            "陪着我",
            "听我说",
            "倾听",
            "emotional support",
            "just emotional",
            "listen",
        )
    )


def _rejects_structured_action(text: str) -> bool:
    """Return whether the user rejects planning, lists, or a specific exercise."""

    lowered = text.casefold()
    return any(
        term in lowered
        for term in (
            "不想列清单",
            "不想做清单",
            "不要列清单",
            "不想要清单",
            "不想理性",
            "不要理性分析",
            "不是理性的事情",
            "不想深呼吸",
            "不太想深呼吸",
            "深呼吸效果一般",
            "效果一般般",
            "don't want a checklist",
            "do not want a checklist",
            "no checklist",
            "not rational",
            "don't want breathing",
            "do not want breathing",
        )
    )


def _wants_short_term_emotion_relief(text: str) -> bool:
    """Return whether the user asks for short-term emotion regulation support."""

    lowered = text.casefold()
    return _wants_concrete_support(text) and any(
        term in lowered
        for term in (
            "情绪",
            "压力",
            "焦虑",
            "缓解",
            "emotion",
            "stress",
            "anxiety",
            "relief",
        )
    )
