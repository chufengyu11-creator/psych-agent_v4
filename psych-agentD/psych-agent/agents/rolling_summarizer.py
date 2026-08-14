"""Rolling summarizer implementations."""

import json
import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import Field

from llm.structured_client import StructuredLLMClientProtocol
from schemas.common import ContractModel, MessageId
from schemas.intervention import InterventionRecord, InterventionStatus
from schemas.messages import Message, MessageRole
from schemas.summary import RollingSummarizerInput, RollingSummary
from services.claim_safety import unsupported_claim_reason
from services.model_execution import (
    ModelExecutionEvent,
    ModelExecutionFailure,
    ModelExecutionStatus,
    log_model_execution,
    model_exception_status,
)

ItemT = TypeVar("ItemT")
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "rolling_summarizer.md"
_LOGGER = logging.getLogger(__name__)
_AGENT_NAME = "rolling_summarizer"


class _MessageEvidence(ContractModel):
    """Private field-level quote evidence for one generated summary fact."""

    message_id: MessageId
    quote: str = Field(min_length=1)


class _GroundedTextDraft(ContractModel):
    """Private model text field with message-level quote evidence."""

    value: str = Field(min_length=1)
    evidence: list[_MessageEvidence] = Field(min_length=1)


class _GroundedStrategyAttemptDraft(ContractModel):
    """Private model strategy-attempt field with assistant evidence."""

    strategy: str = Field(min_length=1)
    evidence: list[_MessageEvidence] = Field(min_length=1)


class _GroundedStrategyResponseDraft(ContractModel):
    """Private model strategy-response field with user-response evidence."""

    strategy: str = Field(min_length=1)
    response: str = Field(min_length=1)
    evidence: list[_MessageEvidence] = Field(min_length=1)


class _GroundedRollingSummaryDraft(ContractModel):
    """Private model output containing only new semantic delta fields."""

    current_problem: _GroundedTextDraft | None = None
    session_goal: _GroundedTextDraft | None = None
    important_user_statements: list[_GroundedTextDraft] = Field(default_factory=list)
    strategies_attempted: list[_GroundedStrategyAttemptDraft] = Field(default_factory=list)
    strategy_responses: list[_GroundedStrategyResponseDraft] = Field(default_factory=list)
    open_questions: list[_GroundedTextDraft] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _EvidenceIndex:
    """Typed lookup tables used for deterministic semantic validation."""

    messages_by_id: dict[MessageId, Message]
    user_message_ids: frozenset[MessageId]
    assistant_message_ids: frozenset[MessageId]
    uncovered_message_ids: frozenset[MessageId]
    eligible_interventions: tuple[InterventionRecord, ...]
    evaluated_intervention_user_responses: dict[str, Message]


class FakeRollingSummarizer:
    """Deterministic summarizer used as a safe fallback and test double."""

    async def run(self, payload: RollingSummarizerInput) -> RollingSummary:
        """Alias for summarize so the class satisfies Agent protocol."""

        return await self.summarize(payload)

    async def summarize(self, payload: RollingSummarizerInput) -> RollingSummary:
        """Build a conservative rolling summary from typed inputs."""

        _validate_input_relationships(payload)
        previous = payload.previous_summary
        if not payload.uncovered_messages:
            if previous is not None:
                return previous
            empty_id = MessageId("no_messages")
            return RollingSummary(
                session_id=payload.session_id,
                summary_version=1,
                covered_from=empty_id,
                covered_to=empty_id,
            )

        source_message_ids = _dedupe(
            [
                *(previous.source_message_ids if previous is not None else []),
                *_source_message_ids(payload),
            ]
        )
        important_user_statements = _dedupe(
            [
                *(previous.important_user_statements if previous is not None else []),
                *[message.content for message in _user_messages(payload.uncovered_messages)],
            ]
        )
        strategies_attempted = _dedupe(
            [
                *(previous.strategies_attempted if previous is not None else []),
                *[
                    intervention.strategy
                    for intervention in payload.interventions
                    if intervention.status != InterventionStatus.CANCELLED
                ],
            ]
        )
        strategy_responses = _dedupe(
            [
                *(previous.strategy_responses if previous is not None else []),
                *_strategy_responses(payload.interventions),
            ]
        )
        open_questions = _dedupe(
            [
                *(previous.open_questions if previous is not None else []),
                *[item.value for item in payload.current_state.open_questions],
                *_explicit_user_questions(payload.uncovered_messages),
            ]
        )

        return RollingSummary(
            session_id=payload.session_id,
            summary_version=_summary_version(payload),
            covered_from=_covered_from(payload),
            covered_to=_covered_to(payload),
            current_problem=_current_problem(payload)
            or (previous.current_problem if previous is not None else None),
            session_goal=_grounded_session_goal(payload)
            or (previous.session_goal if previous is not None else None),
            important_user_statements=important_user_statements,
            strategies_attempted=strategies_attempted,
            strategy_responses=strategy_responses,
            open_questions=open_questions,
            source_message_ids=source_message_ids,
        )


