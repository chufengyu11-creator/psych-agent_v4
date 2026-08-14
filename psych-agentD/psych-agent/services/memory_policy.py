"""Policy gate for proposed long-term memory writes.

MemoryPolicy is deliberately deterministic. It decides whether one typed
MemoryCandidate can be written, needs confirmation, should reinforce an
existing memory, or should be rejected before durable storage.
"""

from schemas.common import MemoryId
from schemas.memory import (
    MemoryCandidate,
    MemoryConflict,
    MemoryOperation,
    MemoryPolicyDecision,
    MemoryPolicyInput,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from services.claim_safety import (
    HIDDEN_MOTIVE_INFERENCE,
    UNSUPPORTED_DIAGNOSIS,
    UNSUPPORTED_PERSONALITY_JUDGMENT,
    UNSUPPORTED_TREATMENT_OUTCOME,
    unsupported_claim_reason,
)
from services.conflict_resolver import ConflictResolver

MEMORY_DISABLED = "memory_disabled"
MISSING_SOURCE = "missing_source"
MODEL_INFERENCE_NOT_ALLOWED = "model_inference_not_allowed"
UNSUPPORTED_TARGETLESS_OPERATION = "unsupported_targetless_operation"
HIGH_SENSITIVITY_REQUIRES_CONFIRMATION = "high_sensitivity_requires_confirmation"
MEDIUM_SENSITIVITY_REQUIRES_CONFIRMATION = "medium_sensitivity_requires_confirmation"
LOW_CONFIDENCE_REQUIRES_CONFIRMATION = "low_confidence_requires_confirmation"
CANDIDATE_REQUIRES_CONFIRMATION = "candidate_requires_confirmation"
ACTIVE_GOAL_REQUIRES_CONFIRMATION = "active_goal_requires_confirmation"
UNFINISHED_TOPIC_REQUIRES_CONFIRMATION = "unfinished_topic_requires_confirmation"
SESSION_SUMMARY_REQUIRES_CONFIRMATION = "session_summary_requires_confirmation"
CONFLICT_REQUIRES_REVIEW = "conflict_requires_review"
ALLOWED_EXPLICIT_INTERACTION_PREFERENCE = "allowed_explicit_interaction_preference"

class MemoryPolicy:
    """Safety and consent policy for long-term memory candidates."""

    def __init__(self, conflict_resolver: ConflictResolver | None = None) -> None:
        """Create a policy with an optional deterministic conflict resolver."""

        self._conflict_resolver = conflict_resolver or ConflictResolver()

    def evaluate_candidate(self, payload: MemoryPolicyInput) -> MemoryPolicyDecision:
        """Return the write decision for one candidate memory."""

        candidate = payload.candidate
        if not payload.user_memory_enabled:
            return _reject(MEMORY_DISABLED)
        if not candidate.source_message_ids:
            return _reject(MISSING_SOURCE)

        unsupported_reason = unsupported_claim_reason(candidate.content)
        if unsupported_reason is not None:
            return _reject(unsupported_reason)
        if _is_disallowed_model_inference(candidate):
            return _reject(MODEL_INFERENCE_NOT_ALLOWED)
        if candidate.recommended_operation != MemoryOperation.CREATE:
            return _reject(UNSUPPORTED_TARGETLESS_OPERATION)

        conflict = _select_conflict(
            self._conflict_resolver.detect(candidate, payload.existing_memories)
        )
        if conflict is not None:
            return _decision_from_conflict(conflict)

        confirmation_reasons = _confirmation_reasons(candidate)
        if confirmation_reasons:
            return _confirm(candidate, confirmation_reasons)

        if _is_auto_allowed_explicit_preference(candidate):
            return _allow(
                candidate.recommended_operation,
                [ALLOWED_EXPLICIT_INTERACTION_PREFERENCE],
            )

        return _confirm(candidate, [CANDIDATE_REQUIRES_CONFIRMATION])


def _reject(reason_code: str) -> MemoryPolicyDecision:
    return MemoryPolicyDecision(
        allowed=False,
        operation=None,
        reason_codes=[reason_code],
        requires_user_confirmation=False,
    )


def _allow(
    operation: MemoryOperation,
    reason_codes: list[str],
    target_memory_id: MemoryId | None = None,
) -> MemoryPolicyDecision:
    return MemoryPolicyDecision(
        allowed=True,
        operation=operation,
        reason_codes=reason_codes,
        requires_user_confirmation=False,
        target_memory_id=target_memory_id,
    )


def _confirm(
    candidate: MemoryCandidate,
    reason_codes: list[str],
    target_memory_id: MemoryId | None = None,
) -> MemoryPolicyDecision:
    return MemoryPolicyDecision(
        allowed=True,
        operation=candidate.recommended_operation,
        reason_codes=_dedupe(reason_codes),
        requires_user_confirmation=True,
        target_memory_id=target_memory_id,
    )


def _select_conflict(conflicts: list[MemoryConflict]) -> MemoryConflict | None:
    """Select by stable operation priority instead of repository return order."""

    priorities = {
        MemoryOperation.SUPERSEDE: 3,
        MemoryOperation.MARK_CONFLICT: 2,
        MemoryOperation.REINFORCE: 1,
    }
    best: MemoryConflict | None = None
    best_priority = -1
    for conflict in conflicts:
        priority = priorities.get(conflict.recommended_operation, 0)
        if priority > best_priority:
            best = conflict
            best_priority = priority
    return best


def _decision_from_conflict(conflict: MemoryConflict) -> MemoryPolicyDecision:
    if conflict.recommended_operation == MemoryOperation.REINFORCE:
        return _allow(
            MemoryOperation.REINFORCE,
            [conflict.reason],
            target_memory_id=conflict.existing_memory_id,
        )
    if conflict.recommended_operation == MemoryOperation.SUPERSEDE:
        return MemoryPolicyDecision(
            allowed=True,
            operation=MemoryOperation.SUPERSEDE,
            reason_codes=_dedupe([CONFLICT_REQUIRES_REVIEW, conflict.reason]),
            requires_user_confirmation=True,
            target_memory_id=conflict.existing_memory_id,
        )
    return MemoryPolicyDecision(
        allowed=False,
        operation=MemoryOperation.MARK_CONFLICT,
        reason_codes=_dedupe([CONFLICT_REQUIRES_REVIEW, conflict.reason]),
        requires_user_confirmation=False,
        target_memory_id=conflict.existing_memory_id,
    )


def _is_disallowed_model_inference(candidate: MemoryCandidate) -> bool:
    durable_types = {
        MemoryType.INTERACTION_PREFERENCE,
        MemoryType.ACTIVE_GOAL,
        MemoryType.UNFINISHED_TOPIC,
        MemoryType.SEMANTIC,
        MemoryType.EPISODIC,
    }
    return (
        candidate.source_type == MemorySourceType.MODEL_INFERENCE
        and candidate.candidate_type in durable_types
    )


def _confirmation_reasons(candidate: MemoryCandidate) -> list[str]:
    reasons: list[str] = []
    if candidate.sensitivity == MemorySensitivity.HIGH:
        reasons.append(HIGH_SENSITIVITY_REQUIRES_CONFIRMATION)
    elif candidate.sensitivity == MemorySensitivity.MEDIUM:
        reasons.append(MEDIUM_SENSITIVITY_REQUIRES_CONFIRMATION)

    if candidate.confidence < 0.8:
        reasons.append(LOW_CONFIDENCE_REQUIRES_CONFIRMATION)
    if candidate.candidate_type == MemoryType.ACTIVE_GOAL:
        reasons.append(ACTIVE_GOAL_REQUIRES_CONFIRMATION)
    if candidate.candidate_type == MemoryType.UNFINISHED_TOPIC:
        reasons.append(UNFINISHED_TOPIC_REQUIRES_CONFIRMATION)
    if candidate.source_type == MemorySourceType.SESSION_SUMMARY:
        reasons.append(SESSION_SUMMARY_REQUIRES_CONFIRMATION)
    if candidate.requires_user_confirmation:
        reasons.append(CANDIDATE_REQUIRES_CONFIRMATION)
    return _dedupe(reasons)


def _is_auto_allowed_explicit_preference(candidate: MemoryCandidate) -> bool:
    return (
        candidate.candidate_type == MemoryType.INTERACTION_PREFERENCE
        and candidate.source_type == MemorySourceType.EXPLICIT_USER_STATEMENT
        and candidate.sensitivity == MemorySensitivity.LOW
        and candidate.confidence >= 0.8
        and candidate.recommended_operation == MemoryOperation.CREATE
    )


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


__all__ = [
    "ACTIVE_GOAL_REQUIRES_CONFIRMATION",
    "ALLOWED_EXPLICIT_INTERACTION_PREFERENCE",
    "CANDIDATE_REQUIRES_CONFIRMATION",
    "CONFLICT_REQUIRES_REVIEW",
    "HIGH_SENSITIVITY_REQUIRES_CONFIRMATION",
    "LOW_CONFIDENCE_REQUIRES_CONFIRMATION",
    "MEDIUM_SENSITIVITY_REQUIRES_CONFIRMATION",
    "MEMORY_DISABLED",
    "MISSING_SOURCE",
    "MODEL_INFERENCE_NOT_ALLOWED",
    "SESSION_SUMMARY_REQUIRES_CONFIRMATION",
    "UNSUPPORTED_DIAGNOSIS",
    "UNSUPPORTED_PERSONALITY_JUDGMENT",
    "HIDDEN_MOTIVE_INFERENCE",
    "UNSUPPORTED_TARGETLESS_OPERATION",
    "UNSUPPORTED_TREATMENT_OUTCOME",
    "UNFINISHED_TOPIC_REQUIRES_CONFIRMATION",
    "MemoryPolicy",
]
