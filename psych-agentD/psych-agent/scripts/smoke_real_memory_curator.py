"""Run strict real-model smoke cases for MemoryCurator with sanitized output."""

# ruff: noqa: E402 -- direct script execution needs the repository root on sys.path.

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agents.memory_curator import (
    MemoryCurator,
    MemoryCuratorInput,
    _CandidateValidationResult,
)
from app.config import Settings
from llm.exceptions import LLMError
from llm.local_client import OpenAICompatibleHTTPClient
from llm.structured_client import StructuredLLMClient
from schemas.common import InterventionId, MessageId, SessionId
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
from schemas.summary import SessionFinalizerResult, StrategyOutcome
from services.claim_safety import unsupported_claim_reason
from services.model_execution import ModelExecutionFailure


@dataclass(frozen=True)
class SmokeCase:
    case_id: str
    payload: MemoryCuratorInput
    expected_min_accepted: int = 0
    expected_type: MemoryType | None = None
    expected_exact_accepted: int | None = None


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    status: str
    raw_candidate_count: int
    accepted_count: int
    rejected_count: int
    fallback_count: int
    unknown_source_id_count: int
    quote_failure_count: int
    accepted_without_evidence_count: int
    assistant_only_accepted_count: int
    unsupported_claim_accepted_count: int
    non_create_accepted_count: int
    repeated_observation_accepted_count: int
    model_inference_accepted_count: int
    strategy_outcome_mismatch_accepted_count: int
    confirmation_violation_count: int
    direct_memory_persistence_count: int
    passed: bool


class _ExecutionLogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() == "model_execution":
            self.records.append(record)


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


def _candidate(
    *,
    candidate_type: MemoryType,
    content: str,
    source_message_id: str,
    source_type: MemorySourceType = MemorySourceType.EXPLICIT_USER_STATEMENT,
    confidence: float = 0.9,
    confirmation: bool = False,
    sensitivity: MemorySensitivity = MemorySensitivity.LOW,
) -> MemoryCandidate:
    return MemoryCandidate(
        candidate_type=candidate_type,
        content=content,
        source_message_ids=[MessageId(source_message_id)],
        source_type=source_type,
        confidence=confidence,
        requires_user_confirmation=confirmation,
        sensitivity=sensitivity,
        recommended_operation=MemoryOperation.CREATE,
    )


def _base_payload(
    session_id: SessionId,
    messages: list[Message],
    *,
    candidates: list[MemoryCandidate] | None = None,
    outcomes: list[StrategyOutcome] | None = None,
    interventions: list[InterventionRecord] | None = None,
) -> MemoryCuratorInput:
    return MemoryCuratorInput(
        session_id=session_id,
        messages=messages,
        final_state=SessionState(session_id=session_id),
        finalizer_result=SessionFinalizerResult(
            session_id=session_id,
            session_summary="Artificial session close fixture.",
            candidate_memories=candidates or [],
            strategy_outcomes=outcomes or [],
        ),
        interventions=interventions,
    )


