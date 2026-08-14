"""Grounded fake and model-backed memory candidate curation."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import Field

from llm.structured_client import StructuredLLMClientProtocol
from schemas.common import ContractModel, MessageId, SessionId
from schemas.intervention import InterventionRecord, InterventionStatus
from schemas.memory import (
    MemoryCandidate,
    MemoryOperation,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from schemas.messages import Message, MessageRole
from schemas.state import SessionState
from schemas.summary import RollingSummary, SessionFinalizerResult, StrategyOutcome
from services.claim_safety import unsupported_claim_reason
from services.model_execution import (
    ModelExecutionEvent,
    ModelExecutionFailure,
    ModelExecutionStatus,
    log_model_execution,
    model_exception_status,
)

ItemT = TypeVar("ItemT")
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "memory_curator.md"
_LOGGER = logging.getLogger(__name__)
_AGENT_NAME = "memory_curator"

_SMALL_STEP_TERMS = (
    "one small step",
    "one step at a time",
    "one at a time",
    "one practical step",
    "too many suggestions at once",
    "too many steps at once",
    "do not give me too many suggestions",
    "small steps",
    "\u5c0f\u6b65\u9aa4",
    "\u4e00\u4e2a\u5c0f\u6b65\u9aa4",
    "\u4e00\u6b65\u4e00\u6b65",
    "\u6bcf\u6b21\u4e00\u4e2a",
    "\u4e00\u6b21\u53ea\u7ed9",
)
_MEETING_ONLY_TERMS = (
    "too many meetings",
    "many meetings",
    "lots of meetings",
    "\u4f1a\u8bae\u5f88\u591a",
    "\u4f1a\u8bae\u592a\u591a",
)
_PREFERENCE_TERMS = (
    "prefer",
    "preference",
    "please remember",
    "i like",
    "works better for me",
    "\u504f\u597d",
    "\u559c\u6b22",
    "\u8bb0\u4f4f",
)
_GOAL_TERMS = (
    "goal",
    "plan",
    "i will",
    "i want to",
    "i'm going to",
    "current plan",
    "\u76ee\u6807",
    "\u8ba1\u5212",
    "\u6211\u4f1a",
    "\u6211\u60f3",
)
_UNFINISHED_TERMS = (
    "continue",
    "next time",
    "unfinished",
    "come back to",
    "talk more about",
    "\u7ee7\u7eed",
    "\u4e0b\u6b21",
    "\u672a\u5b8c",
)
_STRATEGY_TERMS = (
    "strategy",
    "outcome",
    "feedback",
    "mixed",
    "partial",
    "helped",
    "\u7b56\u7565",
    "\u53cd\u9988",
    "\u90e8\u5206",
)
_HIGH_SENSITIVITY_TERMS = (
    "trauma",
    "abuse",
    "self-harm",
    "suicide",
    "diagnosis",
    "medication",
    "\u521b\u4f24",
    "\u81ea\u4f24",
    "\u81ea\u6740",
    "\u8bca\u65ad",
)
_LOW_SENSITIVITY_TYPES = {
    MemoryType.INTERACTION_PREFERENCE,
    MemoryType.STRATEGY_OUTCOME,
}
_SMALL_STEP_CONTENT = "用户希望每次只收到一个小步骤，不要一次给太多建议。"


@dataclass(frozen=True)
class MemoryCuratorInput:
    """Typed input containing all evidence available during one session close."""

    session_id: SessionId
    messages: list[Message]
    final_state: SessionState
    finalizer_result: SessionFinalizerResult
    rolling_summary: RollingSummary | None = None
    interventions: list[InterventionRecord] | None = None


class _MessageEvidenceDraft(ContractModel):
    """Private raw evidence from the model; invalid values stay candidate-local."""

    message_id: str | None = None
    quote: str | None = None


class _RawMemoryCandidateDraft(ContractModel):
    """Private raw candidate draft; enums intentionally remain strings."""

    candidate_type: str | None = None
    content: str | None = None
    source_type: str | None = None
    confidence: float | None = None
    sensitivity: str | None = None
    requires_user_confirmation: bool | None = None
    recommended_operation: str | None = None
    evidence: list[_MessageEvidenceDraft] = Field(default_factory=list)
    provisional_candidate_indexes: list[int] = Field(default_factory=list)


class _RawMemoryCandidateBatch(ContractModel):
    """Private structured-output envelope; it is not a public schema contract."""

    candidates: list[_RawMemoryCandidateDraft] = Field(default_factory=list)


@dataclass(frozen=True)
class _CandidateValidationResult:
    """Private audit result for exactly one raw model candidate."""

    candidate_index: int
    accepted: bool
    reason_codes: tuple[str, ...]
    candidate: MemoryCandidate | None


@dataclass(frozen=True)
class _CuratorAudit:
    """Private batch audit kept out of public schemas."""

    candidates: list[MemoryCandidate]
    validation_results: tuple[_CandidateValidationResult, ...]


@dataclass(frozen=True)
class _EvidenceIndex:
    """Typed evidence and relationship index for one curator input."""

    messages_by_id: dict[MessageId, Message]
    message_sequence_by_id: dict[MessageId, int]
    user_message_ids: set[MessageId]
    assistant_message_ids: set[MessageId]
    allowed_source_message_ids: set[MessageId]
    explicit_user_source_ids: set[MessageId]
    state_preference_source_ids: set[MessageId]
    state_goal_source_ids: set[MessageId]
    rolling_summary_source_ids: set[MessageId]
    evaluated_interventions: dict[str, InterventionRecord]
    evaluated_user_response_ids: set[MessageId]
    provisional_candidates_by_index: dict[int, MemoryCandidate]


class FakeMemoryCurator:
    """Deterministically curate grounded candidates without durable writes."""

    async def run(self, payload: MemoryCuratorInput) -> list[MemoryCandidate]:
        return await self.curate(payload)

    async def curate(self, payload: MemoryCuratorInput) -> list[MemoryCandidate]:
        """Merge safe finalizer, user-message, and sourced-state candidates."""

        _validate_input_relationships(payload)
        candidates = list(payload.finalizer_result.candidate_memories)
        candidates.extend(_message_candidates(payload.messages))
        candidates.extend(_state_preference_candidates(payload.final_state))
        grounded = [
            candidate
            for candidate in candidates
            if _is_valid_public_candidate(candidate, payload)
        ]
        return _dedupe_candidates(grounded)


class MemoryCurator:
    """Model-backed curator with candidate-local validation and safe fallback."""

    def __init__(
        self,
        llm_client: StructuredLLMClientProtocol | None = None,
        *,
        model_name: str | None = None,
        fallback: FakeMemoryCurator | None = None,
        prompt_template: str | None = None,
        strict_model: bool = False,
    ) -> None:
        self._llm_client = llm_client
        self._model_name = model_name
        self._fallback = fallback or FakeMemoryCurator()
        self._strict_model = strict_model
        self._prompt_template = prompt_template or _read_prompt(
            _PROMPT_PATH,
            fallback="Return a private raw memory candidate batch.",
        )

    async def run(self, payload: MemoryCuratorInput) -> list[MemoryCandidate]:
        return await self.curate(payload)

    async def curate(self, payload: MemoryCuratorInput) -> list[MemoryCandidate]:
        """Return public candidates while keeping raw audit private."""

        audit = await self._curate_with_audit(payload)
        return audit.candidates

    async def _curate_with_audit(self, payload: MemoryCuratorInput) -> _CuratorAudit:
        """Curate with batch-level failure handling and candidate-level rejection."""

        _validate_input_relationships(payload)
        if self._llm_client is None:
            event = ModelExecutionEvent(
                agent=_AGENT_NAME,
                status=ModelExecutionStatus.FALLBACK_NO_CLIENT,
                model_name=self._model_name,
                reason_codes=("llm_client_unavailable",),
            )
            candidates = await self._fail_or_fallback(payload, event)
            return _CuratorAudit(candidates=candidates, validation_results=())
        try:
            raw = await self._llm_client.generate_structured(
                self._build_prompt(payload),
                _RawMemoryCandidateBatch,
                model_name=self._model_name,
                metadata={"agent": _AGENT_NAME},
            )
            batch = _RawMemoryCandidateBatch.model_validate(raw)
        except Exception as error:
            status, reasons = model_exception_status(error)
            event = ModelExecutionEvent(
                agent=_AGENT_NAME,
                status=status,
                model_name=self._model_name,
                reason_codes=reasons,
            )
            candidates = await self._fail_or_fallback(payload, event, cause=error)
            return _CuratorAudit(candidates=candidates, validation_results=())

        index = _build_evidence_index(payload)
        validation_results = tuple(
            _validate_candidate(raw_candidate, candidate_index, payload, index)
            for candidate_index, raw_candidate in enumerate(batch.candidates)
        )
        accepted = [
            result.candidate
            for result in validation_results
            if result.accepted and result.candidate is not None
        ]
        candidates = _dedupe_candidates(accepted)
        event = ModelExecutionEvent(
            agent=_AGENT_NAME,
            status=ModelExecutionStatus.MODEL_SUCCESS,
            model_name=self._model_name,
        )
        log_model_execution(_LOGGER, event)
        _log_candidate_audit(validation_results)
        return _CuratorAudit(candidates=candidates, validation_results=validation_results)

    async def _fail_or_fallback(
        self,
        payload: MemoryCuratorInput,
        event: ModelExecutionEvent,
        *,
        cause: Exception | None = None,
    ) -> list[MemoryCandidate]:
        """Log one safe event, then raise in strict mode or use fake fallback once."""

        log_model_execution(_LOGGER, event)
        if self._strict_model:
            failure = ModelExecutionFailure(event)
            if cause is not None:
                raise failure from cause
            raise failure
        return await self._fallback.curate(payload)

    def _build_prompt(self, payload: MemoryCuratorInput) -> str:
        payload_json = json.dumps(
            {
                "session_id": payload.session_id,
                "messages": [message.model_dump(mode="json") for message in payload.messages],
                "final_state": payload.final_state.model_dump(mode="json"),
                "finalizer_result": payload.finalizer_result.model_dump(mode="json"),
                "rolling_summary": (
                    payload.rolling_summary.model_dump(mode="json")
                    if payload.rolling_summary is not None
                    else None
                ),
                "interventions": [
                    intervention.model_dump(mode="json")
                    for intervention in (payload.interventions or [])
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        return f"{self._prompt_template}\n\nINPUT_JSON:\n{payload_json}"


def _read_prompt(path: Path, *, fallback: str) -> str:
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return fallback


def _validate_input_relationships(payload: MemoryCuratorInput) -> None:
    """Validate deterministic input relationships before model calls."""

    if payload.final_state.session_id != payload.session_id:
        raise ValueError("memory curator final_state session_id must match payload")
    if payload.finalizer_result.session_id != payload.session_id:
        raise ValueError("memory curator finalizer_result session_id must match payload")
    if (
        payload.rolling_summary is not None
        and payload.rolling_summary.session_id != payload.session_id
    ):
        raise ValueError("memory curator rolling_summary session_id must match payload")

    message_ids: set[MessageId] = set()
    sequences: set[int] = set()
    previous_sequence = 0
    for message in sorted(payload.messages, key=lambda item: item.sequence_number):
        if message.session_id != payload.session_id:
            raise ValueError("memory curator message session_id must match payload")
        if message.id in message_ids:
            raise ValueError("memory curator message IDs must be unique")
        if message.sequence_number in sequences:
            raise ValueError("memory curator message sequence_numbers must be unique")
        if message.sequence_number <= previous_sequence:
            raise ValueError("memory curator message sequence_numbers must increase")
        if message.role not in set(MessageRole):
            raise ValueError("memory curator message role is invalid")
        message_ids.add(message.id)
        sequences.add(message.sequence_number)
        previous_sequence = message.sequence_number

    for source_id in _state_source_ids(payload.final_state):
        if payload.messages and source_id not in message_ids:
            raise ValueError("memory curator state source IDs must belong to messages")
    if payload.rolling_summary is not None and payload.messages:
        if not set(payload.rolling_summary.source_message_ids).issubset(message_ids):
            raise ValueError("memory curator rolling summary source IDs must belong to messages")
    messages_by_id = {message.id: message for message in payload.messages}
    for intervention in payload.interventions or []:
        if intervention.session_id != payload.session_id:
            raise ValueError("memory curator intervention session_id must match payload")
        assistant = messages_by_id.get(intervention.assistant_message_id)
        if assistant is None or assistant.role != MessageRole.ASSISTANT:
            raise ValueError("memory curator intervention assistant_message_id must be assistant")
        if intervention.status == InterventionStatus.EVALUATED and not (
            intervention.observed_response
            and intervention.explicit_feedback
            and intervention.strategy_fit
            and intervention.objective_progress
        ):
            raise ValueError("memory curator evaluated intervention must include feedback")
        if intervention.status == InterventionStatus.PENDING and any(
            value is not None
            for value in (
                intervention.observed_response,
                intervention.explicit_feedback,
                intervention.strategy_fit,
                intervention.objective_progress,
                intervention.recommended_adjustment,
            )
        ):
            raise ValueError("memory curator pending intervention must not include feedback")


def _build_evidence_index(payload: MemoryCuratorInput) -> _EvidenceIndex:
    messages_by_id = {message.id: message for message in payload.messages}
    user_message_ids = {
        message.id for message in payload.messages if message.role == MessageRole.USER
    }
    assistant_message_ids = {
        message.id for message in payload.messages if message.role == MessageRole.ASSISTANT
    }
    state_preference_source_ids = {
        item.source.message_id for item in payload.final_state.user_preferences
    }
    state_goal_source_ids = _state_source_ids(payload.final_state)
    rolling_summary_source_ids = (
        set(payload.rolling_summary.source_message_ids)
        if payload.rolling_summary is not None
        else set()
    )
    evaluated_interventions = {
        str(intervention.intervention_id): intervention
        for intervention in (payload.interventions or [])
        if intervention.status == InterventionStatus.EVALUATED
    }
    evaluated_user_response_ids = {
        response.id
        for intervention in evaluated_interventions.values()
        if (response := _user_response_for_intervention(intervention, messages_by_id)) is not None
    }
    provisional_candidates_by_index = {
        index: candidate
        for index, candidate in enumerate(payload.finalizer_result.candidate_memories)
    }
    return _EvidenceIndex(
        messages_by_id=messages_by_id,
        message_sequence_by_id={
            message.id: message.sequence_number for message in payload.messages
        },
        user_message_ids=user_message_ids,
        assistant_message_ids=assistant_message_ids,
        allowed_source_message_ids={
            *messages_by_id.keys(),
            *state_goal_source_ids,
            *rolling_summary_source_ids,
        },
        explicit_user_source_ids={*user_message_ids, *state_preference_source_ids},
        state_preference_source_ids=state_preference_source_ids,
        state_goal_source_ids=state_goal_source_ids,
        rolling_summary_source_ids=rolling_summary_source_ids,
        evaluated_interventions=evaluated_interventions,
        evaluated_user_response_ids=evaluated_user_response_ids,
        provisional_candidates_by_index=provisional_candidates_by_index,
    )


def _validate_candidate(
    raw_candidate: _RawMemoryCandidateDraft,
    candidate_index: int,
    payload: MemoryCuratorInput,
    index: _EvidenceIndex,
) -> _CandidateValidationResult:
    reasons = list(_candidate_validation_reasons(raw_candidate, index, payload))
    if reasons:
        return _CandidateValidationResult(
            candidate_index=candidate_index,
            accepted=False,
            reason_codes=tuple(dict.fromkeys(reasons)),
            candidate=None,
        )
    candidate = _raw_to_public_candidate(raw_candidate)
    return _CandidateValidationResult(
        candidate_index=candidate_index,
        accepted=True,
        reason_codes=(),
        candidate=candidate,
    )


def _candidate_validation_reasons(
    raw: _RawMemoryCandidateDraft,
    index: _EvidenceIndex,
    payload: MemoryCuratorInput,
) -> tuple[str, ...]:
    reasons: list[str] = []
    content = (raw.content or "").strip()
    if not content:
        reasons.append("empty_candidate_content")

    provenance_indexes = raw.provisional_candidate_indexes
    for provenance_index in provenance_indexes:
        if provenance_index not in index.provisional_candidates_by_index:
            reasons.append("unknown_provisional_candidate_index")

    memory_type = _parse_memory_type(raw.candidate_type)
    if memory_type is None:
        reasons.append("unknown_memory_type")
    source_type = _parse_source_type(raw.source_type)
    if source_type is None:
        reasons.append("unknown_memory_source_type")

    evidence_ids = _evidence_source_ids(raw.evidence, index, reasons)
    if not raw.evidence:
        reasons.append("missing_candidate_evidence")
    if evidence_ids and not set(evidence_ids).issubset(index.allowed_source_message_ids):
        reasons.append("source_message_not_allowed")
    if source_type == MemorySourceType.EXPLICIT_USER_STATEMENT and not (
        set(evidence_ids) & index.explicit_user_source_ids
    ):
        reasons.append("assistant_only_explicit_memory")
    if source_type == MemorySourceType.REPEATED_OBSERVATION:
        reasons.append("single_session_repeated_observation_not_allowed")
    if source_type == MemorySourceType.MODEL_INFERENCE:
        reasons.append("model_inference_not_allowed")

    if content and raw.evidence and not _candidate_content_supported(raw, memory_type, index):
        reasons.append("candidate_content_not_supported_by_evidence")

    operation = _parse_operation(raw.recommended_operation)
    if operation is None:
        reasons.append("unknown_memory_operation")
    elif operation != MemoryOperation.CREATE:
        reasons.append("non_create_operation_not_allowed")

    sensitivity = _parse_sensitivity(raw.sensitivity)
    if sensitivity is None:
        reasons.append("unknown_memory_sensitivity")
    if raw.confidence is None:
        reasons.append("missing_candidate_confidence")
    elif not 0.0 <= raw.confidence <= 1.0:
        reasons.append("invalid_candidate_confidence")
    if raw.requires_user_confirmation is None:
        reasons.append("missing_requires_user_confirmation")
    if memory_type is not None and sensitivity is not None:
        reasons.extend(
            _sensitivity_confirmation_reasons(
                memory_type=memory_type,
                sensitivity=sensitivity,
                confidence=raw.confidence,
                requires_user_confirmation=raw.requires_user_confirmation,
                content=content,
            )
        )

    if memory_type is not None and content:
        reasons.extend(
            _type_specific_reasons(
                raw,
                memory_type,
                source_type,
                index,
                payload,
            )
        )
    unsupported_reason = unsupported_claim_reason(content)
    if unsupported_reason is not None:
        reasons.append(unsupported_reason)
    return tuple(dict.fromkeys(reasons))


def _evidence_source_ids(
    evidence: list[_MessageEvidenceDraft],
    index: _EvidenceIndex,
    reasons: list[str],
) -> list[MessageId]:
    ids: list[MessageId] = []
    for item in evidence:
        if not item.message_id:
            reasons.append("missing_evidence_message_id")
            continue
        message_id = MessageId(item.message_id)
        message = index.messages_by_id.get(message_id)
        if message is None:
            reasons.append("unknown_source_message_id")
            continue
        quote = (item.quote or "").strip()
        if not quote:
            reasons.append("empty_evidence_quote")
            continue
        if _normalize(quote) not in _normalize(message.content):
            reasons.append("evidence_quote_not_found")
        ids.append(message_id)
    return _dedupe(ids)


def _candidate_content_supported(
    raw: _RawMemoryCandidateDraft,
    memory_type: MemoryType | None,
    index: _EvidenceIndex,
) -> bool:
    content = _normalize(raw.content or "")
    if not content:
        return False
    quotes = [
        _normalize(item.quote or "")
        for item in raw.evidence
        if item.message_id and MessageId(item.message_id) in index.messages_by_id
    ]
    if any(quote and (content in quote or quote in content) for quote in quotes):
        return True
    if memory_type is not None and _type_specific_quote_support(memory_type, content, quotes):
        return True
    for provenance_index in raw.provisional_candidate_indexes:
        candidate = index.provisional_candidates_by_index.get(provenance_index)
        if candidate is None or candidate.candidate_type != memory_type:
            continue
        candidate_sources = set(candidate.source_message_ids)
        evidence_sources = {
            MessageId(item.message_id)
            for item in raw.evidence
            if item.message_id is not None
        }
        if candidate_sources & evidence_sources:
            candidate_content = _normalize(candidate.content)
            if content in candidate_content or candidate_content in content:
                return True
            if _type_specific_quote_support(memory_type, content, [candidate_content]):
                return True
    return False


def _type_specific_quote_support(
    memory_type: MemoryType | None,
    content: str,
    quotes: list[str],
) -> bool:
    if memory_type == MemoryType.INTERACTION_PREFERENCE:
        content_has_small_step = _has_any(content, _SMALL_STEP_TERMS)
        quote_has_small_step = any(_has_any(quote, _SMALL_STEP_TERMS) for quote in quotes)
        if content_has_small_step or quote_has_small_step:
            return content_has_small_step and quote_has_small_step
        return _has_any(content, _PREFERENCE_TERMS) and any(
            _has_any(quote, _PREFERENCE_TERMS) for quote in quotes
        )
    if memory_type == MemoryType.ACTIVE_GOAL:
        return _has_any(content, _GOAL_TERMS) and any(
            _has_any(quote, _GOAL_TERMS) for quote in quotes
        )
    if memory_type == MemoryType.UNFINISHED_TOPIC:
        return _has_any(content, _UNFINISHED_TERMS) and any(
            _has_any(quote, _UNFINISHED_TERMS) for quote in quotes
        )
    if memory_type == MemoryType.STRATEGY_OUTCOME:
        return _has_any(content, _STRATEGY_TERMS) and any(
            _has_any(quote, _STRATEGY_TERMS) for quote in quotes
        )
    if memory_type in {MemoryType.SEMANTIC, MemoryType.EPISODIC}:
        return any(quote and (quote in content or content in quote) for quote in quotes)
    return False


def _type_specific_reasons(
    raw: _RawMemoryCandidateDraft,
    memory_type: MemoryType,
    source_type: MemorySourceType | None,
    index: _EvidenceIndex,
    payload: MemoryCuratorInput,
) -> list[str]:
    content = _normalize(raw.content or "")
    evidence_ids = {
        MessageId(item.message_id)
        for item in raw.evidence
        if item.message_id is not None
    }
    reasons: list[str] = []
    if memory_type == MemoryType.INTERACTION_PREFERENCE:
        if _has_any(content, _MEETING_ONLY_TERMS) and not _has_any(content, _PREFERENCE_TERMS):
            reasons.append("meeting_load_not_interaction_preference")
        if not _has_any(content, (*_PREFERENCE_TERMS, *_SMALL_STEP_TERMS)):
            reasons.append("interaction_preference_missing_preference_semantics")
    elif memory_type == MemoryType.ACTIVE_GOAL:
        if not _has_any(content, _GOAL_TERMS):
            reasons.append("active_goal_missing_explicit_goal")
        if not evidence_ids & index.user_message_ids:
            reasons.append("active_goal_requires_user_evidence")
    elif memory_type == MemoryType.UNFINISHED_TOPIC:
        if not _has_any(content, _UNFINISHED_TERMS):
            reasons.append("unfinished_topic_missing_continue_semantics")
        if not evidence_ids & index.user_message_ids:
            reasons.append("unfinished_topic_requires_user_evidence")
    elif memory_type == MemoryType.STRATEGY_OUTCOME:
        if not _has_any(content, _STRATEGY_TERMS):
            reasons.append("strategy_outcome_missing_outcome_semantics")
        if not _strategy_outcome_supported(raw, payload):
            reasons.append("strategy_outcome_without_evaluated_intervention")
    elif memory_type in {MemoryType.SEMANTIC, MemoryType.EPISODIC}:
        if source_type == MemorySourceType.MODEL_INFERENCE:
            reasons.append("model_inference_not_allowed")
    return reasons


def _strategy_outcome_supported(
    raw: _RawMemoryCandidateDraft,
    payload: MemoryCuratorInput,
) -> bool:
    content = _normalize(raw.content or "")
    source_ids = {
        MessageId(item.message_id)
        for item in raw.evidence
        if item.message_id is not None
    }
    public_outcomes: list[StrategyOutcome] = payload.finalizer_result.strategy_outcomes
    if any(
        _normalize(outcome.strategy) in content
        and any(source_id in source_ids for source_id in outcome.source_message_ids)
        for outcome in public_outcomes
    ):
        return True
    for intervention in payload.interventions or []:
        if intervention.status != InterventionStatus.EVALUATED:
            continue
        if _normalize(intervention.strategy) in content:
            return True
    return False


def _sensitivity_confirmation_reasons(
    *,
    memory_type: MemoryType,
    sensitivity: MemorySensitivity,
    confidence: float | None,
    requires_user_confirmation: bool | None,
    content: str,
) -> list[str]:
    reasons: list[str] = []
    needs_confirmation = bool(
        sensitivity in {MemorySensitivity.MEDIUM, MemorySensitivity.HIGH}
        or (confidence is not None and confidence < 0.8)
        or memory_type in {MemoryType.ACTIVE_GOAL, MemoryType.UNFINISHED_TOPIC}
        or _has_any(_normalize(content), _HIGH_SENSITIVITY_TERMS)
    )
    if needs_confirmation and requires_user_confirmation is not True:
        reasons.append("confirmation_required")
    if (
        sensitivity == MemorySensitivity.HIGH
        and requires_user_confirmation is not True
    ):
        reasons.append("high_sensitivity_requires_confirmation")
    if (
        memory_type in _LOW_SENSITIVITY_TYPES
        and sensitivity == MemorySensitivity.HIGH
        and not _has_any(_normalize(content), _HIGH_SENSITIVITY_TERMS)
    ):
        reasons.append("sensitivity_type_mismatch")
    return reasons


def _raw_to_public_candidate(raw: _RawMemoryCandidateDraft) -> MemoryCandidate:
    memory_type = _parse_memory_type(raw.candidate_type)
    source_type = _parse_source_type(raw.source_type)
    sensitivity = _parse_sensitivity(raw.sensitivity)
    operation = _parse_operation(raw.recommended_operation)
    if (
        memory_type is None
        or source_type is None
        or sensitivity is None
        or operation is None
        or raw.confidence is None
        or raw.requires_user_confirmation is None
    ):
        raise AssertionError("validated raw candidate must be complete")
    return MemoryCandidate(
        candidate_type=memory_type,
        content=(raw.content or "").strip(),
        source_message_ids=_dedupe(
            [
                MessageId(item.message_id)
                for item in raw.evidence
                if item.message_id is not None
            ]
        ),
        source_type=source_type,
        confidence=raw.confidence,
        requires_user_confirmation=raw.requires_user_confirmation,
        sensitivity=sensitivity,
        recommended_operation=operation,
    )


def _parse_memory_type(value: str | None) -> MemoryType | None:
    try:
        return MemoryType(value) if value is not None else None
    except ValueError:
        return None


def _parse_source_type(value: str | None) -> MemorySourceType | None:
    try:
        return MemorySourceType(value) if value is not None else None
    except ValueError:
        return None


def _parse_sensitivity(value: str | None) -> MemorySensitivity | None:
    try:
        return MemorySensitivity(value) if value is not None else None
    except ValueError:
        return None


def _parse_operation(value: str | None) -> MemoryOperation | None:
    try:
        return MemoryOperation(value) if value is not None else None
    except ValueError:
        return None


def _is_valid_public_candidate(
    candidate: MemoryCandidate,
    payload: MemoryCuratorInput,
) -> bool:
    if not candidate.content.strip() or not candidate.source_message_ids:
        return False
    index = _build_evidence_index(payload)
    if any(
        source_id not in index.allowed_source_message_ids
        for source_id in candidate.source_message_ids
    ):
        return False
    if candidate.recommended_operation != MemoryOperation.CREATE:
        return False
    if candidate.source_type in {
        MemorySourceType.REPEATED_OBSERVATION,
        MemorySourceType.MODEL_INFERENCE,
    }:
        return False
    if (
        candidate.source_type == MemorySourceType.EXPLICIT_USER_STATEMENT
        and not set(candidate.source_message_ids) & index.explicit_user_source_ids
    ):
        return False
    if unsupported_claim_reason(candidate.content) is not None:
        return False
    if (
        candidate.sensitivity != MemorySensitivity.LOW
        or candidate.confidence < 0.8
        or candidate.candidate_type in {MemoryType.ACTIVE_GOAL, MemoryType.UNFINISHED_TOPIC}
    ) and not candidate.requires_user_confirmation:
        return False
    return True


def _message_candidates(messages: list[Message]) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []
    for message in messages:
        if message.role != MessageRole.USER:
            continue
        content = _canonical_preference(message.content)
        if content is not None:
            candidates.append(_preference_candidate(content, message.id))
    return candidates


def _state_preference_candidates(final_state: SessionState) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []
    for preference in final_state.user_preferences:
        content = _canonical_preference(preference.value)
        if content is not None:
            candidates.append(_preference_candidate(content, preference.source.message_id))
    return candidates


def _canonical_preference(text: str) -> str | None:
    normalized = _normalize(text)
    if _has_any(normalized, _MEETING_ONLY_TERMS) and not _has_any(
        normalized,
        (*_PREFERENCE_TERMS, *_SMALL_STEP_TERMS),
    ):
        return None
    if _has_any(normalized, _SMALL_STEP_TERMS):
        return _SMALL_STEP_CONTENT
    return None


def _preference_candidate(content: str, source_message_id: MessageId) -> MemoryCandidate:
    return MemoryCandidate(
        candidate_type=MemoryType.INTERACTION_PREFERENCE,
        content=content,
        source_message_ids=[source_message_id],
        source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
        confidence=0.9,
        requires_user_confirmation=False,
        sensitivity=MemorySensitivity.LOW,
        recommended_operation=MemoryOperation.CREATE,
    )


def _dedupe_candidates(candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
    result: list[MemoryCandidate] = []
    key_indexes: dict[tuple[MemoryType, str], int] = {}
    for candidate in candidates:
        key = (candidate.candidate_type, _dedupe_content_key(candidate))
        existing_index = key_indexes.get(key)
        if existing_index is not None:
            existing = result[existing_index]
            result[existing_index] = existing.model_copy(
                update={
                    "source_message_ids": _dedupe(
                        [*existing.source_message_ids, *candidate.source_message_ids]
                    ),
                    "confidence": max(existing.confidence, candidate.confidence),
                    "requires_user_confirmation": (
                        existing.requires_user_confirmation
                        or candidate.requires_user_confirmation
                    ),
                    "sensitivity": _higher_sensitivity(
                        existing.sensitivity,
                        candidate.sensitivity,
                    ),
                }
            )
            continue
        key_indexes[key] = len(result)
        result.append(
            candidate.model_copy(
                update={
                    "content": candidate.content.strip(),
                    "source_message_ids": _dedupe(candidate.source_message_ids),
                }
            )
        )
    return result


def _higher_sensitivity(
    first: MemorySensitivity,
    second: MemorySensitivity,
) -> MemorySensitivity:
    order = {
        MemorySensitivity.LOW: 0,
        MemorySensitivity.MEDIUM: 1,
        MemorySensitivity.HIGH: 2,
    }
    return first if order[first] >= order[second] else second


def _dedupe_content_key(candidate: MemoryCandidate) -> str:
    normalized = _normalize(candidate.content)
    if (
        candidate.candidate_type == MemoryType.INTERACTION_PREFERENCE
        and _has_any(normalized, _SMALL_STEP_TERMS)
    ):
        return "interaction_preference:small_step"
    return normalized


def _state_source_ids(final_state: SessionState) -> set[MessageId]:
    ids: set[MessageId] = set()
    ids.update(item.source.message_id for item in final_state.active_topics)
    ids.update(item.source.message_id for item in final_state.reported_emotions)
    ids.update(item.source.message_id for item in final_state.user_preferences)
    ids.update(item.source.message_id for item in final_state.open_questions)
    return ids


def _user_response_for_intervention(
    intervention: InterventionRecord,
    messages_by_id: dict[MessageId, Message],
) -> Message | None:
    assistant = messages_by_id.get(intervention.assistant_message_id)
    if assistant is None:
        return None
    candidates = [
        message
        for message in messages_by_id.values()
        if message.role == MessageRole.USER
        and message.sequence_number > assistant.sequence_number
    ]
    return min(candidates, key=lambda message: message.sequence_number, default=None)


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(_normalize(term) in text for term in terms)


def _dedupe(items: list[ItemT]) -> list[ItemT]:
    result: list[ItemT] = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


def _log_candidate_audit(
    validation_results: tuple[_CandidateValidationResult, ...],
) -> None:
    for result in validation_results:
        _LOGGER.info(
            "memory_candidate_audit",
            extra={
                "agent": _AGENT_NAME,
                "candidate_index": result.candidate_index,
                "accepted": result.accepted,
                "reason_codes": result.reason_codes,
            },
        )


__all__ = [
    "FakeMemoryCurator",
    "MemoryCurator",
    "MemoryCuratorInput",
]
