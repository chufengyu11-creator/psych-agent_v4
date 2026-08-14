"""Deterministic checks for memory candidates and policy decisions."""

from dataclasses import dataclass

from evaluation.results import stable_reason_codes
from schemas.common import MemoryId, MessageId
from schemas.memory import MemoryCandidate, MemoryPolicyDecision
from services.claim_safety import unsupported_claim_reason


@dataclass(frozen=True, slots=True)
class ExpectedMemoryCandidate:
    candidate_type: str
    content_contains: tuple[str, ...]
    source_message_ids: tuple[str, ...]
    source_type: str
    sensitivity: str
    recommended_operation: str
    requires_user_confirmation: bool


@dataclass(frozen=True, slots=True)
class ExpectedMemoryDecision:
    allowed: bool
    operation: str | None
    requires_user_confirmation: bool
    reason_codes: tuple[str, ...] = ()
    target_memory_id: MemoryId | None = None
    check_target_memory_id: bool = False


def evaluate_memory_candidate(
    candidate: MemoryCandidate | None,
    expected: ExpectedMemoryCandidate,
    *,
    input_message_ids: set[MessageId],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if candidate is None:
        return ("memory_candidate_missing",)
    if candidate.candidate_type.value != expected.candidate_type:
        reasons.append("memory_type_mismatch")
    normalized_content = _normalize(candidate.content)
    if any(
        _normalize(fragment) not in normalized_content for fragment in expected.content_contains
    ):
        reasons.append("memory_content_mismatch")
    if tuple(map(str, candidate.source_message_ids)) != expected.source_message_ids:
        reasons.append("memory_source_ids_mismatch")
    if any(message_id not in input_message_ids for message_id in candidate.source_message_ids):
        reasons.append("unknown_memory_source_message")
    if candidate.source_type.value != expected.source_type:
        reasons.append("memory_source_type_mismatch")
    if candidate.sensitivity.value != expected.sensitivity:
        reasons.append("memory_sensitivity_mismatch")
    if candidate.recommended_operation.value != expected.recommended_operation:
        reasons.append("memory_operation_mismatch")
    if candidate.requires_user_confirmation != expected.requires_user_confirmation:
        reasons.append("memory_confirmation_mismatch")
    if unsupported_claim_reason(candidate.content) is not None:
        reasons.append("unsupported_durable_claim")
    return stable_reason_codes(reasons)


def evaluate_memory_policy(
    decision: MemoryPolicyDecision,
    expected: ExpectedMemoryDecision,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if decision.allowed != expected.allowed:
        reasons.append("memory_policy_allowed_mismatch")
    operation = decision.operation.value if decision.operation is not None else None
    if operation != expected.operation:
        reasons.append("memory_operation_mismatch")
    if decision.requires_user_confirmation != expected.requires_user_confirmation:
        reasons.append("memory_confirmation_mismatch")
    if any(code not in decision.reason_codes for code in expected.reason_codes):
        reasons.append("memory_policy_reason_missing")
    if expected.check_target_memory_id and decision.target_memory_id != expected.target_memory_id:
        reasons.append("memory_target_id_mismatch")
    return stable_reason_codes(reasons)


def _normalize(value: str) -> str:
    return " ".join(value.strip().casefold().split())


__all__ = [
    "ExpectedMemoryCandidate",
    "ExpectedMemoryDecision",
    "evaluate_memory_candidate",
    "evaluate_memory_policy",
]
