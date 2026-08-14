"""Session finalizer implementations for close-session memory extraction."""

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import Field

from llm.structured_client import StructuredLLMClientProtocol
from schemas.common import ContractModel, InterventionId, MessageId
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
from services.claim_safety import unsupported_claim_reason
from services.model_execution import (
    ModelExecutionEvent,
    ModelExecutionFailure,
    ModelExecutionStatus,
    log_model_execution,
    model_exception_status,
)

ItemT = TypeVar("ItemT")
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "session_finalizer.md"
_LOGGER = logging.getLogger(__name__)
_AGENT_NAME = "session_finalizer"

_SMALL_STEP_TERMS = (
    "one small step",
    "one step at a time",
    "small step",
    "\u6bcf\u6b21\u4e00\u4e2a\u5c0f\u6b65\u9aa4",
    "\u6bcf\u6b21\u53ea\u7ed9\u6211\u4e00\u4e2a\u5c0f\u6b65\u9aa4",
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
    "\u4e0d\u8981\u4e00\u6b21\u7ed9\u592a\u591a",
)
_CONCRETE_STEP_TERMS = (
    "concrete next steps",
    "next step",
    "\u4e0b\u4e00\u6b65\u5177\u4f53",
    "\u5177\u4f53\u600e\u4e48\u505a",
    "\u53ea\u7ed9\u6211\u7b2c\u4e00\u6b65",
)
_COMMITMENT_TERMS = (
    "i will",
    "i'll",
    "i can do",
    "please give me",
    "give me the first step",
    "\u6211\u4f1a",
    "\u6211\u5148",
    "\u6211\u53ef\u4ee5",
    "\u90a3\u5148\u544a\u8bc9\u6211",
    "\u8bf7\u5148\u7ed9\u6211",
    "\u53ea\u7ed9\u6211\u7b2c\u4e00\u6b65",
)
_SMALL_STEP_CONTENT = (
    "\u7528\u6237\u5e0c\u671b\u6bcf\u6b21\u53ea\u6536\u5230"
    "\u4e00\u4e2a\u5c0f\u6b65\u9aa4\uff0c\u4e0d\u8981\u4e00\u6b21"
    "\u7ed9\u592a\u591a\u5efa\u8bae\u3002"
)


class _MessageEvidence(ContractModel):
    """Private field-level quote evidence for one finalization claim."""

    message_id: MessageId
    quote: str = Field(min_length=1)


class _GroundedTextDraft(ContractModel):
    """Private text field carrying direct message quote evidence."""

    value: str = Field(min_length=1)
    evidence: list[_MessageEvidence] = Field(min_length=1)


class _GroundedActionItemDraft(ContractModel):
    """Private action item that must be grounded in user commitment."""

    content: str = Field(min_length=1)
    evidence: list[_MessageEvidence] = Field(min_length=1)


class _GroundedStrategyOutcomeDraft(ContractModel):
    """Private strategy outcome bound to one evaluated intervention."""

    intervention_id: InterventionId
    strategy: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    evidence: list[_MessageEvidence] = Field(min_length=1)


class _GroundedRiskEventDraft(ContractModel):
    """Private risk event that can only restate typed risk input."""

    risk_level: RiskLevel
    categories: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    description: str = Field(min_length=1)
    evidence: list[_MessageEvidence] = Field(min_length=1)


class _ProvisionalMemoryDraft(ContractModel):
    """Private provisional memory candidate, not a durable memory decision."""

    candidate_type: MemoryType
    content: str = Field(min_length=1)
    source_type: MemorySourceType
    confidence: float = Field(ge=0.0, le=1.0)
    sensitivity: MemorySensitivity
    requires_user_confirmation: bool
    recommended_operation: MemoryOperation = MemoryOperation.CREATE
    evidence: list[_MessageEvidence] = Field(min_length=1)


