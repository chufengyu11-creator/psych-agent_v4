"""Session finalizer implementations for close-session memory extraction."""

import json
from pathlib import Path
from typing import TypeVar

from llm.structured_client import StructuredLLMClientProtocol
from schemas.common import MessageId
from schemas.intervention import InterventionRecord, InterventionStatus
from schemas.memory import (
    MemoryCandidate,
    MemoryOperation,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from schemas.messages import Message, MessageRole
from schemas.risk import RiskLevel
from schemas.summary import (
    ActionItem,
    SessionFinalizerInput,
    SessionFinalizerResult,
    StrategyOutcome,
)
from services.claim_safety import first_unsupported_claim_reason

ItemT = TypeVar("ItemT")
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "session_finalizer.md"

_SMALL_STEP_TERMS = (
    "one small step",
    "one step at a time",
    "small step",
    "\u6bcf\u6b21\u4e00\u4e2a\u5c0f\u6b65\u9aa4",
    "\u4e00\u4e2a\u5c0f\u6b65\u9aa4",
    "\u4e00\u6b65\u4e00\u6b65",
)
_TOO_MANY_SUGGESTIONS_TERMS = (
    "too many suggestions",
    "too much advice",
    "too many steps",
    "all the suggestions at once",
    "\u4e0d\u8981\u4e00\u6b21\u592a\u591a",
    "\u4e0d\u60f3\u4e00\u6b21\u592a\u591a",
)
_CONCRETE_STEP_TERMS = (
    "concrete next steps",
    "next step",
    "\u4e0b\u4e00\u6b65\u5177\u4f53",
    "\u5177\u4f53\u600e\u4e48\u505a",
)


class FakeSessionFinalizer:
    """Deterministic finalizer for session-close smoke tests."""

    async def run(self, payload: SessionFinalizerInput) -> SessionFinalizerResult:
        """Alias for finalize so callers can treat it like an agent."""

        return await self.finalize(payload)

    async def finalize(self, payload: SessionFinalizerInput) -> SessionFinalizerResult:
        """Build a conservative final session result from typed inputs."""

        action_items = _action_items(payload.messages)
        candidate_memories = _candidate_memories(payload.messages)
        strategy_outcomes = _strategy_outcomes(payload.interventions)
        source_message_ids = _dedupe(
            [
                *_summary_source_ids(payload),
                *(
                    message_id
                    for item in action_items
                    for message_id in item.source_message_ids
                ),
                *(
                    message_id
                    for candidate in candidate_memories
                    for message_id in candidate.source_message_ids
                ),
                *(
                    message_id
                    for outcome in strategy_outcomes
                    for message_id in outcome.source_message_ids
                ),
            ]
        )
        return SessionFinalizerResult(
            session_id=payload.session_id,
            session_summary=_session_summary(payload),
            goal_updates=_goal_updates(payload),
            unfinished_topics=_unfinished_topics(payload),
            action_items=action_items,
            candidate_memories=candidate_memories,
            strategy_outcomes=strategy_outcomes,
            risk_events=_risk_events(payload),
            source_message_ids=source_message_ids,
        )


class SessionFinalizer:
    """Model-backed session finalizer with deterministic fallback behavior."""

    def __init__(
        self,
        llm_client: StructuredLLMClientProtocol | None = None,
        *,
        model_name: str | None = None,
        fallback: FakeSessionFinalizer | None = None,
        prompt_template: str | None = None,
    ) -> None:
        """Create a finalizer with an optional structured model dependency."""

        self._llm_client = llm_client
        self._model_name = model_name
        self._fallback = fallback or FakeSessionFinalizer()
        self._prompt_template = prompt_template or _read_prompt(
            _PROMPT_PATH,
            fallback=(
                "Finalize the session from grounded inputs. Return only a "
                "SessionFinalizerResult JSON object."
            ),
        )

    async def run(self, payload: SessionFinalizerInput) -> SessionFinalizerResult:
        """Alias for finalize so callers can treat it like an agent."""

        return await self.finalize(payload)

    async def finalize(self, payload: SessionFinalizerInput) -> SessionFinalizerResult:
        """Finalize through the model, falling back on any unsafe or invalid result."""

        if self._llm_client is None:
            return await self._fallback.finalize(payload)
        try:
            result = SessionFinalizerResult.model_validate(
                await self._llm_client.generate_structured(
                    self._build_prompt(payload),
                    SessionFinalizerResult,
                    model_name=self._model_name,
                    metadata={"agent": "session_finalizer"},
                )
            )
        except Exception:
            return await self._fallback.finalize(payload)
        if not _validate_model_result(result, payload):
            return await self._fallback.finalize(payload)
        return _normalize_model_result(result, payload)

    def _build_prompt(self, payload: SessionFinalizerInput) -> str:
        """Render the typed finalizer input as grounded JSON context."""

        payload_json = json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        return (
            f"{self._prompt_template}\n\n"
            "Rules:\n"
            "- Candidate memories must be explicit and grounded.\n"
            "- Cite only source_message_ids present in the input.\n"
            "- Only evaluated interventions may have strategy outcomes.\n\n"
            f"INPUT_JSON:\n{payload_json}"
        )


def _read_prompt(path: Path, *, fallback: str) -> str:
    """Read an optional prompt file, falling back when absent or empty."""

    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return fallback


def _allowed_source_message_ids(payload: SessionFinalizerInput) -> list[MessageId]:
    """Collect every source message ID a model finalization may cite."""

    ids = [message.id for message in payload.messages]
    if payload.rolling_summary is not None:
        ids.extend(payload.rolling_summary.source_message_ids)
    ids.extend(item.source.message_id for item in payload.final_state.active_topics)
    ids.extend(item.source.message_id for item in payload.final_state.reported_emotions)
    ids.extend(item.source.message_id for item in payload.final_state.user_preferences)
    ids.extend(item.source.message_id for item in payload.final_state.open_questions)
    ids.extend(intervention.assistant_message_id for intervention in payload.interventions)
    return _dedupe(ids)


def _result_source_message_ids(result: SessionFinalizerResult) -> list[MessageId]:
    """Collect source IDs from every nested source-bearing result field."""

    ids = list(result.source_message_ids)
    ids.extend(
        message_id
        for action_item in result.action_items
        for message_id in action_item.source_message_ids
    )
    ids.extend(
        message_id
        for candidate in result.candidate_memories
        for message_id in candidate.source_message_ids
    )
    ids.extend(
        message_id
        for outcome in result.strategy_outcomes
        for message_id in outcome.source_message_ids
    )
    return _dedupe(ids)


def _validate_model_result(
    result: SessionFinalizerResult,
    payload: SessionFinalizerInput,
) -> bool:
    """Reject ungrounded, status-inconsistent, or unsupported model output."""

    allowed = set(_allowed_source_message_ids(payload))
    if any(
        message_id not in allowed
        for message_id in _result_source_message_ids(result)
    ):
        return False
    if first_unsupported_claim_reason(_result_text_fields(result)) is not None:
        return False
    if _has_top_level_factual_content(result) and not result.source_message_ids:
        return False
    if any(
        item.content.strip() and not item.source_message_ids
        for item in result.action_items
    ):
        return False
    if any(not candidate.source_message_ids for candidate in result.candidate_memories):
        return False
    if not _has_valid_explicit_memory_sources(result, payload):
        return False
    if not _has_valid_strategy_outcomes(result, payload):
        return False
    return _has_valid_risk_events(result, payload)


def _has_top_level_factual_content(result: SessionFinalizerResult) -> bool:
    """Return whether fields without per-item provenance contain session facts."""

    return bool(
        result.session_summary.strip()
        or result.goal_updates
        or result.unfinished_topics
        or result.risk_events
    )


def _has_valid_explicit_memory_sources(
    result: SessionFinalizerResult,
    payload: SessionFinalizerInput,
) -> bool:
    """Require explicit-user candidates to cite at least one direct user message."""

    user_message_ids = {
        message.id for message in payload.messages if message.role == MessageRole.USER
    }
    return all(
        candidate.source_type != MemorySourceType.EXPLICIT_USER_STATEMENT
        or any(
            message_id in user_message_ids
            for message_id in candidate.source_message_ids
        )
        for candidate in result.candidate_memories
    )


def _has_valid_strategy_outcomes(
    result: SessionFinalizerResult,
    payload: SessionFinalizerInput,
) -> bool:
    """Bind every outcome to a matching evaluated intervention and its source."""

    evaluated_by_strategy: dict[str, list[InterventionRecord]] = {}
    for intervention in payload.interventions:
        if intervention.status != InterventionStatus.EVALUATED:
            continue
        evaluated_by_strategy.setdefault(intervention.strategy, []).append(intervention)
    for outcome in result.strategy_outcomes:
        matching_interventions = evaluated_by_strategy.get(outcome.strategy, [])
        if not outcome.source_message_ids or not matching_interventions:
            return False
        if not any(
            set(outcome.source_message_ids)
            <= {intervention.assistant_message_id}
            and _outcome_matches_recorded_feedback(outcome, intervention)
            for intervention in matching_interventions
        ):
            return False
    return True


def _outcome_matches_recorded_feedback(
    outcome: StrategyOutcome,
    intervention: InterventionRecord,
) -> bool:
    """Accept only outcome text composed from recorded intervention feedback."""

    details = [
        detail
        for detail in (
            intervention.objective_progress,
            intervention.strategy_fit,
            intervention.explicit_feedback,
        )
        if detail
    ]
    allowed_outcomes = {"evaluated"} if not details else set(details)
    if details:
        allowed_outcomes.update({"; ".join(details), "_".join(details)})
    return outcome.outcome in allowed_outcomes


def _has_valid_risk_events(
    result: SessionFinalizerResult,
    payload: SessionFinalizerInput,
) -> bool:
    """Require model risk events to exactly restate the typed final risk state."""

    if not result.risk_events:
        return True
    return _has_recorded_risk(payload) and result.risk_events == _risk_events(payload)


def _has_recorded_risk(payload: SessionFinalizerInput) -> bool:
    """Return whether final state contains a non-default risk record."""

    risk_state = payload.final_state.risk_state
    return bool(
        risk_state.level != RiskLevel.LOW
        or risk_state.categories
        or risk_state.reason_codes
    )


def _result_text_fields(result: SessionFinalizerResult) -> list[str]:
    """Collect every model-produced text field subject to claim safety checks."""

    return [
        result.session_summary,
        *result.goal_updates,
        *result.unfinished_topics,
        *(item.content for item in result.action_items),
        *(candidate.content for candidate in result.candidate_memories),
        *(outcome.outcome for outcome in result.strategy_outcomes),
        *result.risk_events,
    ]


def _normalize_model_result(
    result: SessionFinalizerResult,
    payload: SessionFinalizerInput,
) -> SessionFinalizerResult:
    """Deduplicate provenance while preserving model facts and first-seen order."""

    return result.model_copy(
        update={
            "session_id": payload.session_id,
            "source_message_ids": _dedupe(result.source_message_ids),
            "action_items": [
                item.model_copy(
                    update={"source_message_ids": _dedupe(item.source_message_ids)}
                )
                for item in result.action_items
            ],
            "candidate_memories": [
                candidate.model_copy(
                    update={
                        "source_message_ids": _dedupe(candidate.source_message_ids)
                    }
                )
                for candidate in result.candidate_memories
            ],
            "strategy_outcomes": [
                outcome.model_copy(
                    update={
                        "source_message_ids": _dedupe(outcome.source_message_ids)
                    }
                )
                for outcome in result.strategy_outcomes
            ],
        }
    )


def _session_summary(payload: SessionFinalizerInput) -> str:
    """Create a short grounded session summary."""

    if payload.rolling_summary is not None:
        parts = [
            part
            for part in [
                payload.rolling_summary.current_problem,
                payload.rolling_summary.session_goal,
            ]
            if part
        ]
        if parts:
            return "; ".join(parts)
    state_parts = [item.value for item in payload.final_state.active_topics]
    if payload.final_state.session_goal:
        state_parts.append(payload.final_state.session_goal)
    if state_parts:
        return "; ".join(_dedupe(state_parts))
    user_messages = _user_messages(payload.messages)
    if user_messages:
        return user_messages[-1].content
    return "No final session summary was available."


def _goal_updates(payload: SessionFinalizerInput) -> list[str]:
    """Return explicit goal information from the final session state."""

    if payload.final_state.session_goal:
        return [payload.final_state.session_goal]
    return []


def _unfinished_topics(payload: SessionFinalizerInput) -> list[str]:
    """Return active or open topics without inventing future work."""

    topics = [item.value for item in payload.final_state.open_questions]
    topics.extend(item.value for item in payload.final_state.active_topics)
    if payload.rolling_summary is not None:
        topics.extend(payload.rolling_summary.open_questions)
    return _dedupe(topics)


def _action_items(messages: list[Message]) -> list[ActionItem]:
    """Extract simple action items from explicit user requests."""

    items: list[ActionItem] = []
    for message in _user_messages(messages):
        if _mentions_any(message.content, _CONCRETE_STEP_TERMS):
            items.append(
                ActionItem(
                    content="Clarify one concrete next step.",
                    source_message_ids=[message.id],
                )
            )
    return _dedupe(items)


def _candidate_memories(messages: list[Message]) -> list[MemoryCandidate]:
    """Create low-risk memories only from explicit user statements."""

    candidates: list[MemoryCandidate] = []
    for message in _user_messages(messages):
        if _mentions_small_step_preference(message.content):
            candidates.append(
                MemoryCandidate(
                    candidate_type=MemoryType.INTERACTION_PREFERENCE,
                    content=(
                        "\u7528\u6237\u5e0c\u671b\u6bcf\u6b21\u53ea"
                        "\u6536\u5230\u4e00\u4e2a\u5c0f\u6b65\u9aa4"
                        "\uff0c\u4e0d\u8981\u4e00\u6b21\u7ed9\u592a"
                        "\u591a\u5efa\u8bae\u3002"
                    ),
                    source_message_ids=[message.id],
                    source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
                    confidence=0.94,
                    requires_user_confirmation=False,
                    sensitivity=MemorySensitivity.LOW,
                    recommended_operation=MemoryOperation.CREATE,
                )
            )
    return _dedupe(candidates)


def _strategy_outcomes(interventions: list[InterventionRecord]) -> list[StrategyOutcome]:
    """Summarize evaluated intervention outcomes."""

    outcomes: list[StrategyOutcome] = []
    for intervention in interventions:
        if intervention.status != InterventionStatus.EVALUATED:
            continue
        details = [
            part
            for part in [
                intervention.objective_progress,
                intervention.strategy_fit,
                intervention.explicit_feedback,
            ]
            if part
        ]
        outcomes.append(
            StrategyOutcome(
                strategy=intervention.strategy,
                outcome="; ".join(details) if details else "evaluated",
                source_message_ids=[intervention.assistant_message_id],
            )
        )
    return outcomes


def _risk_events(payload: SessionFinalizerInput) -> list[str]:
    """Return risk markers only when the final state records them."""

    risk_state = payload.final_state.risk_state
    if not _has_recorded_risk(payload):
        return []
    return [
        "; ".join(
            [
                f"risk_level={risk_state.level.value}",
                *risk_state.categories,
                *risk_state.reason_codes,
            ]
        )
    ]


def _summary_source_ids(payload: SessionFinalizerInput) -> list[MessageId]:
    """Collect message IDs used by the deterministic finalizer."""

    ids = [message.id for message in _user_messages(payload.messages)]
    if payload.rolling_summary is not None:
        ids.extend(payload.rolling_summary.source_message_ids)
    ids.extend(item.source.message_id for item in payload.final_state.active_topics)
    ids.extend(item.source.message_id for item in payload.final_state.reported_emotions)
    ids.extend(item.source.message_id for item in payload.final_state.user_preferences)
    ids.extend(item.source.message_id for item in payload.final_state.open_questions)
    ids.extend(intervention.assistant_message_id for intervention in payload.interventions)
    return _dedupe(ids)


def _user_messages(messages: list[Message]) -> list[Message]:
    """Return only user-authored messages."""

    return [message for message in messages if message.role == MessageRole.USER]


def _mentions_small_step_preference(text: str) -> bool:
    """Return whether text explicitly asks for small-step guidance."""

    return _mentions_any(text, _SMALL_STEP_TERMS) or _mentions_any(
        text,
        _TOO_MANY_SUGGESTIONS_TERMS,
    )


def _mentions_any(text: str, terms: tuple[str, ...]) -> bool:
    """Return whether text contains any term, case-insensitively."""

    normalized = text.casefold()
    return any(term in normalized for term in terms)


def _dedupe(items: list[ItemT]) -> list[ItemT]:
    """Return items in first-seen order without duplicates."""

    result: list[ItemT] = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


__all__ = ["FakeSessionFinalizer", "SessionFinalizer"]