def _cases() -> list[SmokeCase]:
    cases: list[SmokeCase] = []

    sid = SessionId("curator_live_a")
    cases.append(
        SmokeCase(
            case_id="interaction_preference",
            payload=_base_payload(
                sid,
                [
                    _message(
                        sid,
                        "a_user_001",
                        MessageRole.USER,
                        "Please remember that I prefer one practical step at a time.",
                        1,
                    )
                ],
                candidates=[
                    _candidate(
                        candidate_type=MemoryType.INTERACTION_PREFERENCE,
                        content="User preference: one practical step at a time.",
                        source_message_id="a_user_001",
                    )
                ],
            ),
            expected_min_accepted=1,
            expected_type=MemoryType.INTERACTION_PREFERENCE,
        )
    )

    sid = SessionId("curator_live_b")
    cases.append(
        SmokeCase(
            case_id="active_goal",
            payload=_base_payload(
                sid,
                [
                    _message(
                        sid,
                        "b_user_001",
                        MessageRole.USER,
                        "I will draft one message to my manager this week.",
                        1,
                    )
                ],
                candidates=[
                    _candidate(
                        candidate_type=MemoryType.ACTIVE_GOAL,
                        content="User active goal: draft one manager message this week.",
                        source_message_id="b_user_001",
                        confirmation=True,
                    )
                ],
            ),
            expected_min_accepted=1,
            expected_type=MemoryType.ACTIVE_GOAL,
        )
    )

    sid = SessionId("curator_live_c")
    cases.append(
        SmokeCase(
            case_id="unfinished_topic",
            payload=_base_payload(
                sid,
                [
                    _message(
                        sid,
                        "c_user_001",
                        MessageRole.USER,
                        "Next time I want to continue the work pressure topic.",
                        1,
                    )
                ],
                candidates=[
                    _candidate(
                        candidate_type=MemoryType.UNFINISHED_TOPIC,
                        content="Unfinished topic: continue work pressure next time.",
                        source_message_id="c_user_001",
                        confirmation=True,
                    )
                ],
            ),
            expected_min_accepted=1,
            expected_type=MemoryType.UNFINISHED_TOPIC,
        )
    )

    sid = SessionId("curator_live_d")
    messages = [
        _message(sid, "d_user_001", MessageRole.USER, "I want a single step.", 1),
        _message(sid, "d_assistant_001", MessageRole.ASSISTANT, "Try one sentence.", 2),
        _message(sid, "d_user_002", MessageRole.USER, "That helped a little, mixed fit.", 3),
    ]
    cases.append(
        SmokeCase(
            case_id="strategy_outcome",
            payload=_base_payload(
                sid,
                messages,
                candidates=[
                    _candidate(
                        candidate_type=MemoryType.STRATEGY_OUTCOME,
                        content="Strategy outcome: small_step_support had mixed fit.",
                        source_message_id="d_user_002",
                        source_type=MemorySourceType.SESSION_SUMMARY,
                        confirmation=True,
                    )
                ],
                outcomes=[
                    StrategyOutcome(
                        strategy="small_step_support",
                        outcome="mixed fit",
                        source_message_ids=[MessageId("d_user_002")],
                    )
                ],
                interventions=[
                    InterventionRecord(
                        intervention_id=InterventionId("d_int_001"),
                        session_id=sid,
                        assistant_message_id=MessageId("d_assistant_001"),
                        strategy="small_step_support",
                        objective="offer one step",
                        status=InterventionStatus.EVALUATED,
                        observed_response="mixed fit",
                        explicit_feedback="mixed",
                        strategy_fit="mixed",
                        objective_progress="partial",
                    )
                ],
            ),
            expected_min_accepted=1,
            expected_type=MemoryType.STRATEGY_OUTCOME,
        )
    )

    sid = SessionId("curator_live_e")
    cases.append(
        SmokeCase(
            case_id="partial_acceptance",
            payload=_base_payload(
                sid,
                [
                    _message(
                        sid,
                        "e_user_001",
                        MessageRole.USER,
                        "I prefer one step at a time.",
                        1,
                    ),
                    _message(
                        sid,
                        "e_user_002",
                        MessageRole.USER,
                        "I will write one manager note.",
                        2,
                    ),
                    _message(
                        sid,
                        "e_assistant_001",
                        MessageRole.ASSISTANT,
                        "You have anxiety disorder.",
                        3,
                    ),
                ],
                candidates=[
                    _candidate(
                        candidate_type=MemoryType.INTERACTION_PREFERENCE,
                        content="User preference: one step at a time.",
                        source_message_id="e_user_001",
                    ),
                    _candidate(
                        candidate_type=MemoryType.ACTIVE_GOAL,
                        content="User active goal: write one manager note.",
                        source_message_id="e_user_002",
                        confirmation=True,
                    ),
                ],
            ),
            expected_min_accepted=1,
        )
    )

    sid = SessionId("curator_live_f")
    cases.append(
        SmokeCase(
            case_id="many_meetings_boundary",
            payload=_base_payload(
                sid,
                [
                    _message(
                        sid,
                        "f_user_001",
                        MessageRole.USER,
                        "I have too many meetings this week.",
                        1,
                    )
                ],
            ),
            expected_exact_accepted=0,
        )
    )

    sid = SessionId("curator_live_g")
    cases.append(
        SmokeCase(
            case_id="single_session_repeated_observation",
            payload=_base_payload(
                sid,
                [
                    _message(
                        sid,
                        "g_user_001",
                        MessageRole.USER,
                        "I prefer one step at a time.",
                        1,
                    ),
                    _message(
                        sid,
                        "g_user_002",
                        MessageRole.USER,
                        "Again, one step at a time is best.",
                        2,
                    ),
                ],
            ),
        )
    )

    sid = SessionId("curator_live_h")
    cases.append(
        SmokeCase(
            case_id="model_inference_personality",
            payload=_base_payload(
                sid,
                [_message(sid, "h_user_001", MessageRole.USER, "I keep postponing one email.", 1)],
            ),
            expected_exact_accepted=0,
        )
    )

    sid = SessionId("curator_live_i")
    cases.append(
        SmokeCase(
            case_id="no_long_term_memory",
            payload=_base_payload(
                sid,
                [_message(sid, "i_user_001", MessageRole.USER, "It is chilly outside today.", 1)],
            ),
            expected_exact_accepted=0,
        )
    )

    sid = SessionId("curator_live_j")
    cases.append(
        SmokeCase(
            case_id="high_sensitivity_confirmation",
            payload=_base_payload(
                sid,
                [
                    _message(
                        sid,
                        "j_user_001",
                        MessageRole.USER,
                        "I may want to discuss trauma history later.",
                        1,
                    )
                ],
            ),
        )
    )
    return cases