class _GroundedSessionFinalizerDraft(ContractModel):
    """Private model output containing only grounded finalization fields."""

    session_summary: _GroundedTextDraft
    goal_updates: list[_GroundedTextDraft] = Field(default_factory=list)
    unfinished_topics: list[_GroundedTextDraft] = Field(default_factory=list)
    action_items: list[_GroundedActionItemDraft] = Field(default_factory=list)
    strategy_outcomes: list[_GroundedStrategyOutcomeDraft] = Field(default_factory=list)
    risk_events: list[_GroundedRiskEventDraft] = Field(default_factory=list)
    provisional_memories: list[_ProvisionalMemoryDraft] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _EvidenceIndex:
    """Typed lookup tables for deterministic finalizer validation."""

    messages_by_id: dict[MessageId, Message]
    user_message_ids: frozenset[MessageId]
    assistant_message_ids: frozenset[MessageId]
    message_sequence_by_id: dict[MessageId, int]
    interventions_by_id: dict[InterventionId, InterventionRecord]
    interventions_by_assistant_message_id: dict[MessageId, InterventionRecord]
    evaluated_interventions: frozenset[InterventionId]
    pending_interventions: frozenset[InterventionId]
    cancelled_interventions: frozenset[InterventionId]
    covered_summary_source_ids: frozenset[MessageId]
    risk_source_message_ids: frozenset[MessageId]


class FakeSessionFinalizer:
    """Deterministic finalizer for session-close smoke tests."""

    async def run(self, payload: SessionFinalizerInput) -> SessionFinalizerResult:
        """Alias for finalize so callers can treat it like an agent."""

        return await self.finalize(payload)

    async def finalize(self, payload: SessionFinalizerInput) -> SessionFinalizerResult:
        """Build a conservative final session result from typed inputs."""

        _validate_input_relationships(payload)
        action_items = _action_items(payload.messages)
        candidate_memories = _candidate_memories(payload.messages)
        strategy_outcomes = _strategy_outcomes(payload.interventions)
        risk_events = _risk_events(payload)
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
            risk_events=risk_events,
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
        strict_model: bool = False,
    ) -> None:
        """Create a finalizer with an optional structured model dependency."""

        self._llm_client = llm_client
        self._model_name = model_name
        self._fallback = fallback or FakeSessionFinalizer()
        self._strict_model = strict_model
        self._prompt_template = prompt_template or _read_prompt(
            _PROMPT_PATH,
            fallback=(
                "Finalize the session by returning only a private grounded "
                "finalization draft JSON object."
            ),
        )

    async def run(self, payload: SessionFinalizerInput) -> SessionFinalizerResult:
        """Alias for finalize so callers can treat it like an agent."""

        return await self.finalize(payload)

    async def finalize(self, payload: SessionFinalizerInput) -> SessionFinalizerResult:
        """Finalize through a grounded model draft or the configured failure mode."""

        _validate_input_relationships(payload)
        if not payload.messages:
            raise ValueError("session finalizer input messages must not be empty")
        if self._llm_client is None:
            event = ModelExecutionEvent(
                agent=_AGENT_NAME,
                status=ModelExecutionStatus.FALLBACK_NO_CLIENT,
                model_name=self._model_name,
                reason_codes=("llm_client_unavailable",),
            )
            return await self._fail_or_fallback(payload, event)
        try:
            raw_draft = await self._llm_client.generate_structured(
                self._build_prompt(payload),
                _GroundedSessionFinalizerDraft,
                model_name=self._model_name,
                metadata={"agent": _AGENT_NAME},
            )
            draft = _GroundedSessionFinalizerDraft.model_validate(raw_draft)
        except Exception as error:
            status, reasons = model_exception_status(error)
            event = ModelExecutionEvent(
                agent=_AGENT_NAME,
                status=status,
                model_name=self._model_name,
                reason_codes=reasons,
            )
            return await self._fail_or_fallback(payload, event, cause=error)

        reasons = _finalizer_validation_reasons(draft, payload)
        if reasons:
            event = ModelExecutionEvent(
                agent=_AGENT_NAME,
                status=ModelExecutionStatus.FALLBACK_SEMANTIC_VALIDATION_ERROR,
                model_name=self._model_name,
                reason_codes=reasons,
            )
            return await self._fail_or_fallback(payload, event)

        event = ModelExecutionEvent(
            agent=_AGENT_NAME,
            status=ModelExecutionStatus.MODEL_SUCCESS,
            model_name=self._model_name,
        )
        log_model_execution(_LOGGER, event)
        return _draft_to_result(draft, payload)

    async def _fail_or_fallback(
        self,
        payload: SessionFinalizerInput,
        event: ModelExecutionEvent,
        *,
        cause: Exception | None = None,
    ) -> SessionFinalizerResult:
        """Log one safe event, then raise in strict mode or use the fake."""

        log_model_execution(_LOGGER, event)
        if self._strict_model:
            failure = ModelExecutionFailure(event)
            if cause is not None:
                raise failure from cause
            raise failure
        return await self._fallback.finalize(payload)

    def _build_prompt(self, payload: SessionFinalizerInput) -> str:
        """Render the typed finalizer input as grounded JSON context."""

        payload_json = json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        return f"{self._prompt_template}\n\nINPUT_JSON:\n{payload_json}"