class RollingSummarizer:
    """Model-backed rolling summarizer with deterministic fallback behavior."""

    def __init__(
        self,
        llm_client: StructuredLLMClientProtocol | None = None,
        *,
        model_name: str | None = None,
        fallback: FakeRollingSummarizer | None = None,
        prompt_template: str | None = None,
        strict_model: bool = False,
    ) -> None:
        """Create a summarizer with optional structured model dependency."""

        self._llm_client = llm_client
        self._model_name = model_name
        self._fallback = fallback or FakeRollingSummarizer()
        self._strict_model = strict_model
        self._prompt_template = prompt_template or _read_prompt(
            _PROMPT_PATH,
            fallback=(
                "Update a rolling summary from only the new uncovered-message delta. "
                "Return one private grounded draft JSON object."
            ),
        )

    async def run(self, payload: RollingSummarizerInput) -> RollingSummary:
        """Alias for summarize so the class satisfies Agent protocol."""

        return await self.summarize(payload)

    async def summarize(self, payload: RollingSummarizerInput) -> RollingSummary:
        """Summarize older session messages through grounded drafts or fallback."""

        _validate_input_relationships(payload)
        if not payload.uncovered_messages:
            if payload.previous_summary is not None:
                return payload.previous_summary
            empty_id = MessageId("no_messages")
            return RollingSummary(
                session_id=payload.session_id,
                summary_version=1,
                covered_from=empty_id,
                covered_to=empty_id,
            )
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
                _GroundedRollingSummaryDraft,
                model_name=self._model_name,
                metadata={"agent": _AGENT_NAME},
            )
            draft = _GroundedRollingSummaryDraft.model_validate(raw_draft)
        except Exception as error:
            status, reasons = model_exception_status(error)
            event = ModelExecutionEvent(
                agent=_AGENT_NAME,
                status=status,
                model_name=self._model_name,
                reason_codes=reasons,
            )
            return await self._fail_or_fallback(payload, event, cause=error)

        reasons = _summary_validation_reasons(draft, payload)
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
        return _draft_to_summary(draft, payload)

    async def _fail_or_fallback(
        self,
        payload: RollingSummarizerInput,
        event: ModelExecutionEvent,
        *,
        cause: Exception | None = None,
    ) -> RollingSummary:
        """Log one safe event, then raise in strict mode or use the fake."""

        log_model_execution(_LOGGER, event)
        if self._strict_model:
            failure = ModelExecutionFailure(event)
            if cause is not None:
                raise failure from cause
            raise failure
        return await self._fallback.summarize(payload)

    def _build_prompt(self, payload: RollingSummarizerInput) -> str:
        """Render a structured prompt from the typed summarizer input."""

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


def _validate_input_relationships(payload: RollingSummarizerInput) -> None:
    """Validate the typed input graph before any model or fallback work."""

    if (
        payload.previous_summary is not None
        and payload.previous_summary.session_id != payload.session_id
    ):
        raise ValueError("summary input previous_summary session ID must match")
    if payload.current_state.session_id != payload.session_id:
        raise ValueError("summary input current_state session ID must match")

    seen_message_ids: set[MessageId] = set()
    previous_sequence: int | None = None
    for message in payload.uncovered_messages:
        if message.session_id != payload.session_id:
            raise ValueError("summary input uncovered message session IDs must match")
        if message.id in seen_message_ids:
            raise ValueError("summary input uncovered message IDs must be unique")
        seen_message_ids.add(message.id)
        if previous_sequence is not None and message.sequence_number <= previous_sequence:
            raise ValueError("summary input uncovered sequence numbers must increase")
        previous_sequence = message.sequence_number
        if message.role not in set(MessageRole):
            raise ValueError("summary input uncovered message role must be known")

    for intervention in payload.interventions:
        if intervention.session_id != payload.session_id:
            raise ValueError("summary input intervention session IDs must match")
        assistant_message = next(
            (
                message
                for message in payload.uncovered_messages
                if message.id == intervention.assistant_message_id
            ),
            None,
        )
        if assistant_message is not None and assistant_message.role != MessageRole.ASSISTANT:
            raise ValueError(
                "summary input intervention assistant message ID must reference assistant"
            )