async def _run_case(
    finalizer: MemoryCurator,
    case: SmokeCase,
    capture: _ExecutionLogCapture,
) -> CaseResult:
    start_index = len(capture.records)
    try:
        audit = await finalizer._curate_with_audit(case.payload)
    except ModelExecutionFailure as exc:
        fallback_count = _fallback_count(capture.records[start_index:])
        return CaseResult(
            case_id=case.case_id,
            status=exc.status.value,
            raw_candidate_count=0,
            accepted_count=0,
            rejected_count=0,
            fallback_count=fallback_count,
            unknown_source_id_count=0,
            quote_failure_count=0,
            accepted_without_evidence_count=0,
            assistant_only_accepted_count=0,
            unsupported_claim_accepted_count=0,
            non_create_accepted_count=0,
            repeated_observation_accepted_count=0,
            model_inference_accepted_count=0,
            strategy_outcome_mismatch_accepted_count=0,
            confirmation_violation_count=0,
            direct_memory_persistence_count=0,
            passed=False,
        )
    records = capture.records[start_index:]
    status = str(records[-1].__dict__.get("status", "missing")) if records else "missing"
    accepted = audit.candidates
    reason_counts = _reason_counts(audit.validation_results)
    accepted_without_evidence = sum(1 for candidate in accepted if not candidate.source_message_ids)
    assistant_only_accepted = _assistant_only_accepted_count(accepted, case.payload)
    unsupported_accepted = sum(
        1
        for candidate in accepted
        if unsupported_claim_reason(candidate.content) is not None
    )
    non_create_accepted = sum(
        1 for candidate in accepted if candidate.recommended_operation != MemoryOperation.CREATE
    )
    repeated_accepted = sum(
        1
        for candidate in accepted
        if candidate.source_type == MemorySourceType.REPEATED_OBSERVATION
    )
    inference_accepted = sum(
        1 for candidate in accepted if candidate.source_type == MemorySourceType.MODEL_INFERENCE
    )
    confirmation_violations = sum(
        1
        for candidate in accepted
        if (
            candidate.sensitivity in {MemorySensitivity.MEDIUM, MemorySensitivity.HIGH}
            or candidate.candidate_type in {MemoryType.ACTIVE_GOAL, MemoryType.UNFINISHED_TOPIC}
            or candidate.confidence < 0.8
        )
        and not candidate.requires_user_confirmation
    )
    type_ok = case.expected_type is None or any(
        candidate.candidate_type == case.expected_type for candidate in accepted
    )
    min_ok = len(accepted) >= case.expected_min_accepted
    exact_ok = case.expected_exact_accepted is None or len(accepted) == case.expected_exact_accepted
    fallback_count = _fallback_count(records)
    passed = (
        status == "model_success"
        and fallback_count == 0
        and type_ok
        and min_ok
        and exact_ok
        and accepted_without_evidence == 0
        and assistant_only_accepted == 0
        and unsupported_accepted == 0
        and non_create_accepted == 0
        and repeated_accepted == 0
        and inference_accepted == 0
        and confirmation_violations == 0
    )
    return CaseResult(
        case_id=case.case_id,
        status=status,
        raw_candidate_count=len(audit.validation_results),
        accepted_count=len(accepted),
        rejected_count=sum(1 for result in audit.validation_results if not result.accepted),
        fallback_count=fallback_count,
        unknown_source_id_count=reason_counts.get("unknown_source_message_id", 0),
        quote_failure_count=reason_counts.get("evidence_quote_not_found", 0),
        accepted_without_evidence_count=accepted_without_evidence,
        assistant_only_accepted_count=assistant_only_accepted,
        unsupported_claim_accepted_count=unsupported_accepted,
        non_create_accepted_count=non_create_accepted,
        repeated_observation_accepted_count=repeated_accepted,
        model_inference_accepted_count=inference_accepted,
        strategy_outcome_mismatch_accepted_count=reason_counts.get(
            "strategy_outcome_without_evaluated_intervention",
            0,
        ),
        confirmation_violation_count=confirmation_violations,
        direct_memory_persistence_count=0,
        passed=passed,
    )


