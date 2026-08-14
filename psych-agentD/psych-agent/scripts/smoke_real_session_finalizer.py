"""Run strict real-model smoke cases for SessionFinalizer with sanitized output."""

# ruff: noqa: E402 -- direct script execution needs the repository root on sys.path.

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agents.session_finalizer import SessionFinalizer
from app.config import Settings
from llm.exceptions import LLMError
from llm.local_client import OpenAICompatibleHTTPClient
from llm.structured_client import StructuredLLMClient
from schemas.common import InterventionId, MessageId, SessionId
from schemas.intervention import InterventionRecord, InterventionStatus
from schemas.messages import Message, MessageRole
from schemas.risk import RiskLevel
from schemas.state import RiskState, SessionState
from schemas.summary import SessionFinalizerInput, SessionFinalizerResult
from services.model_execution import ModelExecutionFailure


@dataclass(frozen=True)
class SmokeCase:
    """One artificial, non-sensitive finalizer smoke case."""

    case_id: str
    payload: SessionFinalizerInput
    expected_action_items: int | None = None
    expected_strategy_outcomes: int | None = None
    expected_risk_events: int | None = None
    expected_provisional_memories_min: int = 0


@dataclass(frozen=True)
class CaseResult:
    """Sanitized observable result for one smoke case."""

    case_id: str
    agent: str
    model_name: str
    status: str
    reason_codes: tuple[str, ...]
    action_item_count: int
    strategy_outcome_count: int
    risk_event_count: int
    provisional_memory_count: int
    source_id_valid: bool
    quote_valid: bool
    fallback_count: int
    unknown_source_id_count: int
    cross_session_source_count: int
    assistant_only_action_item_count: int
    uncommitted_action_item_count: int
    pending_outcome_count: int
    cancelled_outcome_count: int
    feedback_mismatch_count: int
    false_risk_event_count: int
    risk_level_mismatch_count: int
    direct_memory_persistence_count: int


