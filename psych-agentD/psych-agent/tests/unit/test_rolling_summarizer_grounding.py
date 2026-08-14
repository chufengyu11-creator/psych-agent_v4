"""Grounding, strict-mode, and model-execution tests for RollingSummarizer."""

import logging
from collections.abc import Mapping
from typing import cast

import pytest

from agents.rolling_summarizer import RollingSummarizer
from llm.exceptions import LLMStructuredOutputError, LLMTimeoutError
from llm.structured_output import ModelT
from schemas.common import MessageId
from schemas.summary import RollingSummarizerInput
from services.model_execution import ModelExecutionFailure, ModelExecutionStatus
from tests.fixtures.interventions import (
    evaluated_poor_fit_intervention,
    pending_reflective_intervention,
)
from tests.fixtures.messages import work_stress_dialogue
from tests.fixtures.states import empty_state
from tests.fixtures.summaries import rolling_summarizer_input
from tests.unit.test_rolling_summarizer import RecordingFallbackSummarizer


class DraftClient:
    """Typed structured client returning one configured draft payload."""

    def __init__(self, draft: dict[str, object]) -> None:
        self._draft = draft
        self.output_model_name = ""
        self.prompt = ""
        self.model_name: str | None = None
        self.metadata: Mapping[str, object] | None = None

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Return the configured draft through the requested output model."""

        self.output_model_name = output_model.__name__
        self.prompt = prompt
        self.model_name = model_name
        self.metadata = metadata
        return output_model.model_validate(self._draft)


class ExceptionClient:
    """Typed structured client that raises one configured exception."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Raise the configured error."""

        _ = (prompt, output_model, model_name, metadata)
        raise self._error


def _current_problem_draft(
    *,
    message_id: str = "msg_001",
    quote: str = "沟通很紧张",
    value: str = "用户和直属领导沟通紧张。",
) -> dict[str, object]:
    """Build a minimal current-problem draft."""

    return {
        "current_problem": {
            "value": value,
            "evidence": [{"message_id": message_id, "quote": quote}],
        }
    }


def _last_execution_record(
    caplog: pytest.LogCaptureFixture,
) -> logging.LogRecord:
    """Return the latest rolling-summarizer model execution log record."""

    return next(
        record
        for record in reversed(caplog.records)
        if record.name == "agents.rolling_summarizer"
        and record.getMessage() == "model_execution"
    )


def _record_status(record: logging.LogRecord) -> str:
    """Return the typed model-execution status carried in logging extra."""

    return cast("str", record.__dict__["status"])


def _record_model_name(record: logging.LogRecord) -> str | None:
    """Return the typed model name carried in logging extra."""

    value = record.__dict__["model_name"]
    return value if isinstance(value, str) or value is None else str(value)


def _record_reason_codes(record: logging.LogRecord) -> tuple[str, ...]:
    """Return typed model-execution reason codes carried in logging extra."""

    value = record.__dict__["reason_codes"]
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    return (str(value),)