def _read_prompt(path: Path, *, fallback: str) -> str:
    """Read an optional prompt file, falling back when absent or empty."""

    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return fallback


def _validate_input_relationships(payload: SessionFinalizerInput) -> None:
    """Validate the finalizer input graph before model or fallback work."""

    if payload.final_state.session_id != payload.session_id:
        raise ValueError("finalizer final_state session ID must match")
    if (
        payload.rolling_summary is not None
        and payload.rolling_summary.session_id != payload.session_id
    ):
        raise ValueError("finalizer rolling_summary session ID must match")

    seen_message_ids: set[MessageId] = set()
    previous_sequence: int | None = None
    for message in payload.messages:
        if message.session_id != payload.session_id:
            raise ValueError("finalizer message session IDs must match")
        if message.id in seen_message_ids:
            raise ValueError("finalizer message IDs must be unique")
        seen_message_ids.add(message.id)
        if previous_sequence is not None and message.sequence_number <= previous_sequence:
            raise ValueError("finalizer message sequence numbers must increase")
        previous_sequence = message.sequence_number

    messages_by_id = {message.id: message for message in payload.messages}
    for intervention in payload.interventions:
        if intervention.session_id != payload.session_id:
            raise ValueError("finalizer intervention session IDs must match")
        assistant = messages_by_id.get(intervention.assistant_message_id)
        if assistant is None or assistant.role != MessageRole.ASSISTANT:
            raise ValueError(
                "finalizer intervention assistant_message_id must reference assistant"
            )
        feedback_fields = [
            intervention.observed_response,
            intervention.explicit_feedback,
            intervention.strategy_fit,
            intervention.objective_progress,
            intervention.recommended_adjustment,
        ]
        if intervention.status == InterventionStatus.EVALUATED and not any(feedback_fields):
            raise ValueError("evaluated intervention must include feedback fields")
        if intervention.status == InterventionStatus.PENDING and any(feedback_fields):
            raise ValueError("pending intervention must not include feedback fields")

    message_ids = set(messages_by_id)
    if payload.rolling_summary is not None and any(
        source_id not in message_ids
        for source_id in payload.rolling_summary.source_message_ids
    ):
        raise ValueError("rolling summary sources must belong to input messages")
    for state_item in [
        *payload.final_state.active_topics,
        *payload.final_state.reported_emotions,
        *payload.final_state.user_preferences,
        *payload.final_state.open_questions,
    ]:
        if state_item.source.message_id not in message_ids:
            raise ValueError("final state source IDs must belong to input messages")


