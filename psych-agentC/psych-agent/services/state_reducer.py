"""Deterministic session-state reducer.

StateReducer is deliberately not model-backed. It validates and applies
StateDelta objects into a new SessionState version without mutating the previous
state object.
"""

from schemas.common import SourceReference
from schemas.feedback import FeedbackResult
from schemas.risk import RiskResult
from schemas.state import (
    EmotionReport,
    GoalUpdate,
    RiskState,
    SessionState,
    StateDelta,
    StateItem,
    StateOperation,
    StrategyPreference,
    TopicUpdate,
)


class StateReducer:
    """Applies typed state deltas to produce the next SessionState."""

    def apply(
        self,
        previous_state: SessionState,
        delta: StateDelta,
        feedback: FeedbackResult | None,
        risk: RiskResult,
    ) -> SessionState:
        """Return a new state version after applying delta, feedback, and risk."""

        next_state = previous_state.model_copy(deep=True)
        next_state.version = previous_state.version + 1
        next_state.risk_state = RiskState(
            level=risk.risk_level,
            categories=risk.categories,
            reason_codes=risk.reason_codes,
        )
        self._apply_topic_updates(next_state, delta.topic_updates)
        self._apply_goal_updates(next_state, delta.goal_updates)
        self._apply_emotions(next_state, delta.reported_emotions)
        self._apply_strategy_preferences(next_state, delta.strategy_preferences)
        if (
            feedback is not None
            and feedback.recommended_adjustment is not None
            and delta.topic_updates
        ):
            self._append_unique(
                next_state.user_preferences,
                StateItem(
                    value=f"strategy_adjustment:{feedback.recommended_adjustment}",
                    source=SourceReference(
                        message_id=delta.topic_updates[0].source_message_id,
                    ),
                ),
            )
        return next_state

    def _apply_topic_updates(self, state: SessionState, updates: list[TopicUpdate]) -> None:
        """Apply active-topic add/update/remove operations."""

        for update in updates:
            item = StateItem(
                value=update.topic,
                source=SourceReference(message_id=update.source_message_id),
            )
            if update.operation == StateOperation.REMOVE:
                self._deactivate_value(state.active_topics, update.topic)
            else:
                self._append_unique(state.active_topics, item)

    def _apply_goal_updates(self, state: SessionState, updates: list[GoalUpdate]) -> None:
        """Apply session-goal updates using the newest explicit goal."""

        for update in updates:
            if update.operation == StateOperation.REMOVE:
                state.session_goal = None
            else:
                state.session_goal = update.goal

    def _apply_emotions(self, state: SessionState, reports: list[EmotionReport]) -> None:
        """Append newly reported emotions without duplicating active values."""

        for report in reports:
            self._append_unique(
                state.reported_emotions,
                StateItem(
                    value=report.label,
                    source=SourceReference(message_id=report.source_message_id),
                ),
            )

    def _apply_strategy_preferences(
        self,
        state: SessionState,
        preferences: list[StrategyPreference],
    ) -> None:
        """Apply user-stated dialogue style preferences."""

        for preference in preferences:
            if preference.operation == StateOperation.REMOVE:
                self._deactivate_value(state.user_preferences, preference.value)
            else:
                self._append_unique(
                    state.user_preferences,
                    StateItem(
                        value=preference.value,
                        source=SourceReference(
                            message_id=preference.source_message_id,
                        ),
                    ),
                )

    def _append_unique(self, items: list[StateItem], candidate: StateItem) -> None:
        """Append candidate when no active item with the same value exists."""

        if not any(item.value == candidate.value and item.active for item in items):
            items.append(candidate)

    def _deactivate_value(self, items: list[StateItem], value: str) -> None:
        """Mark matching state items inactive instead of deleting evidence."""

        for item in items:
            if item.value == value:
                item.active = False
