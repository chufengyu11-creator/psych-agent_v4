"""Complete deterministic boundary matrix for MemoryPolicy."""

import pytest

from schemas.common import MemoryId, MessageId, SourceReference
from schemas.memory import (
    LongTermMemory,
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
)
from services.conflict_resolver import (
    CONFLICTS_WITH_EXISTING_MEMORY,
    DUPLICATE_MEMORY,
    SUPERSEDES_EXISTING_MEMORY,
    ConflictResolver,
)
from services.memory_policy import (
    ACTIVE_GOAL_REQUIRES_CONFIRMATION,
    ALLOWED_EXPLICIT_INTERACTION_PREFERENCE,
    CANDIDATE_REQUIRES_CONFIRMATION,
    CONFLICT_REQUIRES_REVIEW,
    HIGH_SENSITIVITY_REQUIRES_CONFIRMATION,
    LOW_CONFIDENCE_REQUIRES_CONFIRMATION,
    MEDIUM_SENSITIVITY_REQUIRES_CONFIRMATION,
    MEMORY_DISABLED,
    MISSING_SOURCE,
    MODEL_INFERENCE_NOT_ALLOWED,
    SESSION_SUMMARY_REQUIRES_CONFIRMATION,
    UNFINISHED_TOPIC_REQUIRES_CONFIRMATION,
    UNSUPPORTED_TARGETLESS_OPERATION,
    MemoryPolicy,
)


def _candidate(
    content: str = "User prefers one small step at a time.",
    *,
    source_message_ids: list[MessageId] | None = None,
    source_id: str = "msg_memory_policy_1",
    candidate_type: MemoryType = MemoryType.INTERACTION_PREFERENCE,
    confidence: float = 0.9,
    operation: MemoryOperation = MemoryOperation.CREATE,
    requires_confirmation: bool = False,
    sensitivity: MemorySensitivity = MemorySensitivity.LOW,
    source_type: MemorySourceType = MemorySourceType.EXPLICIT_USER_STATEMENT,
) -> MemoryCandidate:
    """Build a valid explicit low-sensitivity preference by default."""

    sources = [MessageId(source_id)] if source_message_ids is None else source_message_ids
    return MemoryCandidate(
        candidate_type=candidate_type,
        content=content,
        source_message_ids=sources,
        source_type=source_type,
        confidence=confidence,
        requires_user_confirmation=requires_confirmation,
        sensitivity=sensitivity,
        recommended_operation=operation,
    )


def _memory(
    content: str,
    *,
    memory_id: str = "mem_existing_1",
    memory_type: MemoryType = MemoryType.INTERACTION_PREFERENCE,
    sensitivity: MemorySensitivity = MemorySensitivity.LOW,
    confidence: float = 1.0,
) -> LongTermMemory:
    return LongTermMemory(
        id=MemoryId(memory_id),
        memory_type=memory_type,
        content=content,
        sensitivity=sensitivity,
        confidence=confidence,
        source=[SourceReference(message_id=MessageId("msg_existing_1"))],
    )


def _evaluate(
    candidate: MemoryCandidate,
    *,
    enabled: bool = True,
    existing: list[LongTermMemory] | None = None,
    policy: MemoryPolicy | None = None,
) -> MemoryPolicyDecision:
    return (policy or MemoryPolicy()).evaluate_candidate(
        MemoryPolicyInput(
            candidate=candidate,
            existing_memories=existing or [],
            user_memory_enabled=enabled,
        )
    )


def _assert_rejected(decision: MemoryPolicyDecision, reason: str) -> None:
    assert decision.allowed is False
    assert decision.operation is None
    assert decision.requires_user_confirmation is False
    assert decision.target_memory_id is None
    assert decision.reason_codes == [reason]


def test_clear_explicit_low_sensitivity_preference_is_auto_allowed() -> None:
    decision = _evaluate(_candidate())
    assert decision.allowed is True
    assert decision.operation == MemoryOperation.CREATE
    assert decision.requires_user_confirmation is False
    assert decision.target_memory_id is None
    assert decision.reason_codes == [ALLOWED_EXPLICIT_INTERACTION_PREFERENCE]


