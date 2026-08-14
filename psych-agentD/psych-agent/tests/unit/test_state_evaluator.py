"""Tests for deterministic state evaluation."""

from evaluation.evaluators.state_evaluator import evaluate_state
from schemas.common import MessageId
from schemas.state import StateDelta, StateOperation, StrategyPreference


def _delta(source: str = "msg_1") -> StateDelta:
    return StateDelta(
        explicit_user_request="每次一个小步骤",
        strategy_preferences=[
            StrategyPreference(
                operation=StateOperation.ADD,
                value="希望每次一个小步骤",
                source_message_id=MessageId(source),
            )
        ],
    )


def test_small_step_preference_passes() -> None:
    assert (
        evaluate_state(
            _delta(),
            strategy_preferences_contains="每次一个小步骤",
            input_message_ids={MessageId("msg_1")},
        )
        == ()
    )


def test_missing_preference_fails() -> None:
    reasons = evaluate_state(
        StateDelta(),
        strategy_preferences_contains="每次一个小步骤",
        input_message_ids={MessageId("msg_1")},
    )
    assert reasons == ("strategy_preference_missing",)


def test_unknown_source_message_fails() -> None:
    reasons = evaluate_state(
        _delta("unknown"),
        strategy_preferences_contains="每次一个小步骤",
        input_message_ids={MessageId("msg_1")},
    )
    assert "unknown_state_source_message" in reasons