@pytest.mark.asyncio
async def test_success_event_and_private_draft_adapter_arguments(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Successful model execution records safe metadata and local control fields."""

    client = DraftClient(_current_problem_draft())

    with caplog.at_level(logging.INFO, logger="agents.rolling_summarizer"):
        result = await RollingSummarizer(
            client,
            model_name="summary-model",
        ).summarize(rolling_summarizer_input())

    record = _last_execution_record(caplog)
    assert client.output_model_name == "_GroundedRollingSummaryDraft"
    assert client.model_name == "summary-model"
    assert client.metadata == {"agent": "rolling_summarizer"}
    assert "INPUT_JSON" in client.prompt
    assert result.session_id == rolling_summarizer_input().session_id
    assert result.summary_version == 1
    assert result.covered_from == MessageId("msg_001")
    assert result.covered_to == MessageId("msg_005")
    assert result.source_message_ids == [MessageId("msg_001")]
    assert _record_status(record) == ModelExecutionStatus.MODEL_SUCCESS.value
    assert _record_model_name(record) == "summary-model"
    assert _record_reason_codes(record) == ()


@pytest.mark.parametrize(
    ("draft", "expected_reason"),
    [
        (
            _current_problem_draft(message_id="msg_unknown", quote="missing"),
            "unknown_summary_source_message",
        ),
        (
            _current_problem_draft(message_id="msg_002", quote="听起来这段沟通关系"),
            "current_problem_requires_user_source",
        ),
        (
            _current_problem_draft(quote="not in this message"),
            "summary_evidence_quote_not_found",
        ),
        (
            {
                "strategies_attempted": [
                    {
                        "strategy": "reflective_listening",
                        "evidence": [{"message_id": "msg_001", "quote": "沟通很紧张"}],
                    }
                ]
            },
            "strategy_attempt_requires_assistant_source",
        ),
        (
            {
                "strategies_attempted": [
                    {
                        "strategy": "invented_strategy",
                        "evidence": [
                            {
                                "message_id": "msg_002",
                                "quote": "听起来这段沟通关系",
                            }
                        ],
                    }
                ]
            },
            "strategy_attempt_not_linked_to_intervention",
        ),
        (
            {
                "strategy_responses": [
                    {
                        "strategy": "reflective_listening",
                        "response": "用户接受了该策略。",
                        "evidence": [
                            {"message_id": "msg_003", "quote": "下一步具体怎么做"}
                        ],
                    }
                ]
            },
            "strategy_response_from_pending_intervention",
        ),
        (
            {
                "strategy_responses": [
                    {
                        "strategy": "reflective_listening",
                        "response": "用户接受了该策略。",
                        "evidence": [
                            {
                                "message_id": "msg_002",
                                "quote": "听起来这段沟通关系",
                            }
                        ],
                    }
                ]
            },
            "strategy_response_requires_user_source",
        ),
        (
            _current_problem_draft(value="用户有 depression。"),
            "unsupported_diagnosis",
        ),
    ],
)
@pytest.mark.asyncio
async def test_semantic_failures_record_stable_reason_codes(
    draft: dict[str, object],
    expected_reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Invalid grounded drafts fall back once with stable semantic reasons."""

    messages = work_stress_dialogue()
    interventions = (
        [pending_reflective_intervention()]
        if expected_reason == "strategy_response_from_pending_intervention"
        else [evaluated_poor_fit_intervention()]
    )
    payload = RollingSummarizerInput(
        session_id=messages[0].session_id,
        uncovered_messages=messages,
        current_state=empty_state(messages[0].session_id),
        interventions=interventions,
    )
    fallback = RecordingFallbackSummarizer()

    with caplog.at_level(logging.WARNING, logger="agents.rolling_summarizer"):
        await RollingSummarizer(
            DraftClient(draft),
            fallback=fallback,
        ).summarize(payload)

    record = _last_execution_record(caplog)
    assert fallback.summarize_calls == 1
    assert (
        _record_status(record)
        == ModelExecutionStatus.FALLBACK_SEMANTIC_VALIDATION_ERROR.value
    )
    assert expected_reason in _record_reason_codes(record)


@pytest.mark.parametrize(
    ("value", "expected_reason"),
    [
        ("用户有 depression。", "unsupported_diagnosis"),
        ("用户是 narcissistic。", "unsupported_personality_judgment"),
        ("用户 is actually avoiding responsibility。", "hidden_motive_inference"),
        ("用户 has fully recovered。", "unsupported_treatment_outcome"),
    ],
)
@pytest.mark.asyncio
async def test_unsupported_claim_reason_matrix(
    value: str,
    expected_reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shared claim-safety reason codes apply to every draft text."""

    with caplog.at_level(logging.WARNING, logger="agents.rolling_summarizer"):
        await RollingSummarizer(
            DraftClient(_current_problem_draft(value=value)),
        ).summarize(rolling_summarizer_input())

    assert expected_reason in _record_reason_codes(_last_execution_record(caplog))


@pytest.mark.parametrize(
    "draft",
    [
        {
            **_current_problem_draft(),
            "session_id": "wrong-session",
            "summary_version": 99,
            "covered_from": "msg_005",
            "covered_to": "msg_001",
            "source_message_ids": ["msg_999"],
        },
        {"current_problem": {"value": "事实", "evidence": []}},
    ],
)
@pytest.mark.asyncio
async def test_schema_error_from_invalid_private_draft_falls_back_once(
    draft: dict[str, object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Private draft schema rejects control fields and empty evidence."""

    fallback = RecordingFallbackSummarizer()

    with caplog.at_level(logging.WARNING, logger="agents.rolling_summarizer"):
        await RollingSummarizer(
            DraftClient(draft),
            fallback=fallback,
        ).summarize(rolling_summarizer_input())

    record = _last_execution_record(caplog)
    assert fallback.summarize_calls == 1
    assert _record_status(record) == ModelExecutionStatus.FALLBACK_SCHEMA_VALIDATION_ERROR.value
    assert _record_reason_codes(record) == ("llm_schema_validation_error",)


@pytest.mark.parametrize(
    ("error", "status", "reason"),
    [
        (
            LLMTimeoutError("timeout with private details"),
            ModelExecutionStatus.FALLBACK_CLIENT_ERROR,
            "llm_timeout",
        ),
        (
            LLMStructuredOutputError("bad json with private details"),
            ModelExecutionStatus.FALLBACK_STRUCTURED_OUTPUT_ERROR,
            "llm_structured_output_error",
        ),
        (
            RuntimeError("raw provider message with user content"),
            ModelExecutionStatus.FALLBACK_UNEXPECTED_ERROR,
            "llm_unexpected_error",
        ),
    ],
)
@pytest.mark.asyncio
async def test_client_exception_status_and_privacy_safe_logs(
    error: Exception,
    status: ModelExecutionStatus,
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Client errors are classified and logs avoid payload, quote, and raw errors."""

    fallback = RecordingFallbackSummarizer()

    with caplog.at_level(logging.WARNING, logger="agents.rolling_summarizer"):
        await RollingSummarizer(
            ExceptionClient(error),
            fallback=fallback,
            model_name="summary-model",
        ).summarize(rolling_summarizer_input())

    record = _last_execution_record(caplog)
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert fallback.summarize_calls == 1
    assert _record_status(record) == status.value
    assert _record_reason_codes(record) == (reason,)
    assert "沟通很紧张" not in rendered_logs
    assert "raw provider message" not in rendered_logs
    assert "INPUT_JSON" not in rendered_logs


@pytest.mark.asyncio
async def test_strict_client_exception_preserves_cause_without_leaking_message() -> None:
    """Strict mode raises stable metadata while preserving exception chaining."""

    original = RuntimeError("raw provider message with user content")

    with pytest.raises(ModelExecutionFailure) as exc_info:
        await RollingSummarizer(
            ExceptionClient(original),
            strict_model=True,
        ).summarize(rolling_summarizer_input())

    assert exc_info.value.__cause__ is original
    assert "raw provider message" not in str(exc_info.value)
    assert exc_info.value.status == ModelExecutionStatus.FALLBACK_UNEXPECTED_ERROR