def test_missing_source_is_rejected_fail_closed() -> None:
    _assert_rejected(
        _evaluate(_candidate(source_message_ids=[])),
        MISSING_SOURCE,
    )


def test_memory_disabled_has_highest_priority() -> None:
    decision = _evaluate(
        _candidate("The user has an anxiety disorder.", source_message_ids=[]),
        enabled=False,
    )
    _assert_rejected(decision, MEMORY_DISABLED)


def test_missing_source_precedes_unsupported_claim_detection() -> None:
    decision = _evaluate(_candidate("The user has an anxiety disorder.", source_message_ids=[]))
    _assert_rejected(decision, MISSING_SOURCE)


@pytest.mark.parametrize(
    "operation",
    [
        MemoryOperation.REINFORCE,
        MemoryOperation.SUPERSEDE,
        MemoryOperation.MARK_CONFLICT,
        MemoryOperation.EXPIRE,
        MemoryOperation.DELETE,
    ],
)
def test_targetless_non_create_operations_are_rejected(
    operation: MemoryOperation,
) -> None:
    _assert_rejected(
        _evaluate(_candidate(operation=operation)),
        UNSUPPORTED_TARGETLESS_OPERATION,
    )


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("The user has an anxiety disorder.", UNSUPPORTED_DIAGNOSIS),
        ("The user has a personality disorder.", UNSUPPORTED_PERSONALITY_JUDGMENT),
        ("The user has a hidden motive.", HIDDEN_MOTIVE_INFERENCE),
        ("The user has fully recovered.", UNSUPPORTED_TREATMENT_OUTCOME),
    ],
)
def test_policy_rejects_each_unsupported_claim(
    content: str,
    reason: str,
) -> None:
    _assert_rejected(_evaluate(_candidate(content)), reason)


@pytest.mark.parametrize(
    ("sensitivity", "reason"),
    [
        (MemorySensitivity.HIGH, HIGH_SENSITIVITY_REQUIRES_CONFIRMATION),
        (MemorySensitivity.MEDIUM, MEDIUM_SENSITIVITY_REQUIRES_CONFIRMATION),
    ],
)
def test_sensitive_candidates_require_confirmation(
    sensitivity: MemorySensitivity,
    reason: str,
) -> None:
    decision = _evaluate(_candidate(sensitivity=sensitivity))
    assert decision.allowed is True
    assert decision.operation == MemoryOperation.CREATE
    assert decision.requires_user_confirmation is True
    assert decision.target_memory_id is None
    assert decision.reason_codes == [reason]


@pytest.mark.parametrize(
    ("confidence", "requires_confirmation", "reason"),
    [
        (0.7999, True, LOW_CONFIDENCE_REQUIRES_CONFIRMATION),
        (0.8, False, ALLOWED_EXPLICIT_INTERACTION_PREFERENCE),
        (1.0, False, ALLOWED_EXPLICIT_INTERACTION_PREFERENCE),
    ],
)
def test_confidence_boundary_is_exactly_point_eight(
    confidence: float,
    requires_confirmation: bool,
    reason: str,
) -> None:
    decision = _evaluate(_candidate(confidence=confidence))
    assert decision.allowed is True
    assert decision.operation == MemoryOperation.CREATE
    assert decision.requires_user_confirmation is requires_confirmation
    assert decision.target_memory_id is None
    assert decision.reason_codes == [reason]


def test_candidate_requested_confirmation_is_preserved() -> None:
    decision = _evaluate(_candidate(requires_confirmation=True))
    assert decision.allowed is True
    assert decision.operation == MemoryOperation.CREATE
    assert decision.requires_user_confirmation is True
    assert decision.target_memory_id is None
    assert decision.reason_codes == [CANDIDATE_REQUIRES_CONFIRMATION]