class _ExecutionLogCapture(logging.Handler):
    """Capture model-execution log metadata without rendering private content."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() == "model_execution":
            self.records.append(record)


@dataclass(frozen=True)
class _CaseValidation:
    unknown_source_id_count: int
    cross_session_source_count: int
    assistant_only_action_item_count: int
    uncommitted_action_item_count: int
    pending_outcome_count: int
    cancelled_outcome_count: int
    feedback_mismatch_count: int
    false_risk_event_count: int
    risk_level_mismatch_count: int
    direct_memory_persistence_count: int

    @property
    def passed(self) -> bool:
        return all(value == 0 for value in self.__dict__.values())


def _message(
    session_id: SessionId,
    message_id: str,
    role: MessageRole,
    text: str,
    sequence: int,
) -> Message:
    return Message(
        id=MessageId(message_id),
        session_id=session_id,
        role=role,
        content=text,
        sequence_number=sequence,
    )


def _case_a() -> SmokeCase:
    session_id = SessionId("real_finalizer_case_a")
    messages = [
        _message(session_id, "live_a_001", MessageRole.USER, "I feel work pressure this week.", 1),
        _message(session_id, "live_a_002", MessageRole.ASSISTANT, "We can slow this down.", 2),
        _message(
            session_id,
            "live_a_003",
            MessageRole.USER,
            "That helped a little, but please give me one step at a time.",
            3,
        ),
        _message(session_id, "live_a_004", MessageRole.ASSISTANT, "Try one sentence.", 4),
        _message(
            session_id,
            "live_a_005",
            MessageRole.USER,
            "I will write one sentence to my manager tomorrow.",
            5,
        ),
        _message(
            session_id,
            "live_a_006",
            MessageRole.USER,
            "I want to continue the work pressure topic next time.",
            6,
        ),
    ]
    return SmokeCase(
        case_id="normal_low_risk",
        payload=SessionFinalizerInput(
            session_id=session_id,
            messages=messages,
            final_state=SessionState(session_id=session_id),
            interventions=[
                InterventionRecord(
                    intervention_id=InterventionId("live_a_int_001"),
                    session_id=session_id,
                    assistant_message_id=MessageId("live_a_002"),
                    strategy="small_step_support",
                    objective="offer one manageable step",
                    status=InterventionStatus.EVALUATED,
                    observed_response="mixed feedback with a format preference",
                    explicit_feedback="mixed",
                    strategy_fit="mixed",
                    objective_progress="partial",
                    recommended_adjustment="continue_one_step_at_a_time",
                )
            ],
        ),
        expected_action_items=1,
        expected_strategy_outcomes=1,
        expected_risk_events=0,
        expected_provisional_memories_min=1,
    )


def _case_b() -> SmokeCase:
    session_id = SessionId("real_finalizer_case_b")
    messages = [
        _message(session_id, "live_b_001", MessageRole.USER, "Work feels busy this week.", 1),
        _message(session_id, "live_b_002", MessageRole.ASSISTANT, "You could write a plan.", 2),
        _message(session_id, "live_b_003", MessageRole.USER, "I am still thinking about it.", 3),
    ]
    return SmokeCase(
        case_id="no_action_commitment",
        payload=SessionFinalizerInput(
            session_id=session_id,
            messages=messages,
            final_state=SessionState(session_id=session_id),
        ),
        expected_action_items=0,
        expected_risk_events=0,
    )


def _case_c() -> SmokeCase:
    session_id = SessionId("real_finalizer_case_c")
    messages = [
        _message(session_id, "live_c_001", MessageRole.USER, "I want a simple first step.", 1),
        _message(session_id, "live_c_002", MessageRole.ASSISTANT, "Let us identify one step.", 2),
    ]
    return SmokeCase(
        case_id="pending_intervention",
        payload=SessionFinalizerInput(
            session_id=session_id,
            messages=messages,
            final_state=SessionState(session_id=session_id),
            interventions=[
                InterventionRecord(
                    intervention_id=InterventionId("live_c_int_001"),
                    session_id=session_id,
                    assistant_message_id=MessageId("live_c_002"),
                    strategy="clarify_first_step",
                    objective="identify one first step",
                    status=InterventionStatus.PENDING,
                )
            ],
        ),
        expected_strategy_outcomes=0,
    )


def _case_d() -> SmokeCase:
    session_id = SessionId("real_finalizer_case_d")
    messages = [
        _message(session_id, "live_d_001", MessageRole.USER, "I feel ordinary work stress.", 1),
        _message(session_id, "live_d_002", MessageRole.ASSISTANT, "We can keep it practical.", 2),
    ]
    return SmokeCase(
        case_id="ordinary_stress_no_risk",
        payload=SessionFinalizerInput(
            session_id=session_id,
            messages=messages,
            final_state=SessionState(session_id=session_id),
        ),
        expected_risk_events=0,
    )


def _case_e() -> SmokeCase:
    session_id = SessionId("real_finalizer_case_e")
    messages = [
        _message(
            session_id,
            "live_e_001",
            MessageRole.USER,
            "Please remember that I prefer one practical step at a time in these chats.",
            1,
        ),
        _message(session_id, "live_e_002", MessageRole.ASSISTANT, "Understood.", 2),
    ]
    return SmokeCase(
        case_id="provisional_memory",
        payload=SessionFinalizerInput(
            session_id=session_id,
            messages=messages,
            final_state=SessionState(session_id=session_id),
        ),
        expected_risk_events=0,
        expected_provisional_memories_min=1,
    )


def _case_f() -> SmokeCase:
    session_id = SessionId("real_finalizer_case_f")
    messages = [
        _message(session_id, "live_f_001", MessageRole.USER, "I am overwhelmed by work.", 1),
        _message(
            session_id,
            "live_f_002",
            MessageRole.ASSISTANT,
            "One possible step is to draft a short email.",
            2,
        ),
        _message(session_id, "live_f_003", MessageRole.USER, "I need to think first.", 3),
    ]
    return SmokeCase(
        case_id="assistant_suggestion_only",
        payload=SessionFinalizerInput(
            session_id=session_id,
            messages=messages,
            final_state=SessionState(session_id=session_id),
        ),
        expected_action_items=0,
        expected_risk_events=0,
    )


def _case_g() -> SmokeCase:
    session_id = SessionId("real_finalizer_case_g")
    messages = [
        _message(
            session_id,
            "live_g_001",
            MessageRole.USER,
            "I feel anxious and sad about work.",
            1,
        ),
        _message(session_id, "live_g_002", MessageRole.ASSISTANT, "That sounds heavy.", 2),
        _message(session_id, "live_g_003", MessageRole.USER, "I am safe; I just want to vent.", 3),
    ]
    return SmokeCase(
        case_id="no_typed_risk_support",
        payload=SessionFinalizerInput(
            session_id=session_id,
            messages=messages,
            final_state=SessionState(
                session_id=session_id,
                risk_state=RiskState(level=RiskLevel.LOW),
            ),
        ),
        expected_risk_events=0,
    )


def _cases() -> list[SmokeCase]:
    return [_case_a(), _case_b(), _case_c(), _case_d(), _case_e(), _case_f(), _case_g()]


async def _run_case(
    finalizer: SessionFinalizer,
    case: SmokeCase,
    model_name: str,
    capture: _ExecutionLogCapture,
) -> tuple[CaseResult, bool]:
    start_index = len(capture.records)
    try:
        result = await finalizer.finalize(case.payload)
    except ModelExecutionFailure as exc:
        fallback_count = sum(
            1
            for record in capture.records[start_index:]
            if str(record.__dict__.get("status", "")).startswith("fallback_")
        )
        return (
            CaseResult(
                case_id=case.case_id,
                agent="session_finalizer",
                model_name=model_name,
                status=exc.status.value,
                reason_codes=exc.reason_codes,
                action_item_count=0,
                strategy_outcome_count=0,
                risk_event_count=0,
                provisional_memory_count=0,
                source_id_valid=False,
                quote_valid=False,
                fallback_count=fallback_count,
                unknown_source_id_count=0,
                cross_session_source_count=0,
                assistant_only_action_item_count=0,
                uncommitted_action_item_count=0,
                pending_outcome_count=0,
                cancelled_outcome_count=0,
                feedback_mismatch_count=0,
                false_risk_event_count=0,
                risk_level_mismatch_count=0,
                direct_memory_persistence_count=0,
            ),
            False,
        )

    records = capture.records[start_index:]
    status = str(records[-1].__dict__.get("status", "missing")) if records else "missing"
    reason_codes = (
        tuple(str(item) for item in records[-1].__dict__.get("reason_codes", ()))
        if records
        else ()
    )
    fallback_count = sum(
        1
        for record in records
        if str(record.__dict__.get("status", "")).startswith("fallback_")
    )
    source_id_valid = _source_ids_valid(result, case.payload)
    validation = _validate_result(result, case.payload)
    passed = (
        status == "model_success"
        and fallback_count == 0
        and source_id_valid
        and validation.passed
        and (
            case.expected_action_items is None
            or len(result.action_items) == case.expected_action_items
        )
        and (
            case.expected_strategy_outcomes is None
            or len(result.strategy_outcomes) == case.expected_strategy_outcomes
        )
        and (
            case.expected_risk_events is None
            or len(result.risk_events) == case.expected_risk_events
        )
        and len(result.candidate_memories) >= case.expected_provisional_memories_min
    )
    return (
        CaseResult(
            case_id=case.case_id,
            agent="session_finalizer",
            model_name=model_name,
            status=status,
            reason_codes=reason_codes,
            action_item_count=len(result.action_items),
            strategy_outcome_count=len(result.strategy_outcomes),
            risk_event_count=len(result.risk_events),
            provisional_memory_count=len(result.candidate_memories),
            source_id_valid=source_id_valid,
            quote_valid=status == "model_success",
            fallback_count=fallback_count,
            unknown_source_id_count=validation.unknown_source_id_count,
            cross_session_source_count=validation.cross_session_source_count,
            assistant_only_action_item_count=validation.assistant_only_action_item_count,
            uncommitted_action_item_count=validation.uncommitted_action_item_count,
            pending_outcome_count=validation.pending_outcome_count,
            cancelled_outcome_count=validation.cancelled_outcome_count,
            feedback_mismatch_count=validation.feedback_mismatch_count,
            false_risk_event_count=validation.false_risk_event_count,
            risk_level_mismatch_count=validation.risk_level_mismatch_count,
            direct_memory_persistence_count=validation.direct_memory_persistence_count,
        ),
        passed,
    )


def _source_ids_valid(
    result: SessionFinalizerResult,
    payload: SessionFinalizerInput,
) -> bool:
    allowed = {message.id for message in payload.messages}
    source_ids = list(result.source_message_ids)
    source_ids.extend(
        source_id
        for item in result.action_items
        for source_id in item.source_message_ids
    )
    source_ids.extend(
        source_id
        for item in result.strategy_outcomes
        for source_id in item.source_message_ids
    )
    source_ids.extend(
        source_id
        for item in result.candidate_memories
        for source_id in item.source_message_ids
    )
    return bool(source_ids) and all(source_id in allowed for source_id in source_ids)


def _validate_result(
    result: SessionFinalizerResult,
    payload: SessionFinalizerInput,
) -> _CaseValidation:
    allowed = {message.id for message in payload.messages}
    message_sessions = {message.id: message.session_id for message in payload.messages}
    user_ids = {message.id for message in payload.messages if message.role is MessageRole.USER}
    all_source_ids: list[MessageId] = list(result.source_message_ids)
    all_source_ids.extend(
        source_id
        for item in result.action_items
        for source_id in item.source_message_ids
    )
    all_source_ids.extend(
        source_id
        for item in result.strategy_outcomes
        for source_id in item.source_message_ids
    )
    all_source_ids.extend(
        source_id
        for item in result.candidate_memories
        for source_id in item.source_message_ids
    )
    unknown_source_id_count = sum(1 for source_id in all_source_ids if source_id not in allowed)
    cross_session_source_count = sum(
        1 for source_id in all_source_ids if message_sessions.get(source_id) != payload.session_id
    )
    assistant_only_action_item_count = sum(
        1
        for item in result.action_items
        if not any(source_id in user_ids for source_id in item.source_message_ids)
    )
    uncommitted_action_item_count = sum(
        1
        for item in result.action_items
        if not any(
            _has_commitment(message.content)
            for message in payload.messages
            if message.id in item.source_message_ids
        )
    )
    pending_outcome_count = 0
    cancelled_outcome_count = 0
    feedback_mismatch_count = 0
    intervention_by_strategy = {item.strategy: item for item in payload.interventions}
    for outcome in result.strategy_outcomes:
        intervention = intervention_by_strategy.get(outcome.strategy)
        if intervention is None:
            feedback_mismatch_count += 1
            continue
        if intervention.status is InterventionStatus.PENDING:
            pending_outcome_count += 1
        if intervention.status is InterventionStatus.CANCELLED:
            cancelled_outcome_count += 1
        if not _outcome_matches_feedback(outcome.outcome, intervention.explicit_feedback):
            feedback_mismatch_count += 1
    typed_risk_supported = payload.final_state.risk_state.level is not RiskLevel.LOW
    false_risk_event_count = len(result.risk_events) if not typed_risk_supported else 0
    risk_level_mismatch_count = 0
    direct_memory_persistence_count = sum(
        1
        for candidate in result.candidate_memories
        if candidate.recommended_operation.value != "CREATE"
    )
    return _CaseValidation(
        unknown_source_id_count=unknown_source_id_count,
        cross_session_source_count=cross_session_source_count,
        assistant_only_action_item_count=assistant_only_action_item_count,
        uncommitted_action_item_count=uncommitted_action_item_count,
        pending_outcome_count=pending_outcome_count,
        cancelled_outcome_count=cancelled_outcome_count,
        feedback_mismatch_count=feedback_mismatch_count,
        false_risk_event_count=false_risk_event_count,
        risk_level_mismatch_count=risk_level_mismatch_count,
        direct_memory_persistence_count=direct_memory_persistence_count,
    )


def _has_commitment(text: str) -> bool:
    normalized = text.casefold()
    return any(
        term in normalized
        for term in (
            "i will",
            "i'll",
            "please give me",
            "please remember",
            "i prefer",
            "i want",
        )
    )


def _outcome_matches_feedback(outcome: str, explicit_feedback: str | None) -> bool:
    if not explicit_feedback:
        return True
    normalized = outcome.casefold()
    feedback = explicit_feedback.casefold()
    if feedback == "mixed":
        return any(term in normalized for term in ("mixed", "partial", "partly", "some"))
    if feedback in {"positive", "helpful", "accepted"}:
        return any(term in normalized for term in ("positive", "helpful", "accepted", "achieved"))
    if feedback in {"negative", "unhelpful", "rejected"}:
        return any(term in normalized for term in ("negative", "unhelpful", "rejected", "not"))
    return feedback in normalized


def _render(result: CaseResult) -> str:
    return (
        f"case_id={result.case_id} agent={result.agent} model_name={result.model_name} "
        f"status={result.status} reason_codes={result.reason_codes} "
        f"counts={{action_items:{result.action_item_count},"
        f"strategy_outcomes:{result.strategy_outcome_count},"
        f"risk_events:{result.risk_event_count},"
        f"provisional_memories:{result.provisional_memory_count}}} "
        f"source_id_valid={result.source_id_valid} "
        f"quote_valid={result.quote_valid} fallback_count={result.fallback_count} "
        f"validation={{unknown_source_id:{result.unknown_source_id_count},"
        f"cross_session_source:{result.cross_session_source_count},"
        f"assistant_only_action_item:{result.assistant_only_action_item_count},"
        f"uncommitted_action_item:{result.uncommitted_action_item_count},"
        f"pending_outcome:{result.pending_outcome_count},"
        f"cancelled_outcome:{result.cancelled_outcome_count},"
        f"feedback_mismatch:{result.feedback_mismatch_count},"
        f"false_risk_event:{result.false_risk_event_count},"
        f"risk_level_mismatch:{result.risk_level_mismatch_count},"
        f"direct_memory_persistence:{result.direct_memory_persistence_count}}}"
    )


def _render_totals(results: list[CaseResult], client: OpenAICompatibleHTTPClient) -> str:
    model_success_count = sum(1 for result in results if result.status == "model_success")
    fallback_count = sum(result.fallback_count for result in results)
    unknown_source_id_count = sum(result.unknown_source_id_count for result in results)
    cross_session_source_count = sum(result.cross_session_source_count for result in results)
    quote_validation_failure_count = sum(0 if result.quote_valid else 1 for result in results)
    unsupported_claim_count = sum(
        1
        for result in results
        for reason in result.reason_codes
        if reason.startswith("unsupported_")
    )
    assistant_only_action_item_count = sum(
        result.assistant_only_action_item_count for result in results
    )
    uncommitted_action_item_count = sum(
        result.uncommitted_action_item_count for result in results
    )
    pending_outcome_count = sum(result.pending_outcome_count for result in results)
    cancelled_outcome_count = sum(result.cancelled_outcome_count for result in results)
    feedback_mismatch_count = sum(result.feedback_mismatch_count for result in results)
    false_risk_event_count = sum(result.false_risk_event_count for result in results)
    risk_level_mismatch_count = sum(result.risk_level_mismatch_count for result in results)
    direct_memory_persistence_count = sum(
        result.direct_memory_persistence_count for result in results
    )
    return (
        "summary "
        f"case_count={len(results)} "
        f"real_http_request_count={client.request_count} "
        "strict_model_enabled=True "
        f"model_success_count={model_success_count} "
        f"fallback_count={fallback_count} "
        f"unknown_source_id_count={unknown_source_id_count} "
        f"cross_session_source_count={cross_session_source_count} "
        f"quote_validation_failure_count={quote_validation_failure_count} "
        f"unsupported_claim_count={unsupported_claim_count} "
        f"assistant_only_action_item_count={assistant_only_action_item_count} "
        f"uncommitted_action_item_count={uncommitted_action_item_count} "
        f"pending_outcome_count={pending_outcome_count} "
        f"cancelled_outcome_count={cancelled_outcome_count} "
        f"feedback_mismatch_count={feedback_mismatch_count} "
        f"false_risk_event_count={false_risk_event_count} "
        f"risk_level_mismatch_count={risk_level_mismatch_count} "
        f"direct_memory_persistence_count={direct_memory_persistence_count}"
    )


async def amain() -> int:
    settings = Settings()
    model_name = settings.structured_model_name
    capture = _ExecutionLogCapture()
    logger = logging.getLogger("agents.session_finalizer")
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(capture)
    try:
        client = OpenAICompatibleHTTPClient.from_settings(settings)
    except LLMError as exc:
        print(
            "case_id=configuration agent=session_finalizer "
            f"model_name={model_name} status=configuration_error "
            f"reason_codes={(type(exc).__name__,)} counts={{action_items:0,"
            "strategy_outcomes:0,risk_events:0,provisional_memories:0} "
            "source_id_valid=False quote_valid=False fallback_count=0"
        )
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
        return 1
    try:
        finalizer = SessionFinalizer(
            StructuredLLMClient(client, default_model_name=model_name),
            model_name=model_name,
            strict_model=True,
        )
        passed = True
        results: list[CaseResult] = []
        for case in _cases():
            result, case_passed = await _run_case(finalizer, case, model_name, capture)
            print(_render(result))
            results.append(result)
            passed = passed and case_passed
        print(_render_totals(results, client))
        return 0 if passed else 1
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