def _evidence_index(payload: SessionFinalizerInput) -> _EvidenceIndex:
    """Build deterministic evidence lookup structures for one finalizer input."""

    messages_by_id = {message.id: message for message in payload.messages}
    user_ids = frozenset(
        message.id for message in payload.messages if message.role == MessageRole.USER
    )
    assistant_ids = frozenset(
        message.id
        for message in payload.messages
        if message.role == MessageRole.ASSISTANT
    )
    interventions_by_id = {
        intervention.intervention_id: intervention
        for intervention in payload.interventions
    }
    risk_source_ids = set(user_ids)
    risk_source_ids.update(
        item.source.message_id
        for item in [
            *payload.final_state.active_topics,
            *payload.final_state.reported_emotions,
            *payload.final_state.open_questions,
        ]
    )
    return _EvidenceIndex(
        messages_by_id=messages_by_id,
        user_message_ids=user_ids,
        assistant_message_ids=assistant_ids,
        message_sequence_by_id={
            message.id: message.sequence_number for message in payload.messages
        },
        interventions_by_id=interventions_by_id,
        interventions_by_assistant_message_id={
            intervention.assistant_message_id: intervention
            for intervention in payload.interventions
        },
        evaluated_interventions=frozenset(
            intervention.intervention_id
            for intervention in payload.interventions
            if intervention.status == InterventionStatus.EVALUATED
        ),
        pending_interventions=frozenset(
            intervention.intervention_id
            for intervention in payload.interventions
            if intervention.status == InterventionStatus.PENDING
        ),
        cancelled_interventions=frozenset(
            intervention.intervention_id
            for intervention in payload.interventions
            if intervention.status == InterventionStatus.CANCELLED
        ),
        covered_summary_source_ids=frozenset(
            payload.rolling_summary.source_message_ids
            if payload.rolling_summary is not None
            else []
        ),
        risk_source_message_ids=frozenset(risk_source_ids),
    )


def _finalizer_validation_reasons(
    draft: _GroundedSessionFinalizerDraft,
    payload: SessionFinalizerInput,
) -> tuple[str, ...]:
    """Return stable semantic reason codes for an invalid finalization draft."""

    index = _evidence_index(payload)
    reasons: list[str] = []

    _extend_evidence_reasons(
        reasons,
        draft.session_summary.evidence,
        index,
        required_role=None,
    )

    for goal in draft.goal_updates:
        _extend_evidence_reasons(
            reasons,
            goal.evidence,
            index,
            required_role=MessageRole.USER,
            required_role_reason="goal_update_requires_user_source",
        )
        if not _evidence_supports_text(goal.value, goal.evidence, index):
            reasons.append("goal_update_not_supported_by_evidence")

    for topic in draft.unfinished_topics:
        _extend_evidence_reasons(
            reasons,
            topic.evidence,
            index,
            required_role=MessageRole.USER,
            required_role_reason="unfinished_topic_requires_user_source",
        )
        if not _evidence_supports_text(topic.value, topic.evidence, index):
            reasons.append("unfinished_topic_not_supported_by_evidence")

    for action in draft.action_items:
        _extend_evidence_reasons(
            reasons,
            action.evidence,
            index,
            required_role=MessageRole.USER,
            required_role_reason="action_item_requires_user_source",
        )
        if any(evidence.message_id in index.assistant_message_ids for evidence in action.evidence):
            reasons.append("action_item_from_assistant_only")
        if not _has_user_commitment(action.evidence, index):
            reasons.append("action_item_requires_user_commitment")
        if not _evidence_supports_text(action.content, action.evidence, index):
            reasons.append("action_item_not_supported_by_evidence")

    for outcome in draft.strategy_outcomes:
        _strategy_outcome_reasons(reasons, outcome, index)

    for risk_event in draft.risk_events:
        _risk_event_reasons(reasons, risk_event, payload, index)

    for memory in draft.provisional_memories:
        _provisional_memory_reasons(reasons, memory, index)

    for text in _draft_texts_for_claim_safety(draft):
        reason = unsupported_claim_reason(text)
        if reason is not None:
            reasons.append(reason)

    return tuple(dict.fromkeys(reasons))


def _extend_evidence_reasons(
    reasons: list[str],
    evidence_items: list[_MessageEvidence],
    index: _EvidenceIndex,
    *,
    required_role: MessageRole | None,
    required_role_reason: str | None = None,
) -> None:
    """Append stable evidence validation reasons for one field."""

    if not evidence_items:
        reasons.append("missing_finalizer_field_evidence")
        return
    for evidence in evidence_items:
        normalized_quote = _normalize(evidence.quote)
        if not normalized_quote:
            reasons.append("empty_finalizer_evidence_quote")
            continue
        message = index.messages_by_id.get(evidence.message_id)
        if message is None:
            reasons.append("unknown_finalizer_source_message")
            reasons.append("finalizer_source_outside_session")
            continue
        if required_role is not None and message.role != required_role:
            if required_role_reason is not None:
                reasons.append(required_role_reason)
        if normalized_quote not in _normalize(message.content):
            reasons.append("finalizer_evidence_quote_not_found")


