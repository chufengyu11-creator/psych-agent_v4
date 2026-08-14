"""Deterministic feedback and strategy-plan evaluation."""

from dataclasses import dataclass

from evaluation.results import stable_reason_codes
from schemas.feedback import FeedbackResult
from schemas.strategy import StrategyPlan


@dataclass(frozen=True, slots=True)
class ExpectedFeedback:
    explicit_feedback: str
    strategy_fit: str
    objective_progress: str
    recommended_adjustment: str | None


@dataclass(frozen=True, slots=True)
class ExpectedStrategy:
    next_strategy_one_of: tuple[str, ...]
    rejected_strategy: str | None = None
    required_avoid: tuple[str, ...] = ()


def evaluate_strategy(
    feedback: FeedbackResult | None,
    plan: StrategyPlan | None,
    *,
    expected_feedback: ExpectedFeedback | None = None,
    expected_strategy: ExpectedStrategy | None = None,
) -> tuple[str, ...]:
    """Return stable machine-readable mismatches for one adaptive-loop case."""

    reasons: list[str] = []
    if expected_feedback is not None:
        if (
            feedback is None
            or feedback.explicit_feedback.value != expected_feedback.explicit_feedback
        ):
            reasons.append("feedback_label_mismatch")
        if feedback is None or feedback.strategy_fit.value != expected_feedback.strategy_fit:
            reasons.append("strategy_fit_mismatch")
        if (
            feedback is None
            or feedback.objective_progress.value != expected_feedback.objective_progress
        ):
            reasons.append("objective_progress_mismatch")
        if (
            feedback is None
            or feedback.recommended_adjustment != expected_feedback.recommended_adjustment
        ):
            reasons.append("recommended_adjustment_mismatch")
    if expected_strategy is not None:
        actual = plan.primary_strategy.value if plan is not None else None
        if actual not in expected_strategy.next_strategy_one_of:
            reasons.append("next_strategy_not_allowed")
        if (
            expected_strategy.rejected_strategy is not None
            and actual == expected_strategy.rejected_strategy
        ):
            reasons.append("rejected_strategy_repeated")
        avoid = {_normalize(item) for item in plan.avoid} if plan is not None else set()
        if any(_normalize(required) not in avoid for required in expected_strategy.required_avoid):
            reasons.append("required_avoid_missing")
    return stable_reason_codes(reasons)


def _normalize(value: str) -> str:
    return " ".join(value.strip().casefold().split())


__all__ = ["ExpectedFeedback", "ExpectedStrategy", "evaluate_strategy"]
