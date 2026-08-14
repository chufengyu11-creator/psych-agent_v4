"""Tests for deterministic memory evaluation."""

from evaluation.evaluators.memory_evaluator import (
    ExpectedMemoryCandidate,
    ExpectedMemoryDecision,
    evaluate_memory_candidate,
    evaluate_memory_policy,
)
from schemas.common import MessageId
from schemas.memory import (
    MemoryCandidate,
    MemoryOperation,
    MemoryPolicyInput,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from services.memory_policy import MEMORY_DISABLED, MemoryPolicy


def _candidate(source: str = "msg_1") -> MemoryCandidate:
    return MemoryCandidate(
        candidate_type=MemoryType.INTERACTION_PREFERENCE,
        content="用户希望每次一个小步骤。",
        source_message_ids=[MessageId(source)],
        source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
        confidence=0.9,
        requires_user_confirmation=False,
        sensitivity=MemorySensitivity.LOW,
        recommended_operation=MemoryOperation.CREATE,
    )


EXPECTED = ExpectedMemoryCandidate(
    "interaction_preference",
    ("一个小步骤",),
    ("msg_1",),
    "explicit_user_statement",
    "low",
    "CREATE",
    False,
)


def test_legal_interaction_preference_passes() -> None:
    assert (
        evaluate_memory_candidate(_candidate(), EXPECTED, input_message_ids={MessageId("msg_1")})
        == ()
    )


def test_unknown_source_message_fails() -> None:
    reasons = evaluate_memory_candidate(
        _candidate("unknown"), EXPECTED, input_message_ids={MessageId("msg_1")}
    )
    assert "unknown_memory_source_message" in reasons


def test_policy_confirmation_mismatch_fails() -> None:
    decision = MemoryPolicy().evaluate_candidate(
        MemoryPolicyInput(candidate=_candidate(), user_memory_enabled=True)
    )
    reasons = evaluate_memory_policy(decision, ExpectedMemoryDecision(True, "CREATE", True))
    assert "memory_confirmation_mismatch" in reasons


def test_memory_disabled_policy_rejection_is_checked() -> None:
    decision = MemoryPolicy().evaluate_candidate(
        MemoryPolicyInput(candidate=_candidate(), user_memory_enabled=False)
    )
    expected = ExpectedMemoryDecision(False, None, False, (MEMORY_DISABLED,))
    assert evaluate_memory_policy(decision, expected) == ()