def _summary_version(payload: RollingSummarizerInput) -> int:
    """Return the next summary version for the payload."""

    if payload.previous_summary is None:
        return 1
    return payload.previous_summary.summary_version + 1


def _covered_from(payload: RollingSummarizerInput) -> MessageId:
    """Return the first message covered by the summary."""

    if payload.previous_summary is not None:
        return payload.previous_summary.covered_from
    if payload.uncovered_messages:
        return payload.uncovered_messages[0].id
    return MessageId("no_messages")


def _covered_to(payload: RollingSummarizerInput) -> MessageId:
    """Return the last message covered by the summary."""

    if payload.uncovered_messages:
        return payload.uncovered_messages[-1].id
    if payload.previous_summary is not None:
        return payload.previous_summary.covered_to
    return MessageId("no_messages")


def _user_messages(messages: list[Message]) -> list[Message]:
    """Return only user-authored messages."""

    return [message for message in messages if message.role == MessageRole.USER]


def _current_problem(payload: RollingSummarizerInput) -> str | None:
    """Use state facts first, then the first user message as a conservative fallback."""

    uncovered_ids = {message.id for message in payload.uncovered_messages}
    parts = [
        item.value
        for item in [*payload.current_state.active_topics, *payload.current_state.reported_emotions]
        if item.active and item.source.message_id in uncovered_ids
    ]
    if parts:
        return "; ".join(_dedupe(parts))
    user_messages = _user_messages(payload.uncovered_messages)
    if user_messages:
        return user_messages[0].content
    return None


def _grounded_session_goal(payload: RollingSummarizerInput) -> str | None:
    """Use a state goal only when a current user-sourced state item is covered."""

    if payload.current_state.session_goal is None:
        return None
    uncovered_user_ids = {
        message.id
        for message in payload.uncovered_messages
        if message.role == MessageRole.USER
    }
    state_items = [
        *payload.current_state.active_topics,
        *payload.current_state.reported_emotions,
        *payload.current_state.user_preferences,
        *payload.current_state.open_questions,
    ]
    if any(item.active and item.source.message_id in uncovered_user_ids for item in state_items):
        return payload.current_state.session_goal
    return None


def _strategy_responses(interventions: list[InterventionRecord]) -> list[str]:
    """Summarize only evaluated intervention responses."""

    responses: list[str] = []
    for intervention in interventions:
        if intervention.status != InterventionStatus.EVALUATED:
            continue
        details = [
            part
            for part in [
                intervention.observed_response,
                intervention.explicit_feedback,
                intervention.strategy_fit,
                intervention.objective_progress,
            ]
            if part
        ]
        if details:
            responses.append(f"{intervention.strategy}: {'; '.join(details)}")
    return responses


def _explicit_user_questions(messages: list[Message]) -> list[str]:
    """Keep question-like user requests without inventing new open loops."""

    return [
        message.content
        for message in _user_messages(messages)
        if "?" in message.content or "\uff1f" in message.content
    ]


def _source_message_ids(payload: RollingSummarizerInput) -> list[MessageId]:
    """Collect source message IDs for deterministic fallback summary fields."""

    uncovered_ids = {message.id for message in payload.uncovered_messages}
    ids = [message.id for message in _user_messages(payload.uncovered_messages)]
    ids.extend(
        item.source.message_id
        for item in payload.current_state.active_topics
        if item.source.message_id in uncovered_ids
    )
    ids.extend(
        item.source.message_id
        for item in payload.current_state.reported_emotions
        if item.source.message_id in uncovered_ids
    )
    ids.extend(
        item.source.message_id
        for item in payload.current_state.user_preferences
        if item.source.message_id in uncovered_ids
    )
    ids.extend(
        item.source.message_id
        for item in payload.current_state.open_questions
        if item.source.message_id in uncovered_ids
    )
    ids.extend(
        intervention.assistant_message_id
        for intervention in payload.interventions
        if intervention.status != InterventionStatus.CANCELLED
        and intervention.assistant_message_id in uncovered_ids
    )
    return _dedupe(ids)


