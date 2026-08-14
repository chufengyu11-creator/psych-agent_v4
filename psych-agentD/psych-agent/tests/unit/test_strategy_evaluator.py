"""Tests for deterministic strategy evaluation."""

from evaluation.evaluators.strategy_evaluator import (
    ExpectedFeedback,
    ExpectedStrategy,
    evaluate_strategy,
)
from schemas.feedback import FeedbackLabel, FeedbackResult, ObjectiveProgress, StrategyFit
from schemas.state import ConversationPhase
from schemas.strategy import StrategyPlan, StrategyType


def _feedback(adjustment: str | None = "switch_or_clarify_strategy") -> FeedbackResult:
    return FeedbackResult(
        observed_response="explicit rejection",
        explicit_feedback=FeedbackLabel.NEGATIVE,
        objective_progress=ObjectiveProgress.NOT_ACHIEVED,
        strategy_fit=StrategyFit.POOR,
        recommended_adjustment=adjustment,
        confidence=1.0,
    )


def _plan(strategy: StrategyType = StrategyType.CLARIFICATION) -> StrategyPlan:
    return StrategyPlan(
        conversation_phase=ConversationPhase.GOAL_ALIGNMENT,
        primary_strategy=strategy,
        objective="align",
        reason="rejected",
        avoid=["repeat the previous strategy"],
    )


EXPECTED_FEEDBACK = ExpectedFeedback(
    "negative", "poor", "not_achieved", "switch_or_clarify_strategy"
)
EXPECTED_STRATEGY = ExpectedStrategy(
    ("clarification",), "reflective_listening", ("repeat the previous strategy",)
)


def test_correct_feedback_and_strategy_pass() -> None:
    assert (
        evaluate_strategy(
            _feedback(),
            _plan(),
            expected_feedback=EXPECTED_FEEDBACK,
            expected_strategy=EXPECTED_STRATEGY,
        )
        == ()
    )


def test_wrong_recommended_adjustment_fails() -> None:
    reasons = evaluate_strategy(_feedback("wrong"), _plan(), expected_feedback=EXPECTED_FEEDBACK)
    assert reasons == ("recommended_adjustment_mismatch",)


def test_rejected_strategy_repeated_fails() -> None:
    expected = ExpectedStrategy(
        ("reflective_listening", "clarification"), rejected_strategy="reflective_listening"
    )
    reasons = evaluate_strategy(
        None, _plan(StrategyType.REFLECTIVE_LISTENING), expected_strategy=expected
    )
    assert "rejected_strategy_repeated" in reasons