def _fallback_count(records: list[logging.LogRecord]) -> int:
    return sum(
        1
        for record in records
        if str(record.__dict__.get("status", "")).startswith("fallback_")
    )


def _reason_counts(results: tuple[_CandidateValidationResult, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        for reason in result.reason_codes:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def _assistant_only_accepted_count(
    candidates: list[MemoryCandidate],
    payload: MemoryCuratorInput,
) -> int:
    user_ids = {message.id for message in payload.messages if message.role == MessageRole.USER}
    return sum(
        1
        for candidate in candidates
        if candidate.source_type == MemorySourceType.EXPLICIT_USER_STATEMENT
        and not (set(candidate.source_message_ids) & user_ids)
    )


def _render_case(result: CaseResult) -> str:
    return (
        f"case_id={result.case_id} status={result.status} "
        f"raw_candidate_count={result.raw_candidate_count} "
        f"accepted_count={result.accepted_count} rejected_count={result.rejected_count} "
        f"fallback_count={result.fallback_count} "
        f"unknown_source_id_count={result.unknown_source_id_count} "
        f"quote_failure_count={result.quote_failure_count} "
        f"assistant_only_accepted_count={result.assistant_only_accepted_count} "
        f"unsupported_claim_accepted_count={result.unsupported_claim_accepted_count} "
        f"non_create_accepted_count={result.non_create_accepted_count} "
        f"repeated_observation_accepted_count={result.repeated_observation_accepted_count} "
        f"model_inference_accepted_count={result.model_inference_accepted_count} "
        f"confirmation_violation_count={result.confirmation_violation_count} "
        f"direct_memory_persistence_count={result.direct_memory_persistence_count} "
        f"passed={result.passed}"
    )


def _render_summary(results: list[CaseResult], client: OpenAICompatibleHTTPClient) -> str:
    model_success_count = sum(1 for result in results if result.status == "model_success")
    model_level_failure_count = len(results) - model_success_count
    fallback_count = sum(result.fallback_count for result in results)
    raw_candidate_count = sum(result.raw_candidate_count for result in results)
    accepted_count = sum(result.accepted_count for result in results)
    rejected_count = sum(result.rejected_count for result in results)
    unknown_source_id_count = sum(result.unknown_source_id_count for result in results)
    quote_failure_count = sum(result.quote_failure_count for result in results)
    accepted_without_evidence_count = sum(
        result.accepted_without_evidence_count for result in results
    )
    assistant_only_count = sum(result.assistant_only_accepted_count for result in results)
    unsupported_count = sum(result.unsupported_claim_accepted_count for result in results)
    non_create_count = sum(result.non_create_accepted_count for result in results)
    repeated_count = sum(result.repeated_observation_accepted_count for result in results)
    inference_count = sum(result.model_inference_accepted_count for result in results)
    strategy_mismatch_count = sum(
        result.strategy_outcome_mismatch_accepted_count for result in results
    )
    confirmation_count = sum(result.confirmation_violation_count for result in results)
    persistence_count = sum(result.direct_memory_persistence_count for result in results)
    partial_success = next(
        result.status == "model_success"
        for result in results
        if result.case_id == "partial_acceptance"
    )
    partial_fallback_count = next(
        result.fallback_count for result in results if result.case_id == "partial_acceptance"
    )
    return (
        "summary "
        f"case_count={len(results)} "
        f"real_http_request_count={client.request_count} "
        "strict_model_enabled=True "
        f"model_success_count={model_success_count} "
        f"model_level_failure_count={model_level_failure_count} "
        f"fallback_count={fallback_count} "
        f"raw_candidate_count={raw_candidate_count} "
        f"accepted_count={accepted_count} "
        f"rejected_count={rejected_count} "
        f"unknown_source_id_count={unknown_source_id_count} "
        f"quote_validation_failure_count={quote_failure_count} "
        f"accepted_without_evidence_count={accepted_without_evidence_count} "
        f"assistant_only_explicit_memory_count={assistant_only_count} "
        f"unsupported_claim_accepted_count={unsupported_count} "
        f"non_create_accepted_count={non_create_count} "
        f"repeated_observation_accepted_count={repeated_count} "
        f"model_inference_accepted_count={inference_count} "
        f"strategy_outcome_mismatch_accepted_count={strategy_mismatch_count} "
        f"confirmation_rule_violation_count={confirmation_count} "
        f"direct_memory_persistence_count={persistence_count} "
        f"partial_acceptance_case_model_success={partial_success} "
        f"partial_acceptance_case_fallback_count={partial_fallback_count}"
    )


async def amain() -> int:
    settings = Settings()
    model_name = settings.structured_model_name
    capture = _ExecutionLogCapture()
    logger = logging.getLogger("agents.memory_curator")
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(capture)
    try:
        client = OpenAICompatibleHTTPClient.from_settings(settings)
    except LLMError as exc:
        print(
            "case_id=configuration status=configuration_error "
            f"model_name={model_name} reason_codes={(type(exc).__name__,)} "
            "raw_candidate_count=0 accepted_count=0 rejected_count=0 fallback_count=0"
        )
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
        return 1
    try:
        curator = MemoryCurator(
            StructuredLLMClient(client, default_model_name=model_name),
            model_name=model_name,
            strict_model=True,
        )
        results: list[CaseResult] = []
        passed = True
        for case in _cases():
            result = await _run_case(curator, case, capture)
            print(_render_case(result))
            results.append(result)
            passed = passed and result.passed
        print(_render_summary(results, client))
        return 0 if passed else 1
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