def _evidence_index(payload: RollingSummarizerInput) -> _EvidenceIndex:
    """Build deterministic evidence lookup structures for one payload."""

    messages_by_id = {message.id: message for message in payload.uncovered_messages}
    user_message_ids = frozenset(
        message.id for message in payload.uncovered_messages if message.role == MessageRole.USER
    )
    assistant_message_ids = frozenset(
        message.id
        for message in payload.uncovered_messages
        if message.role == MessageRole.ASSISTANT
    )
    uncovered_message_ids = frozenset(messages_by_id)
    eligible_interventions = tuple(
        intervention
        for intervention in payload.interventions
        if intervention.status != InterventionStatus.CANCELLED
        and intervention.assistant_message_id in assistant_message_ids
    )
    evaluated_responses: dict[str, Message] = {}
    for intervention in eligible_interventions:
        if intervention.status != InterventionStatus.EVALUATED:
            continue
        user_response = _covered_user_response_for_intervention(
            intervention,
            payload.uncovered_messages,
        )
        if user_response is not None:
            evaluated_responses[intervention.intervention_id] = user_response
    return _EvidenceIndex(
        messages_by_id=messages_by_id,
        user_message_ids=user_message_ids,
        assistant_message_ids=assistant_message_ids,
        uncovered_message_ids=uncovered_message_ids,
        eligible_interventions=eligible_interventions,
        evaluated_intervention_user_responses=evaluated_responses,
    )


def _covered_user_response_for_intervention(
    intervention: InterventionRecord,
    messages: list[Message],
) -> Message | None:
    """Return the first covered user response after an assistant intervention."""

    assistant_message = next(
        (message for message in messages if message.id == intervention.assistant_message_id),
        None,
    )
    if assistant_message is None:
        return None
    return next(
        (
            message
            for message in messages
            if message.role == MessageRole.USER
            and message.sequence_number > assistant_message.sequence_number
        ),
        None,
    )


def _summary_validation_reasons(
    draft: _GroundedRollingSummaryDraft,
    payload: RollingSummarizerInput,
) -> tuple[str, ...]:
    """Return stable semantic reason codes for an invalid grounded draft."""

    index = _evidence_index(payload)
    reasons: list[str] = []

    if draft.current_problem is not None:
        _extend_evidence_reasons(
            reasons,
            draft.current_problem.evidence,
            index,
            required_role=MessageRole.USER,
            required_role_reason="current_problem_requires_user_source",
        )

    if draft.session_goal is not None:
        _extend_evidence_reasons(
            reasons,
            draft.session_goal.evidence,
            index,
            required_role=MessageRole.USER,
            required_role_reason="session_goal_requires_user_source",
        )

    for statement in draft.important_user_statements:
        _extend_evidence_reasons(
            reasons,
            statement.evidence,
            index,
            required_role=MessageRole.USER,
            required_role_reason="important_statement_requires_user_source",
        )

    for attempt in draft.strategies_attempted:
        matching = _matching_intervention(attempt.strategy, index.eligible_interventions)
        _extend_evidence_reasons(
            reasons,
            attempt.evidence,
            index,
            required_role=MessageRole.ASSISTANT,
            required_role_reason="strategy_attempt_requires_assistant_source",
        )
        if matching is None:
            reasons.append("strategy_attempt_not_linked_to_intervention")
        elif not any(
            evidence.message_id == matching.assistant_message_id
            for evidence in attempt.evidence
        ):
            reasons.append("strategy_attempt_not_linked_to_intervention")

    for response in draft.strategy_responses:
        matching = _matching_intervention(response.strategy, index.eligible_interventions)
        _extend_evidence_reasons(
            reasons,
            response.evidence,
            index,
            required_role=MessageRole.USER,
            required_role_reason="strategy_response_requires_user_source",
        )
        if matching is None:
            reasons.append("strategy_response_not_linked_to_intervention")
            if any(
                intervention.strategy == response.strategy
                and intervention.status == InterventionStatus.PENDING
                for intervention in index.eligible_interventions
            ):
                reasons.append("strategy_response_from_pending_intervention")
            continue
        if matching.status == InterventionStatus.PENDING:
            reasons.append("strategy_response_from_pending_intervention")
            continue
        user_response = index.evaluated_intervention_user_responses.get(
            matching.intervention_id
        )
        if user_response is None:
            reasons.append("strategy_response_missing_covered_user_response")
        elif not any(
            evidence.message_id == user_response.id for evidence in response.evidence
        ):
            reasons.append("strategy_response_missing_covered_user_response")

    for question in draft.open_questions:
        _extend_evidence_reasons(
            reasons,
            question.evidence,
            index,
            required_role=MessageRole.USER,
            required_role_reason="open_question_requires_user_source",
        )

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
    required_role: MessageRole,
    required_role_reason: str,
) -> None:
    """Append stable evidence validation reasons for one field."""

    if not evidence_items:
        reasons.append("missing_summary_field_evidence")
        return
    for evidence in evidence_items:
        normalized_quote = _normalize(evidence.quote)
        if not normalized_quote:
            reasons.append("empty_summary_evidence_quote")
            continue
        if evidence.message_id not in index.messages_by_id:
            reasons.append("unknown_summary_source_message")
            reasons.append("summary_source_outside_uncovered_messages")
            continue
        if evidence.message_id not in index.uncovered_message_ids:
            reasons.append("summary_source_outside_uncovered_messages")
            continue
        message = index.messages_by_id[evidence.message_id]
        if message.role != required_role:
            reasons.append(required_role_reason)
        if normalized_quote not in _normalize(message.content):
            reasons.append("summary_evidence_quote_not_found")