def _strategy_outcome_reasons(
    reasons: list[str],
    outcome: _GroundedStrategyOutcomeDraft,
    index: _EvidenceIndex,
) -> None:
    """Validate one strategy outcome against evaluated intervention feedback."""

    _extend_evidence_reasons(
        reasons,
        outcome.evidence,
        index,
        required_role=MessageRole.USER,
        required_role_reason="strategy_outcome_requires_user_feedback_source",
    )
    intervention = index.interventions_by_id.get(outcome.intervention_id)
    if intervention is None:
        reasons.append("strategy_outcome_unknown_intervention")
        return
    if outcome.intervention_id in index.pending_interventions:
        reasons.append("strategy_outcome_from_pending_intervention")
        return
    if outcome.intervention_id in index.cancelled_interventions:
        reasons.append("strategy_outcome_from_cancelled_intervention")
        return
    if outcome.strategy != intervention.strategy:
        reasons.append("strategy_outcome_strategy_mismatch")
    if not _outcome_matches_recorded_feedback(outcome.outcome, intervention):
        reasons.append("strategy_outcome_feedback_mismatch")
    feedback_message = _user_response_for_intervention(intervention, index)
    if feedback_message is None or not any(
        evidence.message_id == feedback_message.id for evidence in outcome.evidence
    ):
        reasons.append("strategy_outcome_not_supported_by_evidence")


def _risk_event_reasons(
    reasons: list[str],
    risk_event: _GroundedRiskEventDraft,
    payload: SessionFinalizerInput,
    index: _EvidenceIndex,
) -> None:
    """Validate one risk event against the typed final risk state."""

    _extend_evidence_reasons(
        reasons,
        risk_event.evidence,
        index,
        required_role=MessageRole.USER,
        required_role_reason="risk_event_unknown_source",
    )
    risk_state = payload.final_state.risk_state
    if not _has_recorded_risk(payload):
        reasons.append("risk_event_without_typed_support")
    if risk_event.risk_level != risk_state.level:
        reasons.append("risk_level_mismatch")
    if set(risk_event.categories) - set(risk_state.categories):
        reasons.append("risk_category_mismatch")
    if not any(
        evidence.message_id in index.risk_source_message_ids
        for evidence in risk_event.evidence
    ):
        reasons.append("risk_event_unknown_source")
    if not _evidence_supports_text(risk_event.description, risk_event.evidence, index):
        reasons.append("risk_event_not_supported_by_evidence")


def _provisional_memory_reasons(
    reasons: list[str],
    memory: _ProvisionalMemoryDraft,
    index: _EvidenceIndex,
) -> None:
    """Validate one provisional memory candidate without durable-write semantics."""

    _extend_evidence_reasons(
        reasons,
        memory.evidence,
        index,
        required_role=MessageRole.USER,
        required_role_reason="provisional_memory_requires_user_source",
    )
    if any(evidence.message_id not in index.messages_by_id for evidence in memory.evidence):
        reasons.append("provisional_memory_unknown_source")
    if any(
        evidence.message_id in index.messages_by_id
        and _normalize(evidence.quote)
        and _normalize(evidence.quote)
        not in _normalize(index.messages_by_id[evidence.message_id].content)
        for evidence in memory.evidence
    ):
        reasons.append("provisional_memory_quote_not_found")
    if memory.source_type not in {
        MemorySourceType.EXPLICIT_USER_STATEMENT,
        MemorySourceType.SESSION_SUMMARY,
    }:
        reasons.append("provisional_memory_invalid_source_type")
    if memory.source_type == MemorySourceType.REPEATED_OBSERVATION:
        reasons.append("provisional_memory_repeated_observation_not_supported")
    if memory.source_type == MemorySourceType.MODEL_INFERENCE:
        reasons.append("provisional_memory_model_inference_not_allowed")
    if memory.recommended_operation != MemoryOperation.CREATE:
        reasons.append("provisional_memory_unsupported_operation")
    if not _memory_type_matches(memory):
        reasons.append("provisional_memory_type_mismatch")
    if memory.sensitivity != MemorySensitivity.LOW and not memory.requires_user_confirmation:
        reasons.append("provisional_memory_invalid_confirmation")
    if memory.sensitivity not in set(MemorySensitivity):
        reasons.append("provisional_memory_invalid_sensitivity")


