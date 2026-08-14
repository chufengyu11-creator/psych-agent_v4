"""Deterministic state-delta and session-state evaluation."""

from evaluation.results import stable_reason_codes
from schemas.common import MessageId
from schemas.state import SessionState, StateDelta


def evaluate_state(
    actual: StateDelta | SessionState,
    *,
    strategy_preferences_contains: str | None,
    input_message_ids: set[MessageId],
    explicit_request: str | None = None,
) -> tuple[str, ...]:
    """Check preference content, request equality, and message provenance."""

    if isinstance(actual, StateDelta):
        preferences = [item.value for item in actual.strategy_preferences]
        source_ids = [item.source_message_id for item in actual.strategy_preferences]
        actual_request = actual.explicit_user_request
    else:
        preferences = [item.value for item in actual.user_preferences if item.active]
        source_ids = [item.source.message_id for item in actual.user_preferences if item.active]
        actual_request = None
    reasons: list[str] = []
    if strategy_preferences_contains is not None and not any(
        _matches(preference, strategy_preferences_contains) for preference in preferences
    ):
        reasons.append("strategy_preference_missing")
    if any(source_id not in input_message_ids for source_id in source_ids):
        reasons.append("unknown_state_source_message")
    if explicit_request is not None and _normalize(actual_request or "") != _normalize(
        explicit_request
    ):
        reasons.append("explicit_request_mismatch")
    return stable_reason_codes(reasons)


def _matches(actual: str, expected: str) -> bool:
    left = _normalize(actual)
    right = _normalize(expected)
    return bool(left and right) and (left == right or left in right or right in left)


def _normalize(value: str) -> str:
    return " ".join(value.strip().casefold().split())


__all__ = ["evaluate_state"]
