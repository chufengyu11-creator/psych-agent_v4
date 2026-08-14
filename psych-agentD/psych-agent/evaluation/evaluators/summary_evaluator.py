"""Deterministic faithfulness checks for rolling summaries."""

from dataclasses import dataclass

from schemas.common import MessageId
from schemas.intervention import InterventionRecord, InterventionStatus
from schemas.messages import Message
from schemas.summary import RollingSummary


@dataclass(frozen=True)
class SummaryFaithfulnessReport:
    """Aggregate result of stable, non-model summary invariants."""

    total: int
    passed: int
    failed: int
    reason_codes: tuple[str, ...]

    @property
    def is_faithful(self) -> bool:
        """Return whether every configured invariant passed."""

        return self.failed == 0


def evaluate_summary_faithfulness(
    summary: RollingSummary,
    messages: list[Message],
    *,
    previous_summary: RollingSummary | None = None,
    interventions: list[InterventionRecord] | None = None,
    retained_message_ids: set[MessageId] | None = None,
) -> SummaryFaithfulnessReport:
    """Evaluate coverage, provenance, versioning, and intervention invariants."""

    message_by_id = {message.id: message for message in messages}
    previous_sources = (
        set(previous_summary.source_message_ids) if previous_summary is not None else set()
    )
    known_sources = set(message_by_id) | previous_sources
    retained = retained_message_ids or set()
    supplied_interventions = interventions or []

    covered_from = message_by_id.get(summary.covered_from)
    covered_to = message_by_id.get(summary.covered_to)
    coverage_present = covered_from is not None and covered_to is not None
    coverage_order_valid = False
    if covered_from is not None and covered_to is not None:
        coverage_order_valid = covered_from.sequence_number <= covered_to.sequence_number

    new_sources = [
        message_id
        for message_id in summary.source_message_ids
        if message_id not in previous_sources
    ]
    source_outside_coverage = False
    if covered_from is not None and covered_to is not None and coverage_order_valid:
        source_outside_coverage = any(
            message_id in message_by_id
            and not (
                covered_from.sequence_number
                <= message_by_id[message_id].sequence_number
                <= covered_to.sequence_number
            )
            for message_id in new_sources
        )

    checks = (
        (
            "session_mismatch",
            all(message.session_id == summary.session_id for message in messages),
        ),
        ("coverage_message_missing", coverage_present),
        ("coverage_order_invalid", coverage_order_valid),
        (
            "unknown_source_message",
            all(message_id in known_sources for message_id in summary.source_message_ids),
        ),
        (
            "retained_message_cited",
            not any(message_id in retained for message_id in summary.source_message_ids),
        ),
        ("source_outside_coverage", not source_outside_coverage),
        (
            "missing_sources_for_factual_content",
            not _has_factual_content(summary) or bool(summary.source_message_ids),
        ),
        (
            "pending_intervention_has_response",
            not _pending_intervention_has_response(summary, supplied_interventions),
        ),
        (
            "summary_version_invalid",
            summary.summary_version
            == (previous_summary.summary_version + 1 if previous_summary is not None else 1),
        ),
        (
            "coverage_regression",
            not _coverage_regressed(
                summary,
                previous_summary=previous_summary,
                message_by_id=message_by_id,
            ),
        ),
    )
    reason_codes = tuple(reason for reason, passed in checks if not passed)
    failed = len(reason_codes)
    total = len(checks)
    return SummaryFaithfulnessReport(
        total=total,
        passed=total - failed,
        failed=failed,
        reason_codes=reason_codes,
    )


def _has_factual_content(summary: RollingSummary) -> bool:
    return any(
        [
            summary.current_problem,
            summary.session_goal,
            *summary.important_user_statements,
            *summary.strategies_attempted,
            *summary.strategy_responses,
            *summary.open_questions,
        ]
    )


def _pending_intervention_has_response(
    summary: RollingSummary,
    interventions: list[InterventionRecord],
) -> bool:
    if not summary.strategy_responses:
        return False
    pending_strategies = {
        intervention.strategy
        for intervention in interventions
        if intervention.status == InterventionStatus.PENDING
    }
    evaluated_strategies = {
        intervention.strategy
        for intervention in interventions
        if intervention.status == InterventionStatus.EVALUATED
    }
    if pending_strategies and not evaluated_strategies:
        return True
    pending_only = pending_strategies - evaluated_strategies
    return any(
        strategy in response for response in summary.strategy_responses for strategy in pending_only
    )


def _coverage_regressed(
    summary: RollingSummary,
    *,
    previous_summary: RollingSummary | None,
    message_by_id: dict[MessageId, Message],
) -> bool:
    if previous_summary is None:
        return False
    if summary.covered_from != previous_summary.covered_from:
        return True
    previous_to = message_by_id.get(previous_summary.covered_to)
    current_to = message_by_id.get(summary.covered_to)
    if previous_to is None or current_to is None:
        return False
    return current_to.sequence_number < previous_to.sequence_number


__all__ = ["SummaryFaithfulnessReport", "evaluate_summary_faithfulness"]
