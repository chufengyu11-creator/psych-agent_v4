"""Feedback evaluator implementations."""

from schemas.feedback import (
    FeedbackInput,
    FeedbackLabel,
    FeedbackResult,
    ObjectiveProgress,
    StrategyFit,
)


class FakeFeedbackEvaluator:
    """Rule-based evaluator for the adaptive-loop skeleton."""

    async def run(self, payload: FeedbackInput) -> FeedbackResult:
        """Alias for evaluate so the class satisfies Agent protocol."""

        return await self.evaluate(payload)

    async def evaluate(self, payload: FeedbackInput) -> FeedbackResult:
        """Infer simple observable feedback from the latest user message."""

        text = payload.next_user_message.content
        negative_terms = [
            "不想",
            "别",
            "没用",
            "不要",
            "not helpful",
            "do not",
            "don't",
            "not want",
            "stop",
        ]
        if any(term in text for term in negative_terms):
            return FeedbackResult(
                observed_response="用户对上一轮方向表达了拒绝或不匹配",
                explicit_feedback=FeedbackLabel.NEGATIVE,
                objective_progress=ObjectiveProgress.NOT_ACHIEVED,
                strategy_fit=StrategyFit.POOR,
                recommended_adjustment="switch_or_clarify_strategy",
                confidence=0.7,
            )
        return FeedbackResult(
            observed_response="用户继续对话，但没有明确评价上一轮策略",
            explicit_feedback=FeedbackLabel.ABSENT,
            objective_progress=ObjectiveProgress.UNKNOWN,
            strategy_fit=StrategyFit.UNKNOWN,
            recommended_adjustment=None,
            confidence=0.5,
        )


