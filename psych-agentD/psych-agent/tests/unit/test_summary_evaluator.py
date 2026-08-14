"""Unit tests for deterministic rolling-summary faithfulness checks."""

from evaluation.evaluators.summary_evaluator import evaluate_summary_faithfulness
from schemas.common import MessageId
from schemas.summary import RollingSummary
from tests.fixtures.interventions import pending_reflective_intervention
from tests.fixtures.messages import work_stress_dialogue


def _valid_summary() -> RollingSummary:
    messages = work_stress_dialogue()
    return RollingSummary(
        session_id=messages[0].session_id,
        summary_version=1,
        covered_from=messages[0].id,
        covered_to=messages[2].id,
        current_problem="用户和直属领导沟通紧张。",
        important_user_statements=[messages[0].content, messages[2].content],
        source_message_ids=[messages[0].id, messages[2].id],
    )


def test_valid_summary_passes_all_faithfulness_checks() -> None:
    report = evaluate_summary_faithfulness(
        _valid_summary(),
        work_stress_dialogue(),
    )

    assert report.is_faithful is True
    assert report.failed == 0
    assert report.passed == report.total
    assert report.reason_codes == ()


def test_unknown_source_fails() -> None:
    summary = _valid_summary().model_copy(
        update={"source_message_ids": [MessageId("msg_not_in_input")]}
    )

    report = evaluate_summary_faithfulness(summary, work_stress_dialogue())

    assert report.is_faithful is False
    assert "unknown_source_message" in report.reason_codes


def test_retained_message_citation_fails() -> None:
    summary = _valid_summary()

    report = evaluate_summary_faithfulness(
        summary,
        work_stress_dialogue(),
        retained_message_ids={MessageId("msg_003")},
    )

    assert "retained_message_cited" in report.reason_codes


def test_reversed_coverage_fails() -> None:
    messages = work_stress_dialogue()
    summary = _valid_summary().model_copy(
        update={
            "covered_from": messages[2].id,
            "covered_to": messages[0].id,
        }
    )

    report = evaluate_summary_faithfulness(summary, messages)

    assert "coverage_order_invalid" in report.reason_codes


def test_factual_content_without_sources_fails() -> None:
    summary = _valid_summary().model_copy(update={"source_message_ids": []})

    report = evaluate_summary_faithfulness(summary, work_stress_dialogue())

    assert "missing_sources_for_factual_content" in report.reason_codes


def test_pending_intervention_written_as_response_fails() -> None:
    summary = _valid_summary().model_copy(
        update={"strategy_responses": ["reflective_listening: negative; poor"]}
    )

    report = evaluate_summary_faithfulness(
        summary,
        work_stress_dialogue(),
        interventions=[pending_reflective_intervention()],
    )

    assert "pending_intervention_has_response" in report.reason_codes


def test_nonconsecutive_version_fails() -> None:
    summary = _valid_summary().model_copy(update={"summary_version": 2})

    report = evaluate_summary_faithfulness(summary, work_stress_dialogue())

    assert "summary_version_invalid" in report.reason_codes