@pytest.mark.parametrize(
    ("memory_type", "reason"),
    [
        (MemoryType.ACTIVE_GOAL, ACTIVE_GOAL_REQUIRES_CONFIRMATION),
        (MemoryType.UNFINISHED_TOPIC, UNFINISHED_TOPIC_REQUIRES_CONFIRMATION),
        (MemoryType.SEMANTIC, CANDIDATE_REQUIRES_CONFIRMATION),
        (MemoryType.EPISODIC, CANDIDATE_REQUIRES_CONFIRMATION),
        (MemoryType.STRATEGY_OUTCOME, CANDIDATE_REQUIRES_CONFIRMATION),
    ],
)
def test_memory_types_follow_explicit_confirmation_rules(
    memory_type: MemoryType,
    reason: str,
) -> None:
    decision = _evaluate(_candidate(candidate_type=memory_type))
    assert decision.allowed is True
    assert decision.operation == MemoryOperation.CREATE
    assert decision.requires_user_confirmation is True
    assert decision.target_memory_id is None
    assert decision.reason_codes == [reason]


def test_session_summary_source_requires_confirmation() -> None:
    decision = _evaluate(_candidate(source_type=MemorySourceType.SESSION_SUMMARY))
    assert decision.allowed is True
    assert decision.operation == MemoryOperation.CREATE
    assert decision.requires_user_confirmation is True
    assert decision.target_memory_id is None
    assert decision.reason_codes == [SESSION_SUMMARY_REQUIRES_CONFIRMATION]


def test_combined_confirmation_reasons_are_stable_and_deduplicated() -> None:
    decision = _evaluate(
        _candidate(
            candidate_type=MemoryType.ACTIVE_GOAL,
            sensitivity=MemorySensitivity.HIGH,
            confidence=0.79,
            source_type=MemorySourceType.SESSION_SUMMARY,
            requires_confirmation=True,
        )
    )
    assert decision.allowed is True
    assert decision.operation == MemoryOperation.CREATE
    assert decision.requires_user_confirmation is True
    assert decision.target_memory_id is None
    assert decision.reason_codes == [
        HIGH_SENSITIVITY_REQUIRES_CONFIRMATION,
        LOW_CONFIDENCE_REQUIRES_CONFIRMATION,
        ACTIVE_GOAL_REQUIRES_CONFIRMATION,
        SESSION_SUMMARY_REQUIRES_CONFIRMATION,
        CANDIDATE_REQUIRES_CONFIRMATION,
    ]
    assert len(decision.reason_codes) == len(set(decision.reason_codes))


@pytest.mark.parametrize(
    "memory_type",
    [
        MemoryType.INTERACTION_PREFERENCE,
        MemoryType.ACTIVE_GOAL,
        MemoryType.UNFINISHED_TOPIC,
        MemoryType.SEMANTIC,
        MemoryType.EPISODIC,
    ],
)
def test_model_inference_is_rejected_for_every_durable_type(
    memory_type: MemoryType,
) -> None:
    _assert_rejected(
        _evaluate(
            _candidate(
                candidate_type=memory_type,
                source_type=MemorySourceType.MODEL_INFERENCE,
            )
        ),
        MODEL_INFERENCE_NOT_ALLOWED,
    )


def test_model_inferred_strategy_outcome_uses_conservative_confirmation() -> None:
    decision = _evaluate(
        _candidate(
            candidate_type=MemoryType.STRATEGY_OUTCOME,
            source_type=MemorySourceType.MODEL_INFERENCE,
        )
    )
    assert decision.allowed is True
    assert decision.operation == MemoryOperation.CREATE
    assert decision.requires_user_confirmation is True
    assert decision.target_memory_id is None
    assert decision.reason_codes == [CANDIDATE_REQUIRES_CONFIRMATION]