def _memory_type_matches(memory: _ProvisionalMemoryDraft) -> bool:
    """Return whether candidate type matches conservative content/source semantics."""

    normalized = _normalize(memory.content)
    if memory.candidate_type == MemoryType.INTERACTION_PREFERENCE:
        return any(_normalize(term) in normalized for term in _SMALL_STEP_TERMS) or (
            "preference" in normalized
        )
    if memory.candidate_type == MemoryType.ACTIVE_GOAL:
        return "goal" in normalized or "\u76ee\u6807" in normalized
    if memory.candidate_type == MemoryType.UNFINISHED_TOPIC:
        return "unfinished" in normalized or "\u7ee7\u7eed" in normalized
    if memory.candidate_type == MemoryType.STRATEGY_OUTCOME:
        return "strategy" in normalized or "\u7b56\u7565" in normalized
    return memory.candidate_type in {MemoryType.SEMANTIC, MemoryType.EPISODIC}


def _outcome_matches_recorded_feedback(
    outcome: str,
    intervention: InterventionRecord,
) -> bool:
    """Accept only outcome text consistent with recorded intervention feedback."""

    normalized = _normalize(outcome)
    if intervention.explicit_feedback == "negative":
        if "not_achieved" in normalized:
            return True
        return not any(term in normalized for term in ("success", "achieved", "good"))
    if intervention.explicit_feedback == "mixed":
        return not any(term in normalized for term in ("fully achieved", "complete success"))
    if intervention.explicit_feedback == "absent":
        return not any(term in normalized for term in ("achieved", "good", "success"))
    details = [
        detail
        for detail in (
            intervention.objective_progress,
            intervention.strategy_fit,
            intervention.explicit_feedback,
        )
        if detail
    ]
    if not details:
        return "evaluated" in normalized or "unknown" in normalized
    return any(_normalize(detail) in normalized for detail in details)


def _user_response_for_intervention(
    intervention: InterventionRecord,
    index: _EvidenceIndex,
) -> Message | None:
    """Return the first user message after the assistant intervention."""

    assistant_sequence = index.message_sequence_by_id.get(intervention.assistant_message_id)
    if assistant_sequence is None:
        return None
    candidates = [
        message
        for message in index.messages_by_id.values()
        if message.role == MessageRole.USER
        and message.sequence_number > assistant_sequence
    ]
    return min(candidates, key=lambda message: message.sequence_number, default=None)


def _evidence_supports_text(
    text: str,
    evidence_items: list[_MessageEvidence],
    index: _EvidenceIndex,
) -> bool:
    """Return whether evidence quotes provide non-empty support for text."""

    _ = text
    return any(
        evidence.message_id in index.messages_by_id
        and _normalize(evidence.quote)
        and _normalize(evidence.quote)
        in _normalize(index.messages_by_id[evidence.message_id].content)
        for evidence in evidence_items
    )


def _has_user_commitment(
    evidence_items: list[_MessageEvidence],
    index: _EvidenceIndex,
) -> bool:
    """Return whether user evidence explicitly requests or accepts an action."""

    for evidence in evidence_items:
        message = index.messages_by_id.get(evidence.message_id)
        if message is None or message.role != MessageRole.USER:
            continue
        normalized = _normalize(f"{message.content} {evidence.quote}")
        if any(_normalize(term) in normalized for term in _COMMITMENT_TERMS):
            return True
    return False


def _draft_texts_for_claim_safety(
    draft: _GroundedSessionFinalizerDraft,
) -> tuple[str, ...]:
    """Collect all private draft semantic text for shared claim-safety checks."""

    texts = [draft.session_summary.value]
    texts.extend(goal.value for goal in draft.goal_updates)
    texts.extend(topic.value for topic in draft.unfinished_topics)
    texts.extend(action.content for action in draft.action_items)
    texts.extend(outcome.outcome for outcome in draft.strategy_outcomes)
    texts.extend(event.description for event in draft.risk_events)
    texts.extend(memory.content for memory in draft.provisional_memories)
    return tuple(texts)