def _matching_intervention(
    strategy: str,
    interventions: Iterable[InterventionRecord],
) -> InterventionRecord | None:
    """Return the first eligible intervention with an exact strategy match."""

    return next(
        (intervention for intervention in interventions if intervention.strategy == strategy),
        None,
    )


def _draft_texts_for_claim_safety(
    draft: _GroundedRollingSummaryDraft,
) -> tuple[str, ...]:
    """Collect all private draft semantic text for shared claim-safety checks."""

    texts: list[str] = []
    if draft.current_problem is not None:
        texts.append(draft.current_problem.value)
    if draft.session_goal is not None:
        texts.append(draft.session_goal.value)
    texts.extend(item.value for item in draft.important_user_statements)
    texts.extend(item.strategy for item in draft.strategies_attempted)
    texts.extend(item.response for item in draft.strategy_responses)
    texts.extend(item.value for item in draft.open_questions)
    return tuple(texts)


def _draft_to_summary(
    draft: _GroundedRollingSummaryDraft,
    payload: RollingSummarizerInput,
) -> RollingSummary:
    """Discard private evidence and produce the unchanged public summary contract."""

    previous = payload.previous_summary
    new_source_ids = _dedupe(
        [
            evidence.message_id
            for evidence in _draft_evidence_items(draft)
        ]
    )
    return RollingSummary(
        session_id=payload.session_id,
        summary_version=_summary_version(payload),
        covered_from=_covered_from(payload),
        covered_to=_covered_to(payload),
        current_problem=(
            draft.current_problem.value
            if draft.current_problem is not None
            else previous.current_problem if previous is not None else None
        ),
        session_goal=(
            draft.session_goal.value
            if draft.session_goal is not None
            else previous.session_goal if previous is not None else None
        ),
        important_user_statements=_dedupe(
            [
                *(previous.important_user_statements if previous is not None else []),
                *[item.value for item in draft.important_user_statements],
            ]
        ),
        strategies_attempted=_dedupe(
            [
                *(previous.strategies_attempted if previous is not None else []),
                *[item.strategy for item in draft.strategies_attempted],
            ]
        ),
        strategy_responses=_dedupe(
            [
                *(previous.strategy_responses if previous is not None else []),
                *[
                    f"{item.strategy}: {item.response}"
                    for item in draft.strategy_responses
                ],
            ]
        ),
        open_questions=_dedupe(
            [
                *(previous.open_questions if previous is not None else []),
                *[item.value for item in draft.open_questions],
            ]
        ),
        source_message_ids=_dedupe(
            [
                *(previous.source_message_ids if previous is not None else []),
                *new_source_ids,
            ]
        ),
    )


def _draft_evidence_items(
    draft: _GroundedRollingSummaryDraft,
) -> tuple[_MessageEvidence, ...]:
    """Return all field-level evidence in stable field order."""

    items: list[_MessageEvidence] = []
    if draft.current_problem is not None:
        items.extend(draft.current_problem.evidence)
    if draft.session_goal is not None:
        items.extend(draft.session_goal.evidence)
    for statement in draft.important_user_statements:
        items.extend(statement.evidence)
    for attempt in draft.strategies_attempted:
        items.extend(attempt.evidence)
    for response in draft.strategy_responses:
        items.extend(response.evidence)
    for question in draft.open_questions:
        items.extend(question.evidence)
    return tuple(items)


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


__all__ = ["FakeRollingSummarizer", "RollingSummarizer"]