def test_duplicate_memory_reinforces_without_confirmation() -> None:
    existing = _memory("User prefers one small step at a time.")
    decision = _evaluate(_candidate(), existing=[existing])
    assert decision.allowed is True
    assert decision.operation == MemoryOperation.REINFORCE
    assert decision.target_memory_id == existing.id
    assert decision.requires_user_confirmation is False
    assert decision.reason_codes == [DUPLICATE_MEMORY]


def test_explicit_update_supersedes_with_review() -> None:
    existing = _memory("User prefers one small step at a time.")
    decision = _evaluate(
        _candidate("Now the user wants a complete plan all at once."),
        existing=[existing],
    )
    assert decision.allowed is True
    assert decision.operation == MemoryOperation.SUPERSEDE
    assert decision.target_memory_id == existing.id
    assert decision.requires_user_confirmation is True
    assert decision.reason_codes == [
        CONFLICT_REQUIRES_REVIEW,
        SUPERSEDES_EXISTING_MEMORY,
    ]


def test_opposing_preference_marks_conflict() -> None:
    existing = _memory("User prefers one small step at a time.")
    decision = _evaluate(
        _candidate("User wants a complete plan with all steps at once."),
        existing=[existing],
    )
    assert decision.allowed is False
    assert decision.operation == MemoryOperation.MARK_CONFLICT
    assert decision.target_memory_id == existing.id
    assert decision.requires_user_confirmation is False
    assert decision.reason_codes == [
        CONFLICT_REQUIRES_REVIEW,
        CONFLICTS_WITH_EXISTING_MEMORY,
    ]


def test_unrelated_existing_memory_does_not_create_false_conflict() -> None:
    existing = _memory(
        "User wants to improve workplace communication.",
        memory_type=MemoryType.ACTIVE_GOAL,
    )
    decision = _evaluate(_candidate(), existing=[existing])
    assert decision.allowed is True
    assert decision.operation == MemoryOperation.CREATE
    assert decision.target_memory_id is None
    assert decision.requires_user_confirmation is False
    assert decision.reason_codes == [ALLOWED_EXPLICIT_INTERACTION_PREFERENCE]


class StubConflictResolver(ConflictResolver):
    """Return typed conflicts in a caller-controlled order."""

    def __init__(self, conflicts: list[MemoryConflict]) -> None:
        self._conflicts = conflicts

    def detect(
        self,
        candidate: MemoryCandidate,
        existing_memories: list[LongTermMemory],
    ) -> list[MemoryConflict]:
        _ = (candidate, existing_memories)
        return self._conflicts


@pytest.mark.parametrize("reverse", [False, True])
def test_conflict_selection_prefers_supersede_regardless_of_input_order(
    reverse: bool,
) -> None:
    candidate = _candidate()
    conflicts = [
        MemoryConflict(
            candidate=candidate,
            existing_memory_id=MemoryId("mem_reinforce"),
            reason=DUPLICATE_MEMORY,
            recommended_operation=MemoryOperation.REINFORCE,
        ),
        MemoryConflict(
            candidate=candidate,
            existing_memory_id=MemoryId("mem_conflict"),
            reason=CONFLICTS_WITH_EXISTING_MEMORY,
            recommended_operation=MemoryOperation.MARK_CONFLICT,
        ),
        MemoryConflict(
            candidate=candidate,
            existing_memory_id=MemoryId("mem_supersede"),
            reason=SUPERSEDES_EXISTING_MEMORY,
            recommended_operation=MemoryOperation.SUPERSEDE,
        ),
    ]
    if reverse:
        conflicts.reverse()
    decision = _evaluate(
        candidate,
        policy=MemoryPolicy(StubConflictResolver(conflicts)),
    )
    assert decision.allowed is True
    assert decision.operation == MemoryOperation.SUPERSEDE
    assert decision.target_memory_id == MemoryId("mem_supersede")
    assert decision.requires_user_confirmation is True
    assert decision.reason_codes == [
        CONFLICT_REQUIRES_REVIEW,
        SUPERSEDES_EXISTING_MEMORY,
    ]