def _draft_to_result(
    draft: _GroundedSessionFinalizerDraft,
    payload: SessionFinalizerInput,
) -> SessionFinalizerResult:
    """Discard private evidence and produce the unchanged public contract."""

    action_items = [
        ActionItem(
            content=item.content,
            source_message_ids=_dedupe(
                [evidence.message_id for evidence in item.evidence]
            ),
        )
        for item in draft.action_items
    ]
    candidate_memories = [
        MemoryCandidate(
            candidate_type=item.candidate_type,
            content=item.content,
            source_message_ids=_dedupe(
                [evidence.message_id for evidence in item.evidence]
            ),
            source_type=item.source_type,
            confidence=item.confidence,
            requires_user_confirmation=item.requires_user_confirmation,
            sensitivity=item.sensitivity,
            recommended_operation=MemoryOperation.CREATE,
        )
        for item in draft.provisional_memories
    ]
    strategy_outcomes = [
        StrategyOutcome(
            strategy=item.strategy,
            outcome=item.outcome,
            source_message_ids=_dedupe(
                [evidence.message_id for evidence in item.evidence]
            ),
        )
        for item in draft.strategy_outcomes
    ]
    risk_events = [
        event.description for event in draft.risk_events
    ]
    source_message_ids = _dedupe(
        [evidence.message_id for evidence in _draft_evidence_items(draft)]
    )
    return SessionFinalizerResult(
        session_id=payload.session_id,
        session_summary=draft.session_summary.value,
        goal_updates=[item.value for item in draft.goal_updates],
        unfinished_topics=[item.value for item in draft.unfinished_topics],
        action_items=action_items,
        candidate_memories=candidate_memories,
        strategy_outcomes=strategy_outcomes,
        risk_events=risk_events,
        source_message_ids=source_message_ids,
    )


def _draft_evidence_items(
    draft: _GroundedSessionFinalizerDraft,
) -> tuple[_MessageEvidence, ...]:
    """Return all field-level evidence in stable field order."""

    items: list[_MessageEvidence] = []
    items.extend(draft.session_summary.evidence)
    for goal in draft.goal_updates:
        items.extend(goal.evidence)
    for topic in draft.unfinished_topics:
        items.extend(topic.evidence)
    for action in draft.action_items:
        items.extend(action.evidence)
    for outcome in draft.strategy_outcomes:
        items.extend(outcome.evidence)
    for event in draft.risk_events:
        items.extend(event.evidence)
    for memory in draft.provisional_memories:
        items.extend(memory.evidence)
    return tuple(items)


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
        if _mentions_any(message.content, _CONCRETE_STEP_TERMS) and _mentions_any(
            message.content,
            _COMMITMENT_TERMS,
        ):
            items.append(
                ActionItem(
                    content="Clarify one concrete next step.",
                    source_message_ids=[message.id],
                )
            )
    return _dedupe(items)


def _candidate_memories(messages: list[Message]) -> list[MemoryCandidate]:
    """Create provisional low-risk memories only from explicit user statements."""

    candidates: list[MemoryCandidate] = []
    for message in _user_messages(messages):
        if _mentions_small_step_preference(message.content):
            candidates.append(
                MemoryCandidate(
                    candidate_type=MemoryType.INTERACTION_PREFERENCE,
                    content=_SMALL_STEP_CONTENT,
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


def _has_recorded_risk(payload: SessionFinalizerInput) -> bool:
    """Return whether final state contains a non-default risk record."""

    risk_state = payload.final_state.risk_state
    return bool(
        risk_state.level != RiskLevel.LOW
        or risk_state.categories
        or risk_state.reason_codes
    )


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
    """Return whether normalized text contains any term."""

    normalized = _normalize(text)
    return any(_normalize(term) in normalized for term in terms)


def _normalize(text: str) -> str:
    """Normalize Unicode, case, and consecutive whitespace for evidence matching."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _dedupe(items: list[ItemT]) -> list[ItemT]:
    """Return items in first-seen order without duplicates."""

    result: list[ItemT] = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


__all__ = ["FakeSessionFinalizer", "SessionFinalizer"]
